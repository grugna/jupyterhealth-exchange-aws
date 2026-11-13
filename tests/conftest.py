import os

import pytest

from jhe_aws.config import EnvironmentConfig

os.environ.setdefault("JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION", "1")


def sample_config(**overrides) -> EnvironmentConfig:
    base = {
        "name": "test",
        "account_id": "123456789012",
        "region": "us-east-2",
        "compute": {
            "domain_name": "jhe.example.org",
            "mcp_domain_name": "mcp.jhe.example.org",
            "dns": {
                "mode": "route53",
                "hosted_zone_id": "Z0123456789ABCDEFGHIJ",
                "hosted_zone_name": "jhe.example.org",
            },
        },
        "monitoring": {"alarm_email": "ops@example.org"},
    }
    return EnvironmentConfig.model_validate(_merge(base, overrides))


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@pytest.fixture
def config() -> EnvironmentConfig:
    return sample_config()
