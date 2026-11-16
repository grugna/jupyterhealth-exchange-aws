"""The serving side: the certificate, the ALB, the web, MCP and cron services, DNS.

One ALB, one HTTPS listener with one certificate carrying both host names. The default action
forwards to the web target group; a host-header rule sends the MCP host to the MCP target
group. HTTP redirects to HTTPS. With `tls_to_tasks` on (the default) the target groups and
health checks speak HTTPS to the containers, which issue their own certificates at start.

The periodic jobs are one always-on task running supercronic on the crontab the JHE image
ships, the arrangement JHE's own compose and Fly deployments use: the schedule lives in JHE,
so there is no second list here to fall out of step. They are the application's background
work and run with exactly the web service's roles. A failing job is a log line, not a task
exit, so the monitoring stack alarms on supercronic's log. Anything a person runs uses the ops
stack's task instead.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_route53 as r53
from aws_cdk import aws_route53_targets as r53_targets
from cdk_nag import NagSuppressions
from constructs import Construct

from jhe_aws.config import DnsMode, EnvironmentConfig
from jhe_aws.images import Images

from .data import DataStack
from .jhe_container import (
    JHE_PORT,
    MCP_PORT,
    execution_role,
    jhe_task_definition,
    jhe_task_role,
    operator_secret,
    suppress_execution_role_wildcards,
    task_security_group,
)
from .network import NetworkStack, _retention
from .ops import OpsStack
from .shared import SharedStack

HEALTH_PATH = "/health"
CRONTAB = "/code/deploy/crontab"  # in the JHE image


class ComputeStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        cfg: EnvironmentConfig,
        images: Images,
        network: NetworkStack,
        shared: SharedStack,
        data: DataStack,
        ops: OpsStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.cfg = cfg
        compute = cfg.compute
        tls = compute.tls_to_tasks.enabled
        self.web_host = compute.domain_name
        self.mcp_host = compute.mcp_domain_name if compute.mcp.enabled else None

        self.zone = self._zone()
        self.certificate = self._certificate()
        self.alb_sg = ec2.SecurityGroup(
            self,
            "AlbSecurityGroup",
            vpc=network.vpc,
            description=f"JHE {cfg.name} ALB",
            allow_all_outbound=False,
        )
        self.alb = elbv2.ApplicationLoadBalancer(
            self,
            "Alb",
            load_balancer_name=f"jhe-{cfg.name}",
            vpc=network.vpc,
            vpc_subnets=network.public_subnets,
            internet_facing=True,
            security_group=self.alb_sg,
            drop_invalid_header_fields=True,
            http2_enabled=True,
            idle_timeout=cdk.Duration.seconds(compute.alb_idle_timeout_seconds),
        )
        self.alb.log_access_logs(data.alb_logs_bucket, prefix=f"jhe-{cfg.name}")
        self.alb.add_redirect()  # :80 -> :443
        self.listener = self.alb.add_listener(
            "Https",
            port=443,
            certificates=[self.certificate],
            ssl_policy=elbv2.SslPolicy.RECOMMENDED_TLS,
        )
        NagSuppressions.add_resource_suppressions(
            self.alb_sg,
            [
                {
                    "id": "AwsSolutions-EC23",
                    "reason": "A public web application: 443 (and 80, redirected) from anywhere.",
                }
            ],
            apply_to_children=True,
        )

        # --- web -----------------------------------------------------------------------------
        web_logs = logs.LogGroup(
            self,
            "WebLogs",
            log_group_name=f"/jhe/{cfg.name}/web",
            retention=_retention(cfg.log_retention_days),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        web_task_role = jhe_task_role(self, "WebTaskRole", cfg, data, role_name=f"jhe-{cfg.name}-web-task")
        web_execution_role = execution_role(
            self, "WebExecutionRole", cfg, role_name=f"jhe-{cfg.name}-web-execution"
        )
        self.web_task = jhe_task_definition(
            self,
            "WebTask",
            cfg,
            shared,
            data,
            family=f"jhe-{cfg.name}-web",
            image_digest=images.web,
            cpu=compute.web.cpu,
            memory=compute.web.memory,
            ephemeral_storage_gib=compute.web.ephemeral_storage_gib,
            task_role=web_task_role,
            execution_role=web_execution_role,
            log_group=web_logs,
            stream_prefix="web",
            command=None,  # the image's gunicorn
            port=True,
        )
        self.web_sg = task_security_group(
            self, "WebSecurityGroup", cfg, network.vpc, data, database=True, name="web"
        )
        self.web_service = self._service(
            "WebService", f"jhe-{cfg.name}-web", self.web_task, shared, network, self.web_sg
        )
        scaling = self.web_service.auto_scale_task_count(
            min_capacity=compute.web.min_count, max_capacity=compute.web.max_count
        )
        scaling.scale_on_cpu_utilization(
            "Cpu",
            target_utilization_percent=compute.web.target_cpu_percent,
            scale_in_cooldown=cdk.Duration.minutes(5),
            scale_out_cooldown=cdk.Duration.minutes(1),
        )
        # Only requests for our names reach a container: anything else (the ALB's own address,
        # which scanners try constantly) gets a fixed 404 from the listener, never a Django
        # DisallowedHost in the log. Health checks go to the target group directly, not via rules.
        self.listener.add_action(
            "NotOurHost",
            action=elbv2.ListenerAction.fixed_response(
                404, content_type="text/plain", message_body="not found"
            ),
        )
        self.web_target_group = self.listener.add_targets(
            "Web",
            port=JHE_PORT,
            protocol=elbv2.ApplicationProtocol.HTTPS if tls else elbv2.ApplicationProtocol.HTTP,
            targets=[self.web_service],
            conditions=[elbv2.ListenerCondition.host_headers([self.web_host])],
            priority=20,
            health_check=self._health_check(tls),
            deregistration_delay=cdk.Duration.seconds(30),
        )

        # --- mcp -----------------------------------------------------------------------------
        self.mcp_service: ecs.FargateService | None = None
        self.mcp_target_group: elbv2.ApplicationTargetGroup | None = None
        if self.mcp_host:
            mcp_task = self._mcp_task(images, shared, data)
            self.mcp_sg = task_security_group(
                self, "McpSecurityGroup", cfg, network.vpc, data, database=False, name="mcp"
            )
            self.mcp_service = self._service(
                "McpService", f"jhe-{cfg.name}-mcp", mcp_task, shared, network, self.mcp_sg
            )
            self.mcp_target_group = self.listener.add_targets(
                "Mcp",
                port=MCP_PORT,
                protocol=elbv2.ApplicationProtocol.HTTPS if tls else elbv2.ApplicationProtocol.HTTP,
                targets=[self.mcp_service],
                conditions=[elbv2.ListenerCondition.host_headers([self.mcp_host])],
                priority=10,
                health_check=self._health_check(tls),
                deregistration_delay=cdk.Duration.seconds(30),
            )

        # --- cron: supercronic on the image's crontab, with the web service's roles ------------
        self.cron_service: ecs.FargateService | None = None
        self.cron_logs: logs.LogGroup | None = None
        if compute.cron.enabled:
            self.cron_logs = logs.LogGroup(
                self,
                "CronLogs",
                log_group_name=f"/jhe/{cfg.name}/cron",
                retention=_retention(cfg.log_retention_days),
                removal_policy=cdk.RemovalPolicy.DESTROY,
            )
            cron_task = jhe_task_definition(
                self,
                "CronTask",
                cfg,
                shared,
                data,
                family=f"jhe-{cfg.name}-cron",
                image_digest=images.web,
                cpu=compute.cron.cpu,
                memory=compute.cron.memory,
                ephemeral_storage_gib=compute.cron.ephemeral_storage_gib,
                task_role=web_task_role,
                execution_role=web_execution_role,
                log_group=self.cron_logs,
                stream_prefix="cron",
                command=["supercronic", CRONTAB],
            )
            # Exactly one scheduler: a roll stops the old task before starting the new one, so
            # no job runs twice. A sweep cut short is picked up by the next one.
            self.cron_service = self._service(
                "CronService",
                f"jhe-{cfg.name}-cron",
                cron_task,
                shared,
                network,
                self.web_sg,
                behind_load_balancer=False,
            )

        # --- dns -----------------------------------------------------------------------------
        if self.zone is not None:
            for record_id, host in (("WebAlias", self.web_host), ("McpAlias", self.mcp_host)):
                if host:
                    r53.ARecord(
                        self,
                        record_id,
                        zone=self.zone,
                        record_name=host,
                        target=r53.RecordTarget.from_alias(r53_targets.LoadBalancerTarget(self.alb)),
                    )

        cdk.CfnOutput(self, "AlbDnsName", value=self.alb.load_balancer_dns_name)
        cdk.CfnOutput(self, "WebUrl", value=f"https://{self.web_host}")
        if self.mcp_host:
            cdk.CfnOutput(self, "McpUrl", value=f"https://{self.mcp_host}")
        cdk.CfnOutput(self, "WebServiceName", value=self.web_service.service_name)
        cdk.CfnOutput(self, "WebImageDigest", value=images.web)
        cdk.CfnOutput(self, "McpImageDigest", value=images.mcp)

    # --- pieces ----------------------------------------------------------------------------------

    def _zone(self) -> r53.IHostedZone | None:
        dns = self.cfg.compute.dns
        if dns.mode is not DnsMode.ROUTE53:
            return None
        assert dns.hosted_zone_id and dns.hosted_zone_name
        return r53.HostedZone.from_hosted_zone_attributes(
            self, "Zone", hosted_zone_id=dns.hosted_zone_id, zone_name=dns.hosted_zone_name
        )

    def _certificate(self) -> acm.ICertificate:
        dns = self.cfg.compute.dns
        if dns.certificate_arn:
            return acm.Certificate.from_certificate_arn(self, "Certificate", dns.certificate_arn)
        # With a zone, validation records are created and the stack waits for issuance. Without
        # one, the records to create appear in the stack's events and the deploy waits for them.
        validation = (
            acm.CertificateValidation.from_dns(self.zone)
            if self.zone
            else acm.CertificateValidation.from_dns()
        )
        return acm.Certificate(
            self,
            "Certificate",
            domain_name=self.web_host,
            subject_alternative_names=[self.mcp_host] if self.mcp_host else None,
            validation=validation,
        )

    def _health_check(self, tls: bool) -> elbv2.HealthCheck:
        return elbv2.HealthCheck(
            path=HEALTH_PATH,
            protocol=elbv2.Protocol.HTTPS if tls else elbv2.Protocol.HTTP,
            healthy_http_codes="200",
            interval=cdk.Duration.seconds(30),
            timeout=cdk.Duration.seconds(5),
            healthy_threshold_count=2,
            unhealthy_threshold_count=3,
        )

    def _service(
        self,
        construct_id: str,
        name: str,
        task: ecs.FargateTaskDefinition,
        shared: SharedStack,
        network: NetworkStack,
        security_group: ec2.SecurityGroup,
        *,
        behind_load_balancer: bool = True,
    ) -> ecs.FargateService:
        return ecs.FargateService(
            self,
            construct_id,
            service_name=name,
            cluster=shared.cluster,
            task_definition=task,
            security_groups=[security_group],
            vpc_subnets=network.private_subnets,
            assign_public_ip=False,
            desired_count=None if behind_load_balancer else 1,  # web is autoscaled
            min_healthy_percent=100 if behind_load_balancer else 0,
            max_healthy_percent=200 if behind_load_balancer else 100,
            circuit_breaker=ecs.DeploymentCircuitBreaker(enable=True, rollback=True),
            health_check_grace_period=cdk.Duration.seconds(120) if behind_load_balancer else None,
            enable_execute_command=False,
            propagate_tags=ecs.PropagatedTagSource.SERVICE,
        )

    def _mcp_task(self, images: Images, shared: SharedStack, data: DataStack) -> ecs.FargateTaskDefinition:
        cfg, compute = self.cfg, self.cfg.compute
        assert self.mcp_host
        mcp_logs = logs.LogGroup(
            self,
            "McpLogs",
            log_group_name=f"/jhe/{cfg.name}/mcp",
            retention=_retention(cfg.log_retention_days),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        task_role = iam.Role(
            self,
            "McpTaskRole",
            role_name=f"jhe-{cfg.name}-mcp-task",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description=f"JHE {cfg.name} MCP task role: nothing beyond the network",
        )
        exec_role = execution_role(self, "McpExecutionRole", cfg, role_name=f"jhe-{cfg.name}-mcp-execution")
        task = ecs.FargateTaskDefinition(
            self,
            "McpTask",
            family=f"jhe-{cfg.name}-mcp",
            cpu=compute.mcp.cpu,
            memory_limit_mib=compute.mcp.memory,
            task_role=task_role,
            execution_role=exec_role,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        environment = {
            "JHE_BASE_URL": f"https://{self.web_host}",
            "JHE_REDIRECT_URI": f"https://{self.mcp_host}/oauth/callback",
            "MCP_RESOURCE_URL": f"https://{self.mcp_host}",
            "MCP_HTTP_PORT": str(MCP_PORT),
            "MCP_REQUIRE_AUDIENCE": "true" if compute.mcp.require_audience else "false",
        }
        if compute.tls_to_tasks.enabled:
            environment["MCP_TLS"] = compute.tls_to_tasks.certificate.value
            environment["MCP_TLS_CN"] = self.mcp_host
        mcp_secret = operator_secret(self, "McpSecret", cfg, "mcp")
        container = task.add_container(
            "mcp",
            image=ecs.ContainerImage.from_registry(
                shared.mcp_repository.repository_uri_for_digest(images.mcp)
            ),
            environment=environment,
            secrets={
                "JHE_CLIENT_ID": ecs.Secret.from_secrets_manager(mcp_secret, "JHE_CLIENT_ID"),
                "JHE_CLIENT_SECRET": ecs.Secret.from_secrets_manager(mcp_secret, "JHE_CLIENT_SECRET"),
                "MCP_BROKER_KEY": ecs.Secret.from_secrets_manager(mcp_secret, "MCP_BROKER_KEY"),
            },
            logging=ecs.LogDrivers.aws_logs(log_group=mcp_logs, stream_prefix="mcp"),
            readonly_root_filesystem=False,  # the per-task certificate is written to a temporary directory
        )
        container.add_port_mappings(ecs.PortMapping(container_port=MCP_PORT, protocol=ecs.Protocol.TCP))
        shared.mcp_repository.grant_pull(exec_role)
        if data.key is not None:
            data.key.grant_decrypt(exec_role)
        suppress_execution_role_wildcards(task, exec_role)
        NagSuppressions.add_resource_suppressions(
            task,
            [
                {
                    "id": "AwsSolutions-ECS2",
                    "reason": "URLs and switches; the credentials are container secrets.",
                }
            ],
        )
        return task
