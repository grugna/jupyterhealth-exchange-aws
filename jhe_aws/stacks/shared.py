"""What every other stack shares and nothing replaces: the image repositories and the ECS
cluster.

Repositories: immutable tags, scan on push, the last twenty-five builds kept. Only the task
execution roles pull from them and only the deployer policy (the access stack) pushes.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from constructs import Construct

from jhe_aws.config import EnvironmentConfig

from .network import NetworkStack


class SharedStack(cdk.Stack):
    def __init__(
        self, scope: Construct, construct_id: str, cfg: EnvironmentConfig, network: NetworkStack, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.cfg = cfg
        self.web_repository = self._repository("Web", "web")
        self.mcp_repository = self._repository("Mcp", "mcp")
        self.cluster = ecs.Cluster(
            self,
            "Cluster",
            cluster_name=f"jhe-{cfg.name}",
            vpc=network.vpc,
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )
        cdk.CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)
        cdk.CfnOutput(self, "WebRepositoryUri", value=self.web_repository.repository_uri)
        cdk.CfnOutput(self, "McpRepositoryUri", value=self.mcp_repository.repository_uri)

    def _repository(self, construct_id: str, part: str) -> ecr.Repository:
        return ecr.Repository(
            self,
            construct_id,
            repository_name=f"jhe/{self.cfg.name}/{part}",
            image_scan_on_push=True,
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            empty_on_delete=False,
            removal_policy=cdk.RemovalPolicy.RETAIN,
            lifecycle_rules=[
                ecr.LifecycleRule(
                    description="untagged layers: keep the last ten",
                    rule_priority=1,
                    tag_status=ecr.TagStatus.UNTAGGED,
                    max_image_count=10,
                ),
                ecr.LifecycleRule(
                    description="builds: keep the last twenty-five",
                    rule_priority=2,
                    tag_status=ecr.TagStatus.ANY,
                    max_image_count=25,
                ),
            ],
        )
