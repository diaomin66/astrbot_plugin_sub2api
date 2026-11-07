import pytest

from ..bridge import BridgeError
from ..presentation import duration, render, render_error


def test_bound_status():
    result = render(
        "account",
        {
            "state": "bound",
            "balance": "12.500000",
            "checked_in_today": True,
            "shield_remaining_seconds": 65,
            "robbery_cooldown_remaining_seconds": 0,
        },
    )
    assert "12.500000" in result and "1分5秒" in result and "已签到" in result


def test_pending_binding_does_not_expose_email_or_internal_fields():
    result = render(
        "bind",
        {
            "state": "pending",
            "verification_code": "ABCD-EFGH",
            "expires_in_seconds": 600,
            "email": "secret@example.com",
            "message": "secret-token",
        },
    )
    assert "ABCD-EFGH" in result and "secret" not in result and "确认" in result
    assert "个人设置 → 管理 QQ 绑定" in result and "手动输入" in result
    assert "http" not in result


def test_pending_account_points_to_modal_without_unverified_url():
    result = render("account", {"state": "pending", "expires_in_seconds": 180})
    assert "个人设置 → 管理 QQ 绑定" in result and "手动输入" in result
    assert "http" not in result


def test_tls_error_does_not_suggest_disabling_verification():
    result = render_error(BridgeError("TLS_ERROR"))
    assert "不要关闭证书校验" in result


@pytest.mark.parametrize(
    "code,expected",
    [("ADMIN_REQUIRED", "充值权限不足"), ("TARGET_NOT_FOUND", "核对邮箱"), ("APP_NOT_ALLOWED", "AppID")],
)
def test_permission_and_missing_target_errors_do_not_blame_token(code, expected):
    result = render_error(BridgeError(code))
    assert expected in result
    assert "后端认证失败" not in result


def test_billing_protection_explains_why_transfer_is_blocked():
    result = render_error(BridgeError("ACCOUNT_BILLING_BUSY"))
    assert "待核实账单" in result and "暂不能打劫" in result
    assert "联系管理员" in result


def test_economy_gate_does_not_claim_credit_or_transfer():
    result = render_error(BridgeError("ECONOMY_NOT_READY"))
    assert "不会发放或转移" in result


@pytest.mark.parametrize(
    "action,data",
    [
        ("bind", {"state": "pending", "verification_code": "<script>", "expires_in_seconds": 600}),
        ("bind", {"state": "pending", "verification_code": "ABCDEFGH", "expires_in_seconds": 0}),
        ("checkin", {"state": "credited", "reward": 1.0, "balance": "1"}),
        ("checkin", {"state": "credited", "reward": "-1", "balance": "1"}),
        ("account", {"state": "unknown"}),
        ("account", {"state": "pending", "expires_in_seconds": True}),
        ("robbery", {"outcome": "success", "amount": "1"}),
    ],
)
def test_malformed_results_fail_closed(action, data):
    with pytest.raises(BridgeError, match="INVALID_RESPONSE"):
        render(action, data)


def test_duplicate_checkin():
    result = render("checkin", {"state": "already_checked_in", "balance": "2.00"})
    assert "不会重复" in result


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_robbery_results(outcome):
    result = render(
        "robbery",
        {
            "outcome": outcome,
            "amount": "0.10",
            "balance": "1.00",
            "robbery_cooldown_remaining_seconds": 3600,
            "target_shield_remaining_seconds": 7200,
        },
    )
    assert ("获得" if outcome == "success" else "赔偿") in result
    assert "1小时" in result and "2小时" in result


def test_error_safety_and_duration():
    assert "5分" in render_error(BridgeError("TARGET_SHIELDED", 300))
    assert "secret-token" not in render_error(BridgeError("secret-token"))
    assert duration(86461) == "1天1分1秒"
