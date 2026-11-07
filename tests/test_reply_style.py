import copy

import pytest

from ..bridge import BridgeError
from ..presentation import ReplyContext, masked_email, money, quip, render, render_error, site_line
from .support import Event, config
from .test_commands import FakeBridge, collect
from .test_commands import plugin_type as plugin_type


def stats_data():
    return dict(
        state="stats",
        history="admin_api",
        timezone="Asia/Shanghai",
        attacks=2,
        wins=1,
        losses=1,
        defenses=1,
        earned="0.270000",
        compensation="0.100000",
        defense_loss="0.000000",
        defense_income="0.020000",
        net="0.190000",
        pending=0,
        unsettled_excluded=0,
        recent=[dict(role="attack", outcome="failure", delta="-0.100000", at="2026-11-07 15:00:00")],
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0.238000", "0.238"),
        ("0.000001", "0.000001"),
        ("13.364100", "13.3641"),
        ("0", "0.00"),
        ("0.100000", "0.10"),
        ("12.500000", "12.50"),
    ],
)
def test_amounts_remain_exact_and_readable(raw, expected):
    assert money({"value": raw}, "value") == expected


def test_masking_and_context_do_not_invent_binding_success():
    context = ReplyContext(site_url="https://site.example.test", email="linda@example.com", scope="qq_group")
    text = render(
        "bind", dict(state="pending", verification_code="ABCD-EFGH", expires_in_seconds=600), context=context
    )
    assert "l****@example.com" in text and "linda@example.com" not in text
    assert "https://site.example.test" in text and "当前 QQ 群身份" in text
    assert "尚未完成绑定" in text and "✅ 绑定成功" not in text
    assert text.splitlines()[-1] == "/绑定状态" and "\\n" not in text
    assert masked_email("bad\naddress@example.com") == ""


@pytest.mark.parametrize(
    "url",
    [
        "http://unsafe.example.test",
        "https://secret:password@example.test",
        "https://example.test/?token=secret",
        "https://example.test/#secret",
        "https://example.test/\nsecret",
    ],
)
def test_untrusted_site_values_are_not_echoed(url):
    assert site_line(ReplyContext(site_url=url)) == ""


def test_repeat_message_has_stable_flavor_without_randomizing_money():
    contexts = [ReplyContext(flavor_key=str(n)) for n in range(30)]
    assert len({quip("success", context) for context in contexts}) == 3
    assert quip("success", contexts[0]) == quip("success", contexts[0])
    text = render(
        "checkin", dict(state="credited", reward="0.238000", balance="13.364100"), context=contexts[0]
    )
    assert "已发放 0.238 额度" in text and "13.3641" in text
    assert text.splitlines()[-1] == "/签到" and "¥" not in text


def test_zero_compensation_and_no_cooldown_are_not_misrepresented():
    text = render(
        "robbery",
        dict(
            outcome="failure",
            amount="0",
            balance="1",
            robbery_cooldown_remaining_seconds=0,
            target_shield_remaining_seconds=0,
        ),
    )
    assert "赔偿为零" in text and "血亏" not in text and "后才能再抢" not in text
    assert "没有新增目标护盾" in text


@pytest.mark.parametrize("state", ["processing", "review_required"])
def test_uncertain_replies_warn_all_money_commands(state):
    text = render("checkin", dict(state=state, operation_id="01234567-89ab-cdef-0123-456789abcdef"))
    assert all(word in text for word in ["签到", "充值", "打劫", "流水号"])
    assert "已到账" not in text and "成功" not in text


@pytest.mark.parametrize("command,kind", [("reset_shields", "shields"), ("reset_cooldowns", "cooldowns")])
def test_reset_commands_check_admin_and_render_scope(plugin_type, command, kind):
    plugin = plugin_type(None, config())
    plugin.client = FakeBridge(dict(state="reset", kind=kind, affected_accounts=3, scope="all_accounts"))
    event = Event("重置护盾" if kind == "shields" else "重置出手")
    event.is_admin = lambda: False
    assert "管理员" in collect(getattr(plugin, command), event)[0].text and not plugin.client.calls
    event.is_admin = lambda: True
    text = collect(getattr(plugin, command), event)[0].text
    assert "全站 3 个账号" in text and plugin.client.calls[0][0] == command
    assert "balance" not in plugin.client.calls[0][1]
    event.text += " @other"
    before = len(plugin.client.calls)
    assert "不接收" in collect(getattr(plugin, command), event)[0].text
    assert len(plugin.client.calls) == before


def test_stats_command_returns_current_user_only(plugin_type):
    plugin = plugin_type(None, config())
    plugin.client = FakeBridge(stats_data())
    text = collect(plugin.stats, Event("战绩"))[0].text
    assert "得手率：50.0%" in text and "净变化：+0.19" in text and "Asia/Shanghai" in text
    assert plugin.client.calls[0][0] == "stats" and "target" not in plugin.client.calls[0][1]
    before = len(plugin.client.calls)
    assert "不接收其他用户" in collect(plugin.stats, Event("战绩 @someone"))[0].text
    assert len(plugin.client.calls) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("attacks", True),
        ("wins", 4),
        ("earned", "NaN"),
        ("net", "-999"),
        ("recent", {}),
        ("timezone", "bad\nzone"),
    ],
)
def test_stats_invalid_fields_fail_closed(field, value):
    data = stats_data()
    data[field] = value
    with pytest.raises(BridgeError, match="INVALID_RESPONSE"):
        render("stats", data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("role", "admin"),
        ("outcome", "mystery"),
        ("delta", "0.1"),
        ("at", "2026-02-30 15:00:00"),
        ("at", "secret-token"),
    ],
)
def test_recent_record_validation(field, value):
    data = copy.deepcopy(stats_data())
    data["recent"][0][field] = value
    with pytest.raises(BridgeError, match="INVALID_RESPONSE"):
        render("stats", data)


def test_reset_admin_error_is_not_recharge_error():
    message = render_error(BridgeError("ADMIN_REQUIRED"), action="reset_shields")
    assert "重置权限不足" in message and "充值权限不足" not in message


def test_help_lists_new_commands(plugin_type):
    text = collect(plugin_type(None, config()).help_command, Event())[0].text
    assert all(command in text for command in ["/重置护盾", "/重置出手", "/战绩"])
    assert "全站" in text and "不代表服务端已开放" in text


@pytest.mark.parametrize("action", ["reset_shields", "reset_cooldowns"])
def test_malformed_reset_cannot_claim_success(action):
    with pytest.raises(BridgeError, match="INVALID_RESPONSE"):
        render(action, dict(state="reset", kind="shields", affected_accounts=True, scope="all_accounts"))
