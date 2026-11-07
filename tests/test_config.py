import json
from pathlib import Path

import pytest

from ..config import ConfigError, Settings, amount
from .support import config


def test_defaults_and_no_token_in_repr():
    source = config()
    settings = Settings.from_mapping(source)
    assert settings.policy["checkin"]["reward"] == {"min": "0.10", "max": "0.50"}
    assert settings.policy["robbery"]["cooldown_seconds"] == 3600
    assert settings.policy["checkin"]["enabled"] is False
    assert settings.policy["robbery"]["enabled"] is False
    assert settings.token not in repr(settings)


def test_explicit_economy_settings_are_not_silently_overwritten():
    source = config()
    source["checkin"] = {"enabled": True}
    source["robbery"] = {"enabled": True}
    settings = Settings.from_mapping(source)
    assert settings.policy["checkin"]["enabled"] is True
    assert settings.policy["robbery"]["enabled"] is True


def test_environment_token_wins(monkeypatch):
    source = config()
    source["connection"]["token_env"] = "TEST_BRIDGE_TOKEN"
    monkeypatch.setenv("TEST_BRIDGE_TOKEN", "a" * 40)
    assert Settings.from_mapping(source).token == "a" * 40


@pytest.mark.parametrize(
    "value", [0.1, True, "-1", "NaN", "Infinity", "1e2", "1.0000001", "01", "10000000000", "0.1\n"]
)
def test_reject_invalid_amount(value):
    with pytest.raises(ConfigError):
        amount(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("base_url", "http://public.example"),
        ("base_url", "https://user:password@example.test"),
        ("base_url", "https://example.test?token=secret"),
        ("base_url", "https://example.test:99999"),
        ("base_url", "https://example.test\\evil"),
        ("bridge_token", "short"),
        ("bridge_token", "x" * 40 + "\r\nInjected: header"),
        ("qq_app_id", ""),
        ("platform_id", ""),
        ("platform_id", "qq-main\x7f"),
        ("timeout_seconds", True),
    ],
)
def test_invalid_connection(field, value):
    source = config()
    source["connection"][field] = value
    with pytest.raises(ConfigError):
        Settings.from_mapping(source)


@pytest.mark.parametrize(
    "section,values",
    [
        ("checkin", {"reward_min": "0"}),
        ("checkin", {"reward_min": "1", "reward_max": "0.5"}),
        ("checkin", {"timezone": "Missing/Timezone"}),
        ("checkin", {"enabled": "false"}),
        ("robbery", {"success_probability": "1.1"}),
        ("robbery", {"cooldown_seconds": -1}),
        ("robbery", {"shield_seconds": 2592001}),
        ("binding", {"ttl_seconds": 0}),
    ],
)
def test_invalid_rules(section, values):
    source = config()
    source[section] = values
    with pytest.raises(ConfigError):
        Settings.from_mapping(source)


def test_zero_failure_and_boundary_probabilities():
    source = config()
    for probability in ["0", "1"]:
        source["robbery"] = {
            "success_probability": probability,
            "failure_min": "0",
            "failure_max": "0",
            "cooldown_seconds": 0,
            "shield_seconds": 0,
        }
        assert Settings.from_mapping(source).policy["robbery"]["failure_amount"] == {"min": "0", "max": "0"}


def test_dashboard_defaults_match_runtime():
    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))
    defaults = {key: {k: v["default"] for k, v in group["items"].items()} for key, group in schema.items()}
    defaults["connection"].update(config()["connection"])
    assert Settings.from_mapping(defaults).policy == Settings.from_mapping(config()).policy
    assert schema["connection"]["items"]["bridge_token"]["secret"] is True
