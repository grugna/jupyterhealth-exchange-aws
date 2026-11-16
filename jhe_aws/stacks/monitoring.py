"""Signals to one SNS topic: web 5xx, no healthy web targets, database storage low, an ops task
that exited non-zero, and two on the cron task's log: a periodic job failed, and no job has
succeeded for a while. An email subscription when configured; a partner wires the topic to
whatever they page with.

The cron task runs supercronic, which outlives a failing job and reports it as a log line
(`level=error msg="error running command: exit status N"`), and logs `msg="job succeeded"`
for each run that worked. JHE's crontab runs the patient-access sweep every minute, so a
quarter of an hour without a success means the scheduler is stopped or every job is failing.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_logs as logs
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

        if compute.cron_logs is not None:
            self._alarm(
                "cron-job-failed",
                self._log_count("CronJobFailed", compute.cron_logs, '"error running command"'),
                threshold=1,
                comparison=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                description="A periodic job exited non-zero; `jhe-deploy logs cron` has its output",
            )
            self._alarm(
                "cron-silent",
                self._log_count("CronJobSucceeded", compute.cron_logs, '"job succeeded"'),
                threshold=1,
                comparison=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
                description="No periodic job has succeeded for fifteen minutes",
                evaluation_periods=3,
                treat_missing=cw.TreatMissingData.BREACHING,
            )

        events.Rule(
            self,
            "OpsTaskFailed",
            rule_name=f"jhe-{cfg.name}-task-failed",
            description=f"JHE {cfg.name}: an ops task exited non-zero",
            event_pattern=events.EventPattern(
                source=["aws.ecs"],
                detail_type=["ECS Task State Change"],
                detail={
                    "clusterArn": [shared.cluster.cluster_arn],
                    "lastStatus": ["STOPPED"],
                    "group": [f"family:{ops.task_definition.family}"],
                    "containers": {"exitCode": [{"anything-but": [0]}]},
                },
            ),
            targets=[targets.SnsTopic(self.topic, message=self._task_failed_message())],
        )
        cdk.CfnOutput(self, "AlarmTopicArn", value=self.topic.topic_arn)

    def _task_failed_message(self) -> events.RuleTargetInput:
        """The failed-task event as a few readable lines, by EventBridge's input transformer.
        A transformer copies whole fields and cannot cut the task id out of the task's ARN, so
        the links go to the ops log group and the cluster's tasks, and the id (the last part of
        the ARN, and of the log stream's name) is what to look for there. The command is an
        array in the event, which does not sit safely inside a text template; `startedBy`, which
        `jhe-deploy` sets to the start of the command, stands in for it. The template is one
        string per line: a single string reaches SNS JSON-encoded, its newlines as `\\n`. Each line
        still arrives in its quotation marks, so there are no blank lines to arrive as `""`."""
        cfg = self.cfg
        # Each link ends in a space, so a mail client does not take the line's closing quote into it.
        console = f"https://{cfg.region}.console.aws.amazon.com"
        # The names are written out, not referenced: the text must hold no deploy-time tokens.
        log_group = f"/jhe/{cfg.name}/ops".replace("/", "$252F")  # the Logs console's own encoding
        field = events.EventField.from_path
        return events.RuleTargetInput.from_multiline_text(
            "\n".join(
                [
                    f"JHE {cfg.name}: an ops task exited non-zero.",
                    f"Exit code:  {field('$.detail.containers[0].exitCode')}",
                    f"Stopped:    {field('$.detail.executionStoppedAt')}",
                    f"Reason:     {field('$.detail.stoppedReason')}",
                    f"Started by: {field('$.detail.startedBy')}",
                    f"Image:      {field('$.detail.containers[0].imageDigest')}",
                    f"Task:       {field('$.detail.taskArn')}",
                    "The task's log is the stream ops/jhe/ followed by the task id, in:",
                    f"{console}/cloudwatch/home?region={cfg.region}#logsV2:log-groups/log-group/{log_group} ",
                    "The task itself (ECS shows a stopped task for about an hour):",
                    f"{console}/ecs/v2/clusters/jhe-{cfg.name}/tasks?region={cfg.region} ",
                    "From this repository: jhe-deploy logs ops --since 1h",
                ]
            )
        )

    def _log_count(self, name: str, log_group: logs.ILogGroup, pattern: str) -> cw.Metric:
        """How many lines of `log_group` matched `pattern` in five minutes."""
        return logs.MetricFilter(
            self,
            f"Filter-{name}",
            log_group=log_group,
            filter_pattern=logs.FilterPattern.literal(pattern),
            metric_namespace=f"JHE/{self.cfg.name}",
            metric_name=name,
            metric_value="1",
        ).metric(period=cdk.Duration.minutes(5), statistic="Sum")

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
