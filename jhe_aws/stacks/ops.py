"""The operator's task definition: the JHE image, run once at a time by a person or the deploy.

`jhe-deploy deploy` deploys this stack with the new digest and runs the task's default
command, `manage.py migrate`, before the compute stack rolls, so a migration that fails stops
the deploy with the old image still serving. `jhe-deploy run` overrides the command for any
other management command, and `admin create` runs `createsuperuser` with the password from the
bootstrap secret, which only this task reads.

The task role holds the union of what an operator's command might need: the bundle bucket now,
the migrate database credentials when that follow-on lands. Nothing automated uses this task:
the periodic jobs run as the cron task with the web service's roles, so they never carry any of it.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_logs as logs
from constructs import Construct

from jhe_aws.config import EnvironmentConfig
from jhe_aws.images import Images

from .data import DataStack
from .jhe_container import (
    CONTAINER_NAME,
    by_arn,
    execution_role,
    jhe_task_definition,
    jhe_task_role,
    task_outputs,
    task_security_group,
)
from .network import NetworkStack, _retention
from .shared import SharedStack

MIGRATE = ["python", "manage.py", "migrate", "--no-input"]


class OpsStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        cfg: EnvironmentConfig,
        images: Images,
        network: NetworkStack,
        shared: SharedStack,
        data: DataStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.cfg = cfg
        self.log_group = logs.LogGroup(
            self,
            "OpsLogs",
            log_group_name=f"/jhe/{cfg.name}/ops",
            retention=_retention(cfg.log_retention_days),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        self.security_group = task_security_group(
            self, "OpsSecurityGroup", cfg, network.vpc, data, database=True, name="ops task"
        )
        self.task_role = jhe_task_role(self, "OpsTaskRole", cfg, data, role_name=f"jhe-{cfg.name}-ops-task")
        self.execution_role = execution_role(
            self, "OpsExecutionRole", cfg, role_name=f"jhe-{cfg.name}-ops-execution"
        )
        self.task_definition = jhe_task_definition(
            self,
            "OpsTask",
            cfg,
            shared,
            data,
            family=f"jhe-{cfg.name}-ops",
            image_digest=images.web,
            cpu=cfg.ops.cpu,
            memory=cfg.ops.memory,
            task_role=self.task_role,
            execution_role=self.execution_role,
            log_group=self.log_group,
            stream_prefix="ops",
            command=MIGRATE,
            extra_secrets={
                "DJANGO_SUPERUSER_PASSWORD": ecs.Secret.from_secrets_manager(
                    by_arn(self, "BootstrapAdminSecret", data.bootstrap_admin_secret)
                )
            },
        )
        self.container_name = CONTAINER_NAME
        task_outputs(self, shared, network, self.task_definition, self.security_group, self.log_group)
        cdk.CfnOutput(self, "WebImageDigest", value=images.web)
