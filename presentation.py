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
    if action in {"checkin", "robbery", "recharge"} and data.get("state") in {
        "processing",
        "review_required",
        "refunded",
    }:
        messages = {
            "processing": "交易处理中，尚未确认完成。请勿重新发送充值或打劫，稍后查询账号或联系管理员。",
            "review_required": "交易结果待管理员对账，已阻止自动重复记账。请勿重新发起充值或打劫。",
            "refunded": "转账未完成，已确认退回扣款；本次没有完成打劫转账。",
        }
        reference = data.get("operation_id", "")
        if not isinstance(reference, str) or not re.fullmatch(r"[0-9a-f-]{36}", reference):
            raise protocol_error()
        return messages[data["state"]] + "\n流水号：" + reference
    if action == "recharge":
        if data.get("state") != "credited":
            raise protocol_error()
        return f"充值成功！增加额度：{money(data, 'amount')}\n目标当前可用额度：{money(data, 'balance')}"
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
            "在「个人设置 → 管理 QQ 绑定」弹窗中核对申请，手动输入本次确认码后确认。\n"
            f"确认码：{code}\n有效期：{duration(ttl)}\n"
            "确认前不会获得任何账户权限；不要确认非本人发起的申请。\n"
            "确认后发送 /绑定状态 查询。群内邮箱可被其他成员看到。"
        )
    if action == "account":
        state = data.get("state")
        if state == "unbound":
            return "当前 QQ 身份尚未绑定。发送 /绑定 邮箱，再到 Sub2API 账号内确认。群聊与私聊身份需分别验证绑定。"
        if state == "pending":
            return (
                "绑定待确认，请登录 Sub2API，在「个人设置 → 管理 QQ 绑定」弹窗中手动输入确认码。"
                f"剩余：{duration(seconds(data, 'expires_in_seconds'))}。"
            )
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
    "BINDING_PENDING": "绑定尚未确认，请登录 Sub2API，在「个人设置 → 管理 QQ 绑定」弹窗中手动输入确认码。",
    "ALREADY_BOUND": "当前 QQ 身份已绑定；换绑或解绑请在 Sub2API 账号设置中操作。",
    "TARGET_NOT_BOUND": "对方还没有完成绑定，无法执行此操作。",
    "ADMIN_REQUIRED": "充值权限不足：须为机器人管理员，且 QQ 身份须列入 bridge 服务端充值白名单。",
    "APP_NOT_ALLOWED": "机器人应用未获后端授权，请管理员检查 bridge 令牌允许的 AppID。",
    "TARGET_NOT_FOUND": "未找到该站内账号，请核对邮箱。",
    "ACCOUNT_TRANSACTION_PENDING": "相关账号存在未完成额度交易，请等待恢复或联系管理员对账，勿重复发起。",
    "ADMIN_API_UNAVAILABLE": "管理员余额接口暂不可用，尚未确认记账结果，请联系管理员查询流水。",
    "SAME_ACCOUNT": "不能打劫自己或绑定到同一 Sub2API 账号的身份。",
    "TARGET_SHIELDED": "对方受到护盾保护，暂时不能打劫。",
    "ROBBERY_COOLDOWN": "你还处于打劫冷却中。",
    "INSUFFICIENT_BALANCE": "你的可用额度不足以覆盖最低失败赔偿，无法发起打劫。",
    "TARGET_EMPTY": "对方没有可被打劫的额度。",
    "ACCOUNT_BILLING_BUSY": "你或对方仍有模型请求、图片预留或待核实账单，暂不能打劫，避免转走已用于消费的额度。请等待结算后再试；长时间未恢复请联系管理员核对账单。",
    "ECONOMY_NOT_READY": "签到/打劫的计费保护尚未完成上线验收，当前不会发放或转移额度。请管理员检查服务端经济功能状态。",
    "ACCOUNT_DISABLED": "账号不可用，请到 Sub2API 查看状态。",
    "FEATURE_DISABLED": "服务端尚未开放该功能。仅开启插件开关不会生效，请等待服务端完成验收并开放。",
    "RATE_LIMITED": "操作过于频繁，请稍后再试。",
    "POLICY_REJECTED": "玩法配置超出服务端允许范围，请管理员检查配置。",
    "IDEMPOTENCY_CONFLICT": "同一消息的处理参数不一致，已拒绝重复执行。请查询账户状态。",
    "UNAUTHORIZED": "后端认证失败，请管理员检查专用 bridge 令牌及权限。",
    "INVALID_REQUEST": "后端拒绝了请求格式，请管理员核对 bridge 接口版本和身份字段。",
    "INVALID_IDEMPOTENCY_KEY": "后端拒绝了请求的幂等标识，请管理员检查接口兼容性，勿重复发起额度操作。",
    "UNSUPPORTED_VERSION": "后端不支持当前 bridge 协议版本，请管理员核对插件与后端版本。",
    "TLS_ERROR": "HTTPS 证书校验或 TLS 握手失败，已拒绝不安全连接。请管理员修复证书、域名或信任链，不要关闭证书校验；若此前已尝试操作，请先核对账户状态。",
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
