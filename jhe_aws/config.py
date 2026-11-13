"""The deployment configuration: one JSON file per environment, flat.

`config/example.json` is the committed shape with every key and its default; a real
environment's file (`config/local/<name>.json`, not committed, since it carries the account
id, zone id and domain names) is a copy with the placeholders replaced and the defaults it
keeps deleted. Loading reads one file, refuses any `<placeholder>` left in it and any key the
models do not know (a misspelled key is an error at synth, not a silently ignored setting),
and validates the rest into `EnvironmentConfig`.

Nothing secret is ever in configuration: secrets are only *named* here and live in Secrets
Manager. Each section is the knobs of one stack: `network`, `data`, `ops`, `compute`,
`monitoring`.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

PLACEHOLDER = re.compile(r"^<[^<>]+>$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- network ------------------------------------------------------------------------------------


class NetworkMode(StrEnum):
    CREATE = "create"
    IMPORT = "import"


class NatMode(StrEnum):
    INSTANCE = "instance"  # one NAT instance (nat_instance_type, t4g.micro by default, about $6 a month)
    GATEWAY = "gateway"  # a managed NAT gateway per AZ: about $32 a month each plus data


class VpcImport(Strict):
    """An existing VPC, by ids: no context lookup, so synth needs no AWS access."""

    vpc_id: str
    availability_zones: list[str] = Field(min_length=1)
    public_subnet_ids: list[str] = Field(min_length=1)
    private_subnet_ids: list[str] = Field(min_length=1)
    isolated_subnet_ids: list[str] = Field(min_length=1)


class NetworkConfig(Strict):
    mode: NetworkMode = NetworkMode.CREATE
    cidr: str = "10.60.0.0/16"
    num_azs: int = Field(default=2, ge=2, le=3)
    nat: NatMode = NatMode.GATEWAY
    nat_instance_type: str = "t4g.micro"  # for nat 'instance'; a nano's 512 MB is too little for dnf
    flow_logs: bool = True
    existing: VpcImport | None = None

    @model_validator(mode="after")
    def _import_needs_ids(self) -> NetworkConfig:
        if self.mode is NetworkMode.IMPORT and self.existing is None:
            raise ValueError("network.mode 'import' needs network.existing")
        return self


# --- data ---------------------------------------------------------------------------------------


class DatabaseEngine(StrEnum):
    RDS_POSTGRES = "rds-postgres"


class DataConfig(Strict):
    engine: DatabaseEngine = DatabaseEngine.RDS_POSTGRES
    postgres_major_version: str = "16"
    instance_class: str = "db.t4g.small"
    multi_az: bool = False
    allocated_storage_gib: int = Field(default=20, ge=20)
    max_allocated_storage_gib: int = Field(default=100, ge=20)
    backup_retention_days: int = Field(default=7, ge=1, le=35)
    deletion_protection: bool = True
    customer_managed_keys: bool = True
    bundle_bucket_name: str | None = None  # None: a generated, unique name
    # The bundle bucket is versioned (an overwrite or delete keeps the previous version); old
    # versions expire after this many days. None keeps every version forever, which means a
    # data-deletion request has to purge versions, not only the current object.
    bundle_noncurrent_version_days: int | None = Field(default=90, ge=1)
    # The JHE image verifies the Postgres server's certificate (DB_SSLMODE=verify-full with the
    # RDS CA bundle, which `build` puts in the image). Needs a JHE with DB_SSLMODE support.
    verify_server_certificate: bool = True
    log_retention_days: int = 30

    @field_validator("instance_class")
    @classmethod
    def _db_prefix(cls, value: str) -> str:
        if not value.startswith("db."):
            raise ValueError("data.instance_class is an RDS class such as 'db.t4g.small'")
        return value


# --- compute ------------------------------------------------------------------------------------


class TlsCertificate(StrEnum):
    SELF_SIGNED = "self-signed"  # issued by the container at start, for that container only
    PROVIDED = "provided"  # files the deployment mounts; a CA of the partner's choosing


class TlsToTasks(Strict):
    enabled: bool = True
    certificate: TlsCertificate = TlsCertificate.SELF_SIGNED


class ServiceSize(Strict):
    cpu: int = 512
    memory: int = 1024
    min_count: int = Field(default=1, ge=1)
    max_count: int = Field(default=3, ge=1)
    target_cpu_percent: int = Field(default=60, ge=10, le=90)
    # Fargate's task-local disk. The web task spools a patient-access pull here before delivery
    # (JHE's default cap is 20 GiB per pull), so it is sized above Fargate's 20 GiB default.
    ephemeral_storage_gib: int = Field(default=50, ge=21, le=200)

    @model_validator(mode="after")
    def _counts(self) -> ServiceSize:
        if self.max_count < self.min_count:
            raise ValueError("max_count is below min_count")
        return self


class McpConfig(Strict):
    enabled: bool = True
    cpu: int = 256
    memory: int = 512
    require_audience: bool = True


class DnsMode(StrEnum):
    ROUTE53 = "route53"  # the zone is in this account: certificate validated and records created
    EXTERNAL = "external"  # the zone is elsewhere: validation record and ALB name are outputs


class DnsConfig(Strict):
    mode: DnsMode = DnsMode.ROUTE53
    hosted_zone_id: str | None = None
    hosted_zone_name: str | None = None
    certificate_arn: str | None = None  # external mode only: skip the request, use this one

    @model_validator(mode="after")
    def _zone(self) -> DnsConfig:
        if self.mode is DnsMode.ROUTE53 and not (self.hosted_zone_id and self.hosted_zone_name):
            raise ValueError("dns.mode 'route53' needs dns.hosted_zone_id and dns.hosted_zone_name")
        return self


class EmailMode(StrEnum):
    NONE = "none"  # v0: invitations are copied from the admin
    SMTP = "smtp"  # a relay: host and port in configuration, credentials in the service secret


class EmailConfig(Strict):
    mode: EmailMode = EmailMode.NONE
    smtp_host: str | None = None
    smtp_port: int = 587

    @model_validator(mode="after")
    def _host(self) -> EmailConfig:
        if self.mode is EmailMode.SMTP and not self.smtp_host:
            raise ValueError("email.mode 'smtp' needs email.smtp_host")
        return self


STACK_ENVIRONMENT = frozenset(
    {
        "SITE_URL",
        "ALLOWED_HOSTS",
        "SECURE_HSTS_SECONDS",
        "DJANGO_LOG_LEVEL",
        "JHE_IGNORE_DOTENV",
        "DB_HOST",
        "DB_PORT",
        "DB_NAME",
        "DB_USER",
        "DB_PASSWORD",
        "SECRET_KEY",
        "PATIENT_ACCESS_SPOOL_DIR",
        "PATIENT_ACCESS_ENCRYPTION_KEYS",
        "JHE_TLS",
        "JHE_TLS_CN",
        "SMTP_HOST",
        "SMTP_PORT",
    }
)
"""What the stacks set on the JHE container themselves; `compute.environment` may not."""


class ComputeConfig(Strict):
    domain_name: str
    mcp_domain_name: str | None = None
    dns: DnsConfig  # required: every deployment decides where its zone is
    web: ServiceSize = ServiceSize()
    mcp: McpConfig = McpConfig()
    tls_to_tasks: TlsToTasks = TlsToTasks()
    hsts_seconds: int = 31536000
    log_level: str = "INFO"
    email: EmailConfig = EmailConfig()
    open_wearables: bool = False  # maps the OW bucket credentials from the service secret
    alb_idle_timeout_seconds: int = Field(default=300, ge=60)
    environment: dict[str, str] = Field(default_factory=dict)  # plain extra variables

    @field_validator("environment")
    @classmethod
    def _not_ours_and_never_debug(cls, env: dict[str, str]) -> dict[str, str]:
        """JHE reads DEBUG as a boolean of the string, so `DEBUG=false` turns debug (and plain-HTTP
        EHR endpoints) on: it is never set here. The variables the stacks set are not overridable."""
        refused = set(env) & (STACK_ENVIRONMENT | {"DEBUG"})
        if refused:
            raise ValueError(f"compute.environment may not set {', '.join(sorted(refused))}")
        return env

    # management command -> EventBridge schedule expression; each run is the web service's task
    # definition with the command overridden, so it has exactly the web service's privileges
    schedules: dict[str, str] = Field(
        default_factory=lambda: {"ow_poll": "rate(15 minutes)", "patient_access_run_jobs": "rate(5 minutes)"}
    )

    @field_validator("schedules")
    @classmethod
    def _expressions(cls, value: dict[str, str]) -> dict[str, str]:
        for command, expression in value.items():
            if not (expression.startswith("rate(") or expression.startswith("cron(")):
                raise ValueError(
                    f"compute.schedules[{command!r}] must be a rate(...) or cron(...) expression"
                )
        return value

    @model_validator(mode="after")
    def _mcp_host(self) -> ComputeConfig:
        if self.mcp.enabled and not self.mcp_domain_name:
            raise ValueError("compute.mcp.enabled needs compute.mcp_domain_name")
        return self


# --- ops, monitoring ----------------------------------------------------------------------------


class OpsConfig(Strict):
    """The operator's task: `manage.py migrate` before each release, `jhe-deploy run`, the first
    admin. Its own task definition and role with the union of what an operator needs (the
    bucket, and later the migrate database credentials), so that the scheduled jobs, which run
    as the web service, never carry any of it."""

    cpu: int = 512
    memory: int = 1024


class AccessConfig(Strict):
    """Who deploys with the `JheDeployer-<env>` policy: IAM user and role names in this account
    that `jhe-deploy access` attaches it to. Empty is fine; an administrator needs no policy."""

    deployer_users: list[str] = []
    deployer_roles: list[str] = []


class MonitoringConfig(Strict):
    enabled: bool = True
    alarm_email: str | None = None
    target_5xx_threshold: int = Field(default=10, ge=1)  # per five minutes


# --- the environment ----------------------------------------------------------------------------


class EnvironmentConfig(Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,19}$")
    account_id: str = Field(pattern=r"^\d{12}$")
    region: str = Field(pattern=r"^[a-z]{2}-[a-z]+-\d$")
    bootstrap_qualifier: str = "hnb659fds"  # the CDK bootstrap qualifier; default unless changed
    termination_protection: bool = True  # network, shared and data stacks
    log_retention_days: int = 30
    network: NetworkConfig = NetworkConfig()
    data: DataConfig = DataConfig()
    ops: OpsConfig = OpsConfig()
    compute: ComputeConfig
    monitoring: MonitoringConfig = MonitoringConfig()
    access: AccessConfig = AccessConfig()

    @property
    def mcp_enabled(self) -> bool:
        return self.compute.mcp.enabled

    def stack_name(self, part: str) -> str:
        return f"jhe-{self.name}-{part}"


class ConfigError(Exception):
    pass


def placeholders(value: Any, path: str = "") -> list[str]:
    """Every `<placeholder>` string left in a configuration tree, by dotted path."""
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in placeholders(v, f"{path}.{k}" if path else k)]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in placeholders(v, f"{path}[{i}]")]
    if isinstance(value, str) and PLACEHOLDER.match(value):
        return [path]
    return []


def load_config(path: Path) -> EnvironmentConfig:
    """The validated configuration in one environment's file."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise ConfigError(f"{path}: {e}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a JSON object")
    if left := placeholders(raw):
        raise ConfigError(f"{path} still has placeholder values at: {', '.join(left)}")
    try:
        return EnvironmentConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"{path}: {e}") from None
