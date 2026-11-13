"""Who may deploy: the `JheDeployer-<env>` managed policy, and its attachment to the users and
roles the configuration names.

This is an administrator's decision made once, like the CDK bootstrap, so it is its own stack
with its own command (`jhe-deploy access`) and `deploy` never touches it. It depends on no
other stack: every statement names its resources by ARN pattern, so it can be created before
anything else exists and left alone afterwards. An administrator needs none of it; the stack is
for giving a narrower identity exactly what `jhe-deploy` uses.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from cdk_nag import NagSuppressions
from constructs import Construct

from jhe_aws.config import EnvironmentConfig

TOOLKIT_ROLES = ("deploy-role", "file-publishing-role", "image-publishing-role", "lookup-role")


class AccessStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, cfg: EnvironmentConfig, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.cfg = cfg
        self.deployer_policy = self._deployer_policy()
        cdk.CfnOutput(self, "DeployerPolicyArn", value=self.deployer_policy.managed_policy_arn)

    def _deployer_policy(self) -> iam.ManagedPolicy:
        """What `jhe-deploy` needs after `cdk bootstrap`, statement by statement. CDK itself
        deploys by assuming the bootstrap toolkit roles, so the operator's own identity needs
        only `sts:AssumeRole` on them; the rest is the script's direct calls."""
        cfg, account, region = self.cfg, self.account, self.region
        env = cfg.name
        toolkit = [
            f"arn:aws:iam::{account}:role/cdk-{cfg.bootstrap_qualifier}-{role}-{account}-{region}"
            for role in TOOLKIT_ROLES
        ]
        statements = [
            iam.PolicyStatement(sid="CdkToolkitRoles", actions=["sts:AssumeRole"], resources=toolkit),
            iam.PolicyStatement(
                sid="CdkBootstrapVersion",
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:aws:ssm:{region}:{account}:parameter/cdk-bootstrap/{cfg.bootstrap_qualifier}/version"
                ],
            ),
            iam.PolicyStatement(sid="EcrLogin", actions=["ecr:GetAuthorizationToken"], resources=["*"]),
            iam.PolicyStatement(
                sid="EcrPush",
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:BatchGetImage",
                    "ecr:CompleteLayerUpload",
                    "ecr:DescribeImages",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:InitiateLayerUpload",
                    "ecr:PutImage",
                    "ecr:UploadLayerPart",
                ],
                resources=[f"arn:aws:ecr:{region}:{account}:repository/jhe/{env}/*"],
            ),
            iam.PolicyStatement(
                sid="RunTasks",
                actions=["ecs:RunTask"],
                resources=[f"arn:aws:ecs:{region}:{account}:task-definition/jhe-{env}-ops:*"],
                conditions={
                    "ArnEquals": {"ecs:cluster": f"arn:aws:ecs:{region}:{account}:cluster/jhe-{env}"}
                },
            ),
            iam.PolicyStatement(
                sid="WatchTasks",
                actions=[
                    "ecs:DescribeTasks",
                    "ecs:DescribeTaskDefinition",
                    "ecs:DescribeServices",
                    "ecs:ListTasks",
                ],
                resources=["*"],
            ),
            iam.PolicyStatement(
                sid="PassTaskRoles",
                actions=["iam:PassRole"],
                resources=[f"arn:aws:iam::{account}:role/jhe-{env}-ops-*"],
                conditions={"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}},
            ),
            iam.PolicyStatement(
                sid="ReadTaskLogs",
                actions=["logs:GetLogEvents", "logs:FilterLogEvents", "logs:DescribeLogStreams"],
                resources=[f"arn:aws:logs:{region}:{account}:log-group:/jhe/{env}/*"],
            ),
            iam.PolicyStatement(
                sid="WriteSecrets",
                actions=[
                    "secretsmanager:CreateSecret",
                    "secretsmanager:TagResource",
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:PutSecretValue",
                ],
                resources=[f"arn:aws:secretsmanager:{region}:{account}:secret:jhe/{env}/*"],
            ),
            iam.PolicyStatement(
                sid="UseDataKey",
                actions=["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                resources=[f"arn:aws:kms:{region}:{account}:key/*"],
                conditions={"ForAnyValue:StringEquals": {"kms:ResourceAliases": f"alias/jhe-{env}-data"}},
            ),
            iam.PolicyStatement(
                sid="RestartServices",
                actions=["ecs:UpdateService"],
                resources=[f"arn:aws:ecs:{region}:{account}:service/jhe-{env}/jhe-{env}-*"],
            ),
            iam.PolicyStatement(
                sid="StopStartDatabase",
                actions=["rds:StopDBInstance", "rds:StartDBInstance", "rds:DescribeDBInstances"],
                resources=[f"arn:aws:rds:{region}:{account}:db:jhe-{env}-db"],
            ),
            iam.PolicyStatement(
                sid="RememberImages",
                actions=["ssm:GetParameter", "ssm:PutParameter"],
                resources=[f"arn:aws:ssm:{region}:{account}:parameter/jhe/{env}/*"],
            ),
            iam.PolicyStatement(
                sid="ReadStacks",
                actions=["cloudformation:DescribeStacks"],
                resources=[f"arn:aws:cloudformation:{region}:{account}:stack/jhe-{env}-*/*"],
            ),
        ]
        policy = iam.ManagedPolicy(
            self,
            "DeployerPolicy",
            managed_policy_name=f"JheDeployer-{env}",
            description=f"What jhe-deploy needs for the {env} environment after cdk bootstrap",
            statements=statements,
            users=[
                iam.User.from_user_name(self, f"User{i}", name)
                for i, name in enumerate(cfg.access.deployer_users)
            ],
            roles=[
                iam.Role.from_role_name(self, f"Role{i}", name)
                for i, name in enumerate(cfg.access.deployer_roles)
            ],
        )
        NagSuppressions.add_resource_suppressions(
            policy,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "ecr:GetAuthorizationToken and ecs:Describe* take no resource; the other "
                    "wildcards are the two repositories, the revisions of one task definition family, "
                    "roles and secrets under one environment's prefix, and a KMS key selected by its alias.",
                }
            ],
        )
        return policy
