"""Four signals to one SNS topic: web 5xx, no healthy web targets, database storage low, a
ops or scheduled task that exited non-zero. An email subscription when configured; a partner
wires the topic to whatever they page with.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from constructs import Construct

from jhe_aws.config import EnvironmentConfig

from .compute import ComputeStack
from .data import DataStack
from .ops import OpsStack
from .shared import SharedStack

GIB = 1024**3


class MonitoringStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        cfg: EnvironmentConfig,
        shared: SharedStack,
        data: DataStack,
        ops: OpsStack,
        compute: ComputeStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.cfg = cfg
        self.topic = sns.Topic(
            self,
            "Alarms",
            topic_name=f"jhe-{cfg.name}-alarms",
            display_name=f"JHE {cfg.name} alarms",
            master_key=data.key,
            enforce_ssl=True,
        )
        if cfg.monitoring.alarm_email:
            self.topic.add_subscription(subscriptions.EmailSubscription(cfg.monitoring.alarm_email))
        self.action = cw_actions.SnsAction(self.topic)

        self._alarm(
            "web-5xx",
            compute.web_target_group.metrics.http_code_target(
                elbv2.HttpCodeTarget.TARGET_5XX_COUNT, period=cdk.Duration.minutes(5), statistic="Sum"
            ),
            threshold=cfg.monitoring.target_5xx_threshold,
            comparison=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            description="The web service answered 5xx more than the threshold allows in five minutes",
        )
        self._alarm(
            "web-no-healthy-targets",
            compute.web_target_group.metrics.healthy_host_count(
                period=cdk.Duration.minutes(1), statistic="Minimum"
            ),
            threshold=1,
            comparison=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
            description="No healthy web target behind the load balancer",
            evaluation_periods=3,
            treat_missing=cw.TreatMissingData.BREACHING,
        )
        self._alarm(
            "database-storage-low",
            data.database.metric_free_storage_space(period=cdk.Duration.minutes(5), statistic="Minimum"),
            threshold=2 * GIB,
            comparison=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
            description="Under 2 GiB of free database storage",
        )

        events.Rule(
            self,
            "OpsTaskFailed",
            rule_name=f"jhe-{cfg.name}-task-failed",
            description=f"JHE {cfg.name}: an ops or scheduled task exited non-zero",
            event_pattern=events.EventPattern(
                source=["aws.ecs"],
                detail_type=["ECS Task State Change"],
                detail={
                    "clusterArn": [shared.cluster.cluster_arn],
                    "lastStatus": ["STOPPED"],
                    "group": [
                        f"family:{ops.task_definition.family}",
                        f"family:{compute.web_task.family}",
                    ],
                    "containers": {"exitCode": [{"anything-but": [0]}]},
                },
            ),
            targets=[targets.SnsTopic(self.topic)],
        )
        cdk.CfnOutput(self, "AlarmTopicArn", value=self.topic.topic_arn)

    def _alarm(
        self,
        name: str,
        metric: cw.IMetric,
        *,
        threshold: float,
        comparison: cw.ComparisonOperator,
        description: str,
        evaluation_periods: int = 1,
        treat_missing: cw.TreatMissingData = cw.TreatMissingData.NOT_BREACHING,
    ) -> cw.Alarm:
        alarm = cw.Alarm(
            self,
            f"Alarm-{name}",
            alarm_name=f"jhe-{self.cfg.name}-{name}",
            alarm_description=description,
            metric=metric,
            threshold=threshold,
            comparison_operator=comparison,
            evaluation_periods=evaluation_periods,
            treat_missing_data=treat_missing,
        )
        alarm.add_alarm_action(self.action)
        alarm.add_ok_action(self.action)
        return alarm
