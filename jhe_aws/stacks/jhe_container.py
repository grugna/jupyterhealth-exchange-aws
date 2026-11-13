"""The JHE container as the web service and the ops task both run it: environment, secrets,
roles. One place, so the two task definitions cannot drift apart.

Plain configuration goes in `environment`; every credential is a container secret resolved by
ECS from Secrets Manager at task start, so nothing secret is in a task definition, an image or
a file. The database endpoint is read from the instance, the domain from configuration, so each
value is typed once.

The secrets are referenced in the consuming stack rather than as the data stack's constructs:
a secret under a customer-managed key would otherwise write the reading role into the key
policy (in the data stack), and the data stack cannot depend on the stack it serves. The
execution role is granted decrypt on the key here instead. The database and bootstrap-admin
secrets are referenced by ARN; the service and mcp secrets by name, because they are not
CloudFormation resources at all (`jhe-deploy secrets init` creates them; see data.py).
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as sm
from cdk_nag import NagSuppressions
from constructs import Construct

from jhe_aws.config import EmailMode, EnvironmentConfig

from .data import DATABASE_NAME, DataStack
from .shared import SharedStack

JHE_PORT = 8000
DB_CA_BUNDLE_PATH = "/etc/ssl/certs/db-ca-bundle.pem"  # where the JHE image stores DB_CA_BUNDLE_URL
MCP_PORT = 8401
CONTAINER_NAME = "jhe"


def jhe_environment(cfg: EnvironmentConfig, data: DataStack) -> dict[str, str]:
    compute = cfg.compute
    hosts = [compute.domain_name] + (
        [compute.mcp_domain_name] if compute.mcp.enabled and compute.mcp_domain_name else []
    )
    env = {
        "SITE_URL": f"https://{compute.domain_name}",
        "ALLOWED_HOSTS": ",".join(hosts),
        "SECURE_HSTS_SECONDS": str(compute.hsts_seconds),
        "DJANGO_LOG_LEVEL": compute.log_level,
        "JHE_IGNORE_DOTENV": "1",
        "DB_HOST": data.database.db_instance_endpoint_address,
        "DB_PORT": data.database.db_instance_endpoint_port,
        "DB_NAME": DATABASE_NAME,
        "PATIENT_ACCESS_SPOOL_DIR": "/data/patient-access/spool",
    }
    if cfg.data.verify_server_certificate:
        env["DB_SSLMODE"] = "verify-full"
        env["DB_SSLROOTCERT"] = DB_CA_BUNDLE_PATH
    if compute.tls_to_tasks.enabled:
        env["JHE_TLS"] = compute.tls_to_tasks.certificate.value
        env["JHE_TLS_CN"] = compute.domain_name
    if compute.email.mode is EmailMode.SMTP:
        env["SMTP_HOST"] = compute.email.smtp_host or ""
        env["SMTP_PORT"] = str(compute.email.smtp_port)
    env.update(compute.environment)
    return env


def task_security_group(
    scope: Construct,
    construct_id: str,
    cfg: EnvironmentConfig,
    vpc: ec2.IVpc,
    data: DataStack,
    *,
    database: bool,
    name: str,
) -> ec2.SecurityGroup:
    """The security group a task runs with, and (when `database`) the rule on the database's
    group that admits it, both in the calling stack."""
    group = ec2.SecurityGroup(
        scope, construct_id, vpc=vpc, description=f"JHE {cfg.name} {name}", allow_all_outbound=True
    )
    if database:
        ec2.CfnSecurityGroupIngress(
            scope,
            f"{construct_id}ToDatabase",
            group_id=data.database_sg.security_group_id,
            source_security_group_id=group.security_group_id,
            ip_protocol="tcp",
            from_port=5432,
            to_port=5432,
            description=f"JHE {name} to Postgres",
        )
    return group


def by_arn(scope: Construct, construct_id: str, secret: sm.ISecret) -> sm.ISecret:
    """`secret` as the consuming stack sees it: by its ARN, with no key attached (see the module
    docstring)."""
    return sm.Secret.from_secret_complete_arn(scope, construct_id, secret.secret_arn)


def operator_secret(scope: Construct, construct_id: str, cfg: EnvironmentConfig, which: str) -> sm.ISecret:
    """`jhe/<env>/service` or `jhe/<env>/mcp`, by name: created by `jhe-deploy secrets init`,
    never by a stack, so no stack update can regenerate what an operator filled. A read grant
    on it covers `secret:jhe/<env>/<which>-??????`, the name plus Secrets Manager's suffix."""
    return sm.Secret.from_secret_name_v2(scope, construct_id, f"jhe/{cfg.name}/{which}")


def jhe_secrets(
    scope: Construct, prefix: str, cfg: EnvironmentConfig, data: DataStack
) -> dict[str, ecs.Secret]:
    service = operator_secret(scope, f"{prefix}ServiceSecret", cfg, "service")
    database = by_arn(scope, f"{prefix}DatabaseSecret", data.database_secret)
    secrets = {
        "DB_USER": ecs.Secret.from_secrets_manager(database, "username"),
        "DB_PASSWORD": ecs.Secret.from_secrets_manager(database, "password"),
        "SECRET_KEY": ecs.Secret.from_secrets_manager(service, "SECRET_KEY"),
        "OIDC_RSA_PRIVATE_KEY": ecs.Secret.from_secrets_manager(service, "OIDC_RSA_PRIVATE_KEY"),
        "PATIENT_ACCESS_ENCRYPTION_KEYS": ecs.Secret.from_secrets_manager(
            service, "PATIENT_ACCESS_ENCRYPTION_KEYS"
        ),
    }
    if cfg.compute.email.mode is EmailMode.SMTP:
        secrets["SMTP_USER"] = ecs.Secret.from_secrets_manager(service, "SMTP_USER")
        secrets["SMTP_PASSWORD"] = ecs.Secret.from_secrets_manager(service, "SMTP_PASSWORD")
    if cfg.compute.open_wearables:
        secrets["OW_S3_ACCESS_KEY"] = ecs.Secret.from_secrets_manager(service, "OW_S3_ACCESS_KEY")
        secrets["OW_S3_SECRET_KEY"] = ecs.Secret.from_secrets_manager(service, "OW_S3_SECRET_KEY")
    return secrets


def jhe_task_role(
    scope: Construct,
    construct_id: str,
    cfg: EnvironmentConfig,
    data: DataStack,
    role_name: str,
    *,
    bucket: bool = True,
) -> iam.Role:
    """What a running JHE task may do: with `bucket`, read and write bundles and use the data
    key for them (the web service, the jobs task); without, nothing beyond the network (the
    migration task)."""
    role = iam.Role(
        scope,
        construct_id,
        role_name=role_name,
        assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        description=f"JHE {cfg.name} task role ({role_name})",
    )
    if not bucket:
        return role
    data.bundle_bucket.grant_read_write(role)
    if data.key is not None:
        data.key.grant_encrypt_decrypt(role)
    NagSuppressions.add_resource_suppressions(
        role,
        [
            {
                "id": "AwsSolutions-IAM5",
                "reason": "Bundle objects are written under per-patient prefixes the module chooses; "
                "the grant is scoped to this environment's bucket and its key.",
            }
        ],
        apply_to_children=True,
    )
    return role


def jhe_task_definition(
    scope: Construct,
    construct_id: str,
    cfg: EnvironmentConfig,
    shared: SharedStack,
    data: DataStack,
    *,
    family: str,
    image_digest: str,
    cpu: int,
    memory: int,
    task_role: iam.Role,
    execution_role: iam.Role,
    log_group: logs.ILogGroup,
    stream_prefix: str,
    command: list[str] | None,
    extra_secrets: dict[str, ecs.Secret] | None = None,
    port: bool = False,
    ephemeral_storage_gib: int | None = None,
) -> ecs.FargateTaskDefinition:
    """A Fargate task definition running the JHE image at `image_digest`."""
    task = ecs.FargateTaskDefinition(
        scope,
        construct_id,
        family=family,
        cpu=cpu,
        memory_limit_mib=memory,
        ephemeral_storage_gib=ephemeral_storage_gib,
        task_role=task_role,
        execution_role=execution_role,
        runtime_platform=ecs.RuntimePlatform(
            cpu_architecture=ecs.CpuArchitecture.ARM64,
            operating_system_family=ecs.OperatingSystemFamily.LINUX,
        ),
    )
    container = task.add_container(
        CONTAINER_NAME,
        image=ecs.ContainerImage.from_registry(shared.web_repository.repository_uri_for_digest(image_digest)),
        command=command,
        environment=jhe_environment(cfg, data),
        secrets={**jhe_secrets(scope, construct_id, cfg, data), **(extra_secrets or {})},
        logging=ecs.LogDrivers.aws_logs(log_group=log_group, stream_prefix=stream_prefix),
        readonly_root_filesystem=False,  # the spool and the per-task certificate are written at run time
        # A patient-access pull runs inside the web process: once the ALB has drained a task,
        # it gets Fargate's maximum to finish before SIGKILL (a dropped pull waits for the sweep).
        stop_timeout=cdk.Duration.seconds(120),
    )
    if port:
        container.add_port_mappings(ecs.PortMapping(container_port=JHE_PORT, protocol=ecs.Protocol.TCP))
    shared.web_repository.grant_pull(execution_role)
    if data.key is not None:
        data.key.grant_decrypt(execution_role)  # the secrets above are under the data key
    suppress_execution_role_wildcards(task, execution_role)
    NagSuppressions.add_resource_suppressions(
        task,
        [
            {
                "id": "AwsSolutions-ECS2",
                "reason": "The environment holds plain configuration (URLs, log level, the database "
                "endpoint); every credential is a container secret from Secrets Manager.",
            }
        ],
    )
    return task


def execution_role(scope: Construct, construct_id: str, cfg: EnvironmentConfig, role_name: str) -> iam.Role:
    role = iam.Role(
        scope,
        construct_id,
        role_name=role_name,
        assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        description=f"JHE {cfg.name} execution role ({role_name}): pull the image, write logs, read secrets",
    )
    return role


def suppress_execution_role_wildcards(task: ecs.FargateTaskDefinition, role: iam.Role) -> None:
    """After the container is added: the execution role's default policy holds
    ecr:GetAuthorizationToken, which takes no resource, and the log-stream grant."""
    NagSuppressions.add_resource_suppressions(
        role,
        [
            {
                "id": "AwsSolutions-IAM5",
                "reason": "ecr:GetAuthorizationToken takes no resource; image pull is scoped to the "
                "repository, log writes to the task's log group, secrets by ARN.",
            }
        ],
        apply_to_children=True,
    )


def task_outputs(
    stack: cdk.Stack,
    shared: SharedStack,
    network,
    task: ecs.FargateTaskDefinition,
    security_group: ec2.SecurityGroup,
    log_group: logs.ILogGroup,
) -> None:
    """What `jhe-deploy` needs to RunTask a task definition, as stack outputs under one set of
    names, so the script reads the same keys from the migration and the compute stack."""
    private = network.vpc.select_subnets(subnet_type=network.private_subnets.subnet_type)
    cdk.CfnOutput(stack, "ClusterName", value=shared.cluster.cluster_name)
    cdk.CfnOutput(stack, "TaskDefinitionFamily", value=task.family)
    cdk.CfnOutput(stack, "ContainerName", value=CONTAINER_NAME)
    cdk.CfnOutput(stack, "SubnetIds", value=",".join(private.subnet_ids))
    cdk.CfnOutput(stack, "SecurityGroupId", value=security_group.security_group_id)
    cdk.CfnOutput(stack, "LogGroupName", value=log_group.log_group_name)
