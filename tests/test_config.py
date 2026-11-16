import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from jhe_aws.config import ConfigError, EnvironmentConfig, load_config, placeholders
from jhe_aws.images import PLACEHOLDER_DIGEST, Images

from .conftest import sample_config

EXAMPLE = Path(__file__).parent.parent / "config" / "example.json"


def filled(**overrides) -> dict:
    data = {
        "name": "sandbox",
        "account_id": "123456789012",
        "region": "us-east-2",
        "log_retention_days": 14,
        "compute": {
            "domain_name": "jhe.example.org",
            "mcp_domain_name": "mcp.jhe.example.org",
            "dns": {"mode": "route53", "hosted_zone_id": "Z1", "hosted_zone_name": "jhe.example.org"},
        },
    }
    data.update(overrides)
    return data


def write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "sandbox.json"
    path.write_text(json.dumps(data))
    return path


def test_example_file_is_the_committed_shape_and_only_synths():
    with pytest.raises(ConfigError, match="placeholder values at: ") as e:
        load_config(EXAMPLE)
    for path in (
        "account_id",
        "region",
        "compute.domain_name",
        "compute.dns.hosted_zone_id",
        "monitoring.alarm_email",
    ):
        assert path in str(e.value)


def test_load_one_flat_file(tmp_path):
    cfg = load_config(write(tmp_path, filled()))
    assert cfg.name == "sandbox"
    assert cfg.log_retention_days == 14
    assert cfg.stack_name("data") == "jhe-sandbox-data"
    assert cfg.compute.tls_to_tasks.enabled is True
    assert cfg.compute.cron.enabled is True
    assert cfg.ops.cpu == 512


def test_unknown_keys_and_bad_files_are_errors(tmp_path):
    with pytest.raises(ConfigError, match="extra_forbidden|Extra inputs"):
        load_config(
            write(
                tmp_path, filled(compute={"domain_name": "x", "dns": {"mode": "external"}, "tls_to_task": {}})
            )
        )
    with pytest.raises(ConfigError, match="shared"):  # the old shared/environments shape is an unknown key
        load_config(write(tmp_path, {"shared": {}, "environments": {"sandbox": filled()}}))
    with pytest.raises(ConfigError, match="expected a JSON object"):
        load_config(write(tmp_path, []))
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.json")


def test_placeholders_are_found_by_path():
    assert placeholders({"a": "<x>", "b": {"c": "<y>", "d": "ok"}, "e": ["<z>"]}) == ["a", "b.c", "e[0]"]


def test_cross_field_rules():
    with pytest.raises(ValueError, match="needs network.existing"):
        sample_config(network={"mode": "import"})
    with pytest.raises(ValueError, match="needs compute.mcp_domain_name"):
        sample_config(compute={"mcp_domain_name": None})
    with pytest.raises(ValueError, match="needs dns.hosted_zone_id"):
        sample_config(compute={"dns": {"mode": "route53", "hosted_zone_id": None}})
    with pytest.raises(ValueError, match="schedules"):
        sample_config(compute={"schedules": {"ow_poll": "rate(15 minutes)"}})  # the crontab is JHE's
    with pytest.raises(ValueError, match="max_count is below min_count"):
        sample_config(compute={"web": {"min_count": 3, "max_count": 1}})
    with pytest.raises(ValueError, match="RDS class"):
        sample_config(data={"instance_class": "t4g.small"})


def test_config_is_frozen(config: EnvironmentConfig):
    with pytest.raises(ValidationError):
        config.name = "other"  # type: ignore[misc]


def test_images_validate_digests():
    assert Images().placeholders == ["web", "mcp"]
    assert Images(web="sha256:" + "a" * 64).placeholders == ["mcp"]
    with pytest.raises(ValueError, match="web_image must be an image digest"):
        Images(web="latest")
    assert Images(web=PLACEHOLDER_DIGEST).web == PLACEHOLDER_DIGEST


def test_example_is_every_key_at_its_default():
    """config/example.json is the committed statement of the defaults: every model field appears,
    and every value that is not a placeholder equals the model's default."""
    import enum
    import json
    from pathlib import Path

    from pydantic import BaseModel
    from pydantic_core import PydanticUndefined

    from jhe_aws.config import EnvironmentConfig

    example = json.loads(Path("config/example.json").read_text())

    def plain(value):
        if isinstance(value, enum.Enum):
            return value.value
        if isinstance(value, BaseModel):
            return {k: plain(v) for k, v in value.__dict__.items()}
        return value

    def walk(model_cls: type[BaseModel], raw: dict, path: str) -> None:
        fields = model_cls.model_fields
        assert set(raw) == set(fields), f"{path}: keys differ from the model's: {set(raw) ^ set(fields)}"
        for name, field in fields.items():
            value = raw[name]
            if isinstance(value, str) and value.startswith("<"):
                continue  # a placeholder: no default to compare with
            default = field.default_factory() if field.default_factory else field.default
            if default is PydanticUndefined:
                # a required section (compute, compute.dns): walk its fields by annotation
                nested = field.annotation
                assert isinstance(nested, type) and issubclass(nested, BaseModel), (
                    f"{path}.{name}: required field without a placeholder"
                )
                walk(nested, value, f"{path}.{name}")
            elif isinstance(default, BaseModel):
                walk(type(default), value, f"{path}.{name}")
            else:
                assert value == plain(default), (
                    f"{path}.{name}: {value!r} is not the default {plain(default)!r}"
                )

    walk(EnvironmentConfig, example, "config")


def test_environment_refuses_debug_and_the_stacks_own_variables():
    for key in ("DEBUG", "SITE_URL", "DB_PASSWORD"):
        with pytest.raises(ValidationError) as e:
            sample_config(compute={"environment": {key: "x"}})
        assert key in str(e.value)
    assert sample_config(compute={"environment": {"OW_S3_BUCKET": "b"}}).compute.environment == {
        "OW_S3_BUCKET": "b"
    }
