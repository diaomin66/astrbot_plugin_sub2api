import pytest

from ..identity import InputError, identify, recharge_input
from ..presentation import render
from .support import At, Event, config, settings
from .test_commands import FakeBridge, collect
from .test_commands import plugin_type as plugin_type


def parse(event):
    return recharge_input(event, identify(event, settings()), At)


def test_email_and_private_target():
    assert parse(Event("/充值 Alice@EXAMPLE.com 1.25", group="")) == {
        "recipient": {"email": "Alice@example.com"},
        "amount": "1.25",
    }


def test_real_at_target_and_self():
    assert (
        parse(Event("/充值 @群友 2", mentions=[{"member_openid": "member-b"}]))["recipient"]["identity"][
            "openid"
        ]
        == "member-b"
    )
    assert (
        parse(Event("/充值 @我 2", mentions=[{"member_openid": "member-a"}]))["recipient"]["identity"][
            "openid"
        ]
        == "member-a"
    )


@pytest.mark.parametrize(
    "text",
    [
        "充值",
        "充值 a@example.com",
        "充值 a@example.com 0",
        "充值 a@example.com -1",
        "充值 a@example.com NaN",
        "充值 a@example.com 1e2",
        "充值 a@example.com 0.0000001",
        "充值 12345 10",
        "充值 a@example.com 1 2",
    ],
)
def test_invalid_inputs(text):
    with pytest.raises(InputError):
        parse(Event(text))


def test_mixed_target_rejected():
    with pytest.raises(InputError):
        parse(Event("充值 a@example.com 1", mentions=[{"member_openid": "b"}]))


def test_admin_check_before_network(plugin_type):
    cfg = config()
    cfg["recharge"] = {"enabled": True}
    plugin = plugin_type(None, cfg)
    plugin.client = FakeBridge()
    e = Event("充值 a@example.com 1")
    e.is_admin = lambda: False
    assert "管理员" in collect(plugin.recharge, e)[0].text and not plugin.client.calls


def test_admin_recharge_and_flag(plugin_type):
    cfg = config()
    cfg["recharge"] = {"enabled": True}
    plugin = plugin_type(None, cfg)
    plugin.client = FakeBridge({"state": "credited", "amount": "1", "balance": "2"})
    e = Event("充值 a@example.com 1")
    e.is_admin = lambda: True
    assert "充值成功" in collect(plugin.recharge, e)[0].text
    assert plugin.client.calls[0][1]["amount"] == "1"
    plugin = plugin_type(None, config())
    plugin.client = FakeBridge()
    assert "关闭" in collect(plugin.recharge, e)[0].text and not plugin.client.calls


@pytest.mark.parametrize("state", ["processing", "review_required", "refunded"])
def test_pending_never_claims_success(state):
    result = render("recharge", {"state": state, "operation_id": "01234567-89ab-cdef-0123-456789abcdef"})
    assert "充值成功" not in result and "流水号" in result
