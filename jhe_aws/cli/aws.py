"""The AWS calls the deploy script makes, behind one small class so the rest of the script is
plain logic and the tests can fake it. Nothing here is clever: each method is one API call or
one wait loop.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any

import boto3


@dataclass(frozen=True)
class TaskOutcome:
    task_arn: str
    exit_code: int | None
    stopped_reason: str

    @property
    def task_id(self) -> str:
        return self.task_arn.rsplit("/", 1)[-1]

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class Aws:
    def __init__(self, region: str, profile: str | None = None) -> None:
        self.region = region
        self.session = boto3.Session(region_name=region, profile_name=profile)

    def client(self, service: str) -> Any:
        return self.session.client(service)

    # --- identity, stacks ----------------------------------------------------------------------

    def account_id(self) -> str:
        return self.client("sts").get_caller_identity()["Account"]

    def stack_status(self, stack_name: str) -> str | None:
        try:
            return self.client("cloudformation").describe_stacks(StackName=stack_name)["Stacks"][0][
                "StackStatus"
            ]
        except self.client("cloudformation").exceptions.ClientError as e:
            if "does not exist" in str(e):
                return None
            raise

    def stack_outputs(self, stack_name: str) -> dict[str, str]:
        stacks = self.client("cloudformation").describe_stacks(StackName=stack_name)["Stacks"]
        return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}

    # --- ecr ---------------------------------------------------------------------------------

    def ecr_login(self) -> tuple[str, str, str]:
        """(username, password, registry host) for `docker login`."""
        data = self.client("ecr").get_authorization_token()["authorizationData"][0]
        user, password = base64.b64decode(data["authorizationToken"]).decode().split(":", 1)
        registry = data["proxyEndpoint"].removeprefix("https://")
        return user, password, registry

    def image_digest(self, repository_name: str, tag: str) -> str:
        images = self.client("ecr").describe_images(
            repositoryName=repository_name, imageIds=[{"imageTag": tag}]
        )
        return images["imageDetails"][0]["imageDigest"]

    def image_tags(self, repository_name: str, digest: str) -> list[str]:
        """The tags on an image, by digest: the build names each push `<sha>[-dirty]-<timestamp>`,
        so this is the digest's commit and build time. Empty when the image is gone or untagged."""
        try:
            images = self.client("ecr").describe_images(
                repositoryName=repository_name, imageIds=[{"imageDigest": digest}]
            )
        except self.client("ecr").exceptions.ImageNotFoundException:
            return []
        return images["imageDetails"][0].get("imageTags", [])

    # --- ecs ---------------------------------------------------------------------------------

    def run_task(
        self,
        *,
        cluster: str,
        family: str,
        subnets: list[str],
        security_group: str,
        container: str,
        command: list[str] | None,
        started_by: str,
    ) -> str:
        overrides: dict[str, Any] = {}
        if command is not None:
            overrides = {"containerOverrides": [{"name": container, "command": command}]}
        response = self.client("ecs").run_task(
            cluster=cluster,
            taskDefinition=family,
            launchType="FARGATE",
            platformVersion="LATEST",
            startedBy=started_by[:36],
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": subnets,
                    "securityGroups": [security_group],
                    "assignPublicIp": "DISABLED",
                }
            },
            overrides=overrides,
            propagateTags="TASK_DEFINITION",
        )
        if response.get("failures"):
            raise RuntimeError(f"RunTask failed: {response['failures']}")
        return response["tasks"][0]["taskArn"]

    def wait_task(self, cluster: str, task_arn: str, container: str, poll_seconds: float = 5) -> TaskOutcome:
        ecs = self.client("ecs")
        while True:
            task = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])["tasks"][0]
            if task["lastStatus"] == "STOPPED":
                exit_code = next(
                    (c.get("exitCode") for c in task["containers"] if c["name"] == container), None
                )
                return TaskOutcome(task_arn, exit_code, task.get("stoppedReason", ""))
            time.sleep(poll_seconds)

    def task_definition_image(self, family: str) -> str:
        definition = self.client("ecs").describe_task_definition(taskDefinition=family)["taskDefinition"]
        return definition["containerDefinitions"][0]["image"]

    def restart_service(self, cluster: str, service: str) -> bool:
        """A new deployment of the service with the same task definition: fresh tasks, which
        re-read their secrets and drop any in-process cache. False when the service is absent."""
        ecs = self.client("ecs")
        found = ecs.describe_services(cluster=cluster, services=[service])["services"]
        if not found or found[0]["status"] != "ACTIVE":
            return False
        ecs.update_service(cluster=cluster, service=service, forceNewDeployment=True)
        return True

    # --- logs --------------------------------------------------------------------------------

    def log_events(self, group: str, since_seconds: int, pattern: str | None) -> list[tuple[int, str]]:
        """(timestamp ms, message) across every stream of the group since `since_seconds` ago,
        oldest first; empty when the group does not exist yet."""
        logs = self.client("logs")
        start = int((time.time() - since_seconds) * 1000)
        kwargs: dict = {"logGroupName": group, "startTime": start, "interleaved": True}
        if pattern:
            kwargs["filterPattern"] = pattern
        events: list[tuple[int, str]] = []
        try:
            for page in logs.get_paginator("filter_log_events").paginate(**kwargs):
                events.extend((e["timestamp"], e["message"]) for e in page["events"])
        except logs.exceptions.ResourceNotFoundException:
            return []
        return events

    def log_lines(self, group: str, stream: str) -> list[str]:
        logs = self.client("logs")
        lines: list[str] = []
        token = None
        while True:
            kwargs = {"logGroupName": group, "logStreamName": stream, "startFromHead": True}
            if token:
                kwargs["nextToken"] = token
            try:
                page = logs.get_log_events(**kwargs)
            except logs.exceptions.ResourceNotFoundException:
                return lines
            lines.extend(e["message"] for e in page["events"])
            if page.get("nextForwardToken") in (None, token):
                return lines
            token = page["nextForwardToken"]

    # --- secrets -----------------------------------------------------------------------------

    def get_secret_json(self, name: str, version_stage: str = "AWSCURRENT") -> dict[str, str]:
        """The secret's fields; `version_stage="AWSPREVIOUS"` reads the version before the current
        one, which Secrets Manager keeps until the next change."""
        value = self.client("secretsmanager").get_secret_value(SecretId=name, VersionStage=version_stage)
        return json.loads(value["SecretString"])

    def secret_exists(self, name: str) -> bool:
        client = self.client("secretsmanager")
        try:
            client.describe_secret(SecretId=name)
        except client.exceptions.ResourceNotFoundException:
            return False
        return True

    def create_secret_json(
        self, name: str, value: dict[str, str], kms_key: str | None, tags: dict[str, str]
    ) -> None:
        kwargs: dict = {
            "Name": name,
            "SecretString": json.dumps(value),
            "Tags": [{"Key": k, "Value": v} for k, v in tags.items()],
        }
        if kms_key:
            kwargs["KmsKeyId"] = kms_key
        self.client("secretsmanager").create_secret(**kwargs)

    def put_secret_json(self, name: str, value: dict[str, str]) -> None:
        self.client("secretsmanager").put_secret_value(SecretId=name, SecretString=json.dumps(value))

    def put_secret_string(self, name: str, value: str) -> None:
        self.client("secretsmanager").put_secret_value(SecretId=name, SecretString=value)

    # --- rds ---------------------------------------------------------------------------------

    def db_status(self, identifier: str) -> str:
        return self.client("rds").describe_db_instances(DBInstanceIdentifier=identifier)["DBInstances"][0][
            "DBInstanceStatus"
        ]

    def stop_db(self, identifier: str) -> None:
        self.client("rds").stop_db_instance(DBInstanceIdentifier=identifier)

    def start_db(self, identifier: str) -> None:
        self.client("rds").start_db_instance(DBInstanceIdentifier=identifier)

    def wait_db(self, identifier: str, status: str, poll_seconds: float = 15) -> None:
        while self.db_status(identifier) != status:
            time.sleep(poll_seconds)

    # --- ssm ---------------------------------------------------------------------------------

    def get_parameter(self, name: str) -> str | None:
        ssm = self.client("ssm")
        try:
            return ssm.get_parameter(Name=name)["Parameter"]["Value"]
        except ssm.exceptions.ParameterNotFound:
            return None

    def put_parameter(self, name: str, value: str) -> None:
        self.client("ssm").put_parameter(Name=name, Value=value, Type="String", Overwrite=True)
