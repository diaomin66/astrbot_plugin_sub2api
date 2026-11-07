"""Adapter contract tests use lightweight API stubs, not a running AstrBot."""

import asyncio
import importlib
import logging
import sys
from types import ModuleType, SimpleNamespace

import pytest

from ..bridge import BridgeError
from .support import At, Event, config


@pytest.fixture
def plugin_type(monkeypatch):
    api = ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = logging.getLogger("plugin-tests")
    event = ModuleType("astrbot.api.event")
    event.AstrMessageEvent = Event

    def command(name, alias=None):
        def decorate(function):
            function.command = name
            function.alias = alias or set()
            return function

        return decorate

    event.filter = SimpleNamespace(command=command)
    components = ModuleType("astrbot.api.message_components")
    components.At = At
    star = ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context):
            self.context = context

    star.Star = Star
    star.Context = object
    for name, module in {
        "astrbot": ModuleType("astrbot"),
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.message_components": components,
        "astrbot.api.star": star,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    module_name = __package__.rsplit(".", 1)[0] + ".main"
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    module = importlib.import_module(module_name)
    yield module.Sub2APIPlugin
    sys.modules.pop(module_name, None)


class FakeBridge:
    def __init__(self, data=None, error=None):
        self.data = data
        self.error = error
        self.calls = []
        self.closed = False

    async def request(self, action, payload):
        self.calls.append((action, payload))
        if self.error:
            raise self.error
        return self.data

    async def close(self):
        self.closed = True


def collect(handler, event):
    async def run():
        return [result async for result in handler(event)]

    return asyncio.run(run())


def test_binding_command(plugin_type):
    plugin = plugin_type(None, config())
    plugin.client = FakeBridge(
        {"state": "pending", "verification_code": "ABCD-EFGH", "expires_in_seconds": 600}
    )
    event = Event("绑定 User@example.com")
    results = collect(plugin.bind, event)
    assert event.stopped and results[0].stopped
    assert "确认码" in results[0].text and "User@example.com" not in results[0].text
    assert plugin.client.calls[0][1]["email"] == "User@example.com"


def test_missing_configuration_gives_actionable_reply(plugin_type):
    plugin = plugin_type(None, {})
    result = collect(plugin.checkin, Event())[0]
    assert "尚未正确配置" in result.text


def test_webhook_command_routes_to_same_bridge(plugin_type):
    plugin = plugin_type(None, config())
    plugin.client = FakeBridge({"state": "unbound"})
    event = Event("我的账号")
    event.platform = "qq_official_webhook"
    assert "尚未绑定" in collect(plugin.account, event)[0].text
    assert plugin.client.calls[0][1]["actor"]["provider"] == "qq_official"


def test_other_platforms_and_instances_are_ignored(plugin_type):
    plugin = plugin_type(None, config())
    plugin.client = FakeBridge()
    event = Event()
    event.platform_id = "another-bot"
    assert collect(plugin.checkin, event) == [] and not event.stopped
    event.platform_id = "qq-main"
    event.platform = "aiocqhttp"
    assert collect(plugin.checkin, event) == [] and not plugin.client.calls


def test_disabled_game_never_calls_backend(plugin_type):
    source = config()
    source["robbery"] = {"enabled": False}
    plugin = plugin_type(None, source)
    plugin.client = FakeBridge()
    assert "关闭" in collect(plugin.robbery, Event())[0].text
    assert not plugin.client.calls


def test_group_binding_can_be_disabled(plugin_type):
    source = config()
    source["binding"] = {"allow_group_binding": False}
    plugin = plugin_type(None, source)
    plugin.client = FakeBridge()
    assert "关闭" in collect(plugin.bind, Event("绑定 a@example.com"))[0].text
    assert not plugin.client.calls


def test_robbery_passes_only_authenticated_target(plugin_type):
    plugin = plugin_type(None, config())
    plugin.client = FakeBridge(
        {
            "outcome": "failure",
            "amount": "0.01",
            "balance": "1",
            "robbery_cooldown_remaining_seconds": 3600,
            "target_shield_remaining_seconds": 7200,
        }
    )
    result = collect(plugin.robbery, Event("打劫 @群友", mentions=[{"member_openid": "member-b"}]))[0]
    assert "失败" in result.text
    assert plugin.client.calls[0][1]["target"]["openid"] == "member-b"


def test_invalid_mention_never_calls_backend(plugin_type):
    plugin = plugin_type(None, config())
    plugin.client = FakeBridge()
    assert "真实 @" in collect(plugin.robbery, Event("打劫 @fake"))[0].text
    assert not plugin.client.calls


def test_unknown_error_does_not_leak_token(plugin_type, caplog):
    plugin = plugin_type(None, config())
    plugin.client = FakeBridge(error=RuntimeError("secret-token"))
    assert "secret-token" not in collect(plugin.checkin, Event())[0].text
    assert "secret-token" not in caplog.text


def test_backend_error_translated(plugin_type):
    plugin = plugin_type(None, config())
    plugin.client = FakeBridge(error=BridgeError("NOT_BOUND"))
    assert "没有完成绑定" in collect(plugin.checkin, Event())[0].text


def test_unknown_backend_code_is_not_logged(plugin_type, caplog):
    plugin = plugin_type(None, config())
    plugin.client = FakeBridge(error=BridgeError("SECRET_TOKEN"))
    assert "SECRET_TOKEN" not in collect(plugin.checkin, Event())[0].text
    assert "SECRET_TOKEN" not in caplog.text
    assert "UNKNOWN_BACKEND_ERROR" in caplog.text


def test_status_aliases_help_and_cleanup(plugin_type):
    plugin = plugin_type(None, config())
    plugin.client = FakeBridge()
    assert {"护盾", "冷却", "余额", "绑定状态"} <= plugin.account.alias
    assert "40%" in collect(plugin.help_command, Event())[0].text
    asyncio.run(plugin.terminate())
    assert plugin.client.closed
