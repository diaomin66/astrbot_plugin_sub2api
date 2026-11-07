"""Render only known, validated backend fields, never arbitrary server messages."""

import re

from .bridge import BridgeError, protocol_error
from .config import ConfigError, amount, decimal_text


def money(data: dict, key: str) -> str:
    try:
        return decimal_text(amount(data.get(key), key))
    except ConfigError as exc:
        raise protocol_error() from exc


def seconds(data: dict, key: str) -> int:
    value = data.get(key)
    if type(value) is not int or not 0 <= value <= 31536000:
        raise protocol_error()
    return value


def duration(value: int) -> str:
    if value == 0:
        return "无"
    days, remainder = divmod(value, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    return "".join(
        f"{n}{unit}" for n, unit in [(days, "天"), (hours, "小时"), (minutes, "分"), (secs, "秒")] if n
    )


def render(action: str, data: dict) -> str:
    if action == "bind":
        if data.get("state") != "pending":
            raise protocol_error()
        code = data.get("verification_code")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9-]{6,32}", code):
            raise protocol_error()
        ttl = seconds(data, "expires_in_seconds")
        if not 1 <= ttl <= 3600:
            raise protocol_error()
        return (
            "已受理绑定申请。若邮箱对应账号存在，请登录你自己的 Sub2API 账号，"
            "在「QQ 绑定」中核对并输入本次确认码后确认。\n"
            f"确认码：{code}\n有效期：{duration(ttl)}\n"
            "确认前不会获得任何账户权限；不要确认非本人发起的申请。\n"
            "确认后发送 /绑定状态 查询。群内邮箱可被其他成员看到。"
        )
    if action == "account":
        state = data.get("state")
        if state == "unbound":
            return "当前 QQ 身份尚未绑定。发送 /绑定 邮箱，再到 Sub2API 账号内确认。群聊与私聊身份需分别验证绑定。"
        if state == "pending":
            return f"绑定待确认，请到 Sub2API 账号内完成确认。剩余：{duration(seconds(data, 'expires_in_seconds'))}。"
        if state != "bound" or type(data.get("checked_in_today")) is not bool:
            raise protocol_error()
        checkin = "已签到" if data["checked_in_today"] else "未签到"
        return (
            f"绑定状态：已绑定\n可用额度：{money(data, 'balance')}\n今日签到：{checkin}\n"
            f"护盾剩余：{duration(seconds(data, 'shield_remaining_seconds'))}\n"
            f"打劫冷却：{duration(seconds(data, 'robbery_cooldown_remaining_seconds'))}"
        )
    if action == "checkin":
        if data.get("state") not in {"credited", "already_checked_in"}:
            raise protocol_error()
        if data["state"] == "already_checked_in":
            return f"今天已经签到过啦，不会重复发放。当前可用额度：{money(data, 'balance')}"
        return f"签到成功！获得额度：{money(data, 'reward')}\n当前可用额度：{money(data, 'balance')}"
    if action == "robbery":
        outcome = data.get("outcome")
        if outcome not in {"success", "failure"}:
            raise protocol_error()
        transferred = money(data, "amount")
        headline = (
            f"打劫成功！获得额度：{transferred}"
            if outcome == "success"
            else f"打劫失败！赔偿对方额度：{transferred}"
        )
        return (
            f"{headline}\n当前可用额度：{money(data, 'balance')}\n"
            f"打劫冷却：{duration(seconds(data, 'robbery_cooldown_remaining_seconds'))}\n"
            f"对方护盾：{duration(seconds(data, 'target_shield_remaining_seconds'))}"
        )
    raise protocol_error()


ERROR_MESSAGES = {
    "NOT_BOUND": "你还没有完成绑定，请发送 /绑定 邮箱 并到站内确认。",
    "BINDING_PENDING": "绑定尚未确认，请登录 Sub2API 完成确认。",
    "ALREADY_BOUND": "当前 QQ 身份已绑定；换绑或解绑请在 Sub2API 账号设置中操作。",
    "TARGET_NOT_BOUND": "对方还没有完成绑定，无法打劫。",
    "SAME_ACCOUNT": "不能打劫自己或绑定到同一 Sub2API 账号的身份。",
    "TARGET_SHIELDED": "对方受到护盾保护，暂时不能打劫。",
    "ROBBERY_COOLDOWN": "你还处于打劫冷却中。",
    "INSUFFICIENT_BALANCE": "你的可用额度不足以覆盖最低失败赔偿，无法发起打劫。",
    "TARGET_EMPTY": "对方没有可被打劫的额度。",
    "ACCOUNT_DISABLED": "账号不可用，请到 Sub2API 查看状态。",
    "FEATURE_DISABLED": "该功能已被管理员关闭。",
    "RATE_LIMITED": "操作过于频繁，请稍后再试。",
    "POLICY_REJECTED": "玩法配置超出服务端允许范围，请管理员检查配置。",
    "IDEMPOTENCY_CONFLICT": "同一消息的处理参数不一致，已拒绝重复执行。请查询账户状态。",
    "UNAUTHORIZED": "后端认证失败，请管理员检查专用 bridge 令牌及权限。",
    "BRIDGE_NOT_INSTALLED": "Sub2API 尚未安装 QQ bridge 接口，或接口地址不正确。",
    "REDIRECT_REFUSED": "接口发生重定向，已阻止令牌转发，请管理员检查地址。",
    "INVALID_RESPONSE": "后端响应格式异常，结果尚未确认，请查询账户状态，不要连续重试打劫。",
    "UNAVAILABLE": "后端暂时不可用，操作可能已执行。请先用 /我的账号 核对，暂勿重新发送打劫命令。",
}


def render_error(error: BridgeError) -> str:
    message = ERROR_MESSAGES.get(error.code, "请求未完成，请稍后查询账号状态或联系管理员。")
    if error.retry_after:
        message += f" 剩余等待：{duration(error.retry_after)}。"
    return message
