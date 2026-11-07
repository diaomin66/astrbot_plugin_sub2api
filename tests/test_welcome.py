import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ..config import ConfigError, Settings
from ..presentation import gift_line
from ..welcome import MEMBER_INTENT, WelcomeListener, WelcomeSender, validate_template
from .support import config


def settings(enabled=True, template="欢迎 {at}！站点 {site_url}"):
    source = config()
    source["welcome"] = {"enabled": enabled, "template": template}
    return Settings.from_mapping(source)


def event(**changes):
    payload = {
        "op": 0,
        "t": "GROUP_MEMBER_ADD",
        "id": "event-1",
        "d": {"group_openid": "group-a", "member_openid": "member-a", "timestamp": int(time.time())},
    }
    payload.update(changes)
    return payload


@pytest.mark.parametrize(
    "template", ["", "{at.__class__}", "{at[0]}", "{at!r}", "{site_url:>10000}", "{name}", "{", "x" * 1601]
)
def test_template_rejects_unsafe_fields(template):
    with pytest.raises(ConfigError):
        validate_template(template)


def test_template_literals():
    assert validate_template("欢迎 {{新人}} {at}") == "欢迎 {{新人}} {at}"


def test_custom_welcome_preserved_in_markdown(tmp_path):
    async def run():
        template = "欢迎 {at} 加入Orbitix AI 交流群！\n站点：{site_url}\n发送 /额度帮助"
        api = SimpleNamespace(post_group_message=AsyncMock())
        sender = WelcomeSender(settings(template=template), tmp_path / "w.db", logging.getLogger())
        assert await sender.handle(event(), api)
        kwargs = api.post_group_message.call_args.kwargs
        assert kwargs["msg_type"] == 2
        assert kwargs["markdown"] == {
            "content": (
                '欢迎 <qqbot-at-user id="member-a" /> 加入Orbitix AI 交流群！\n'
                "站点：https://sub2api.example.test\n发送 /额度帮助"
            )
        }
        assert "content" not in kwargs
        assert kwargs["event_id"] == "event-1" and kwargs["msg_seq"] == 1

    asyncio.run(run())


@pytest.mark.parametrize("failure", [TimeoutError, RuntimeError])
def test_markdown_failure_never_leaks_tag_through_plain_fallback(tmp_path, failure):
    async def run():
        api = SimpleNamespace(post_group_message=AsyncMock(side_effect=failure))
        path = tmp_path / "w.db"
        sender = WelcomeSender(settings(), path, logging.getLogger())
        payload = event()
        assert not await sender.handle(payload, api)
        reloaded = WelcomeSender(settings(), path, logging.getLogger())
        assert not await reloaded.handle(payload, api)
        api.post_group_message.assert_awaited_once()
        kwargs = api.post_group_message.call_args.kwargs
        assert kwargs["msg_type"] == 2 and "content" not in kwargs

    asyncio.run(run())


def test_welcome_true_event_persistent_dedup(tmp_path):
    async def run():
        path = tmp_path / "welcome.sqlite3"
        api = SimpleNamespace(post_group_message=AsyncMock())
        payload = event()
        sender = WelcomeSender(settings(), path, logging.getLogger())
        results = await asyncio.gather(*(sender.handle(payload, api) for _ in range(5)))
        assert results.count(True) == 1
        sender = WelcomeSender(settings(), path, logging.getLogger())
        assert not await sender.handle(payload, api)
        api.post_group_message.assert_awaited_once()
        kwargs = api.post_group_message.call_args.kwargs
        assert kwargs["group_openid"] == "group-a"
        assert kwargs["msg_type"] == 2
        assert "content" not in kwargs and "ark" not in kwargs
        assert '<qqbot-at-user id="member-a" />' in kwargs["markdown"]["content"]
        assert "https://sub2api.example.test" in kwargs["markdown"]["content"]
        assert kwargs["event_id"] == "event-1"

    asyncio.run(run())


@pytest.mark.parametrize(
    "kind", ["GROUP_ADD_ROBOT", "GUILD_MEMBER_ADD", "GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"]
)
def test_other_events_never_welcome(tmp_path, kind):
    sender = WelcomeSender(settings(), tmp_path / "w.db", logging.getLogger())
    api = SimpleNamespace(post_group_message=AsyncMock())
    assert not asyncio.run(sender.handle(event(t=kind), api))
    api.post_group_message.assert_not_awaited()


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"timestamp": True},
        {"group_openid": "g", "member_openid": "<bad>", "timestamp": int(time.time())},
        {"group_openid": "g", "member_openid": "m", "timestamp": 1},
    ],
)
def test_invalid_payload(tmp_path, data):
    sender = WelcomeSender(settings(), tmp_path / "w.db", logging.getLogger())
    api = SimpleNamespace(post_group_message=AsyncMock())
    assert not asyncio.run(sender.handle(event(d=data), api))
    api.post_group_message.assert_not_awaited()


def test_disabled_and_timeout_no_retry(tmp_path):
    async def run():
        path = tmp_path / "w.db"
        api = SimpleNamespace(post_group_message=AsyncMock(side_effect=TimeoutError))
        sender = WelcomeSender(settings(False), path, logging.getLogger())
        payload = event()
        assert not await sender.handle(payload, api)
        api.post_group_message.assert_not_awaited()
        sender = WelcomeSender(settings(), path, logging.getLogger())
        assert not await sender.handle(payload, api)
        assert not await sender.handle(payload, api)
        api.post_group_message.assert_awaited_once()

    asyncio.run(run())


def test_hook_scoped_restored_and_intent_reidentification(tmp_path):
    async def run():
        def previous(value):
            return None

        connection = SimpleNamespace(parser={"group_member_add": previous}, _session_list=[])
        websocket = SimpleNamespace(_session={"intent": 1 << 25}, close=AsyncMock())
        client = SimpleNamespace(
            intents=1 << 25,
            _connection=connection,
            _active_websockets=[websocket],
            api=SimpleNamespace(post_group_message=AsyncMock()),
        )
        sender = WelcomeSender(settings(), tmp_path / "w.db", logging.getLogger())
        listener = WelcomeListener(None, settings(), sender, logging.getLogger())
        await listener.attach(client)
        await listener.attach(client)
        assert client.intents & MEMBER_INTENT
        websocket.close.assert_awaited_once()
        connection.parser["group_member_add"](event())
        await asyncio.sleep(0.01)
        client.api.post_group_message.assert_awaited_once()
        await listener.stop()
        assert connection.parser["group_member_add"] is previous
        assert client.intents == 1 << 25

    asyncio.run(run())


def test_gift_text_distinguishes_pending_success():
    assert "$2.00 已到账" in gift_line({"binding_gift": {"state": "credited", "amount": "2.000000"}})
    assert "发放处理中" in gift_line({"binding_gift": {"state": "processing", "amount": "2"}})
    assert "尚未确认" in gift_line({"binding_gift": {"state": "review_required", "amount": "2"}})
    assert gift_line({}) == ""


def test_rejoin_new_timestamp_and_missing_event_id(tmp_path):
    async def run():
        api = SimpleNamespace(post_group_message=AsyncMock())
        sender = WelcomeSender(settings(), tmp_path / "w.db", logging.getLogger())
        first = event()
        first.pop("id")
        assert await sender.handle(first, api)
        second = event()
        second["d"]["timestamp"] += 1
        assert await sender.handle(second, api)
        assert api.post_group_message.await_count == 2
        assert "event_id" not in api.post_group_message.call_args_list[0].kwargs

    asyncio.run(run())


def test_gift_invalid_response_fails_closed():
    from ..bridge import BridgeError

    for gift in [{"state": "credited", "amount": "200"}, {"state": "anything", "amount": "2"}, "bad"]:
        with pytest.raises(BridgeError):
            gift_line({"binding_gift": gift})
