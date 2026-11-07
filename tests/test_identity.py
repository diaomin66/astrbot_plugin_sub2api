from types import SimpleNamespace

import pytest

from ..identity import InputError, binding_email, identify, robbery_target
from .support import At, Event, settings


def target(event):
    return robbery_target(event, identify(event, settings()), At)


def test_app_scoped_group_and_c2c_are_distinct():
    group = identify(Event(), settings())
    private = identify(Event(group=""), settings())
    assert group.actor["scope"] == "qq_group"
    assert private.actor["scope"] == "qq_c2c"
    assert group.actor != private.actor
    assert identify(Event(group="other-group"), settings()).actor == group.actor


def test_webhook_and_websocket_share_identity():
    websocket = Event()
    webhook = Event()
    webhook.platform = "qq_official_webhook"
    assert identify(websocket, settings()) == identify(webhook, settings())


def test_raw_botpy_object_with_original_payload():
    event = Event()
    event.message_obj.raw_message = SimpleNamespace(
        id="msg-1",
        author=SimpleNamespace(member_openid="member-a"),
        group_openid="group-a",
        raw_data={"mentions": [{"id": "alias", "member_openid": "member-b"}]},
    )
    assert target(event)["openid"] == "member-b"


@pytest.mark.parametrize(
    "field,value",
    [("platform", "aiocqhttp"), ("platform_id", "another-bot"), ("sender", "spoofed"), ("group", "spoofed")],
)
def test_identity_mismatch_rejected(field, value):
    event = Event()
    setattr(event, field, value)
    with pytest.raises(InputError):
        identify(event, settings())


@pytest.mark.parametrize("message_id", ["", None, "x" * 513, "msg\n1", "msg\x7f1"])
def test_missing_unique_message_id_rejected(message_id):
    with pytest.raises(InputError):
        identify(Event(message_id=message_id), settings())


def test_channel_rejected():
    event = Event(group="")
    event.message_obj.raw_message["author"] = {"id": "channel-user"}
    with pytest.raises(InputError):
        identify(event, settings())


def test_true_mentions_and_bot_exclusion():
    event = Event(
        mentions=[{"id": "bot-id", "is_you": True}, {"id": "target-alias", "member_openid": "member-b"}]
    )
    event.components.append(At("target-alias"))
    assert target(event)["openid"] == "member-b"


def test_structured_at_component():
    event = Event()
    event.components.append(At("member-b"))
    assert target(event)["openid"] == "member-b"


def test_mixed_unresolved_mention_is_not_silently_ignored():
    event = Event(mentions=[{"member_openid": "member-b"}, {"user_openid": "c2c-c", "id": "alias"}])
    with pytest.raises(InputError):
        target(event)


@pytest.mark.parametrize(
    "mentions",
    [
        [],
        [{"id": "bot-id", "is_you": True}],
        [{"member_openid": "member-a"}],
        [{"member_openid": "b"}, {"member_openid": "c"}],
        [{"user_openid": "c2c-b", "id": "alias"}],
    ],
)
def test_invalid_targets_rejected(mentions):
    with pytest.raises(InputError):
        target(Event(text="打劫 @member-b <@member-b> [CQ:at,qq=member-b]", mentions=mentions))


def test_private_robbery_rejected():
    with pytest.raises(InputError):
        target(Event(group="", mentions=[{"id": "member-b"}]))


@pytest.mark.parametrize(
    "text",
    [
        "绑定",
        "绑定 a@example.com extra",
        "绑定 a..b@example.com",
        "绑定 a@localhost",
        "绑定 a@@example.com",
        "绑定 a@-bad.com",
    ],
)
def test_invalid_email(text):
    with pytest.raises(InputError):
        binding_email(text)


def test_email_preserves_local_part():
    assert binding_email("/绑定 User+qq@EXAMPLE.COM") == "User+qq@example.com"
