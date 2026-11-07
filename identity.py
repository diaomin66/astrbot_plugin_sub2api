"""Use adapter-authenticated IDs, never QQ numbers typed in message text."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .config import ID_PATTERN, ConfigError, Settings, amount, decimal_text

OFFICIAL_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})


class InputError(ValueError):
    pass


def get(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def raw_field(raw: Any, name: str, default: Any = None) -> Any:
    value = get(raw, name)
    return get(get(raw, "raw_data", {}), name, default) if value is None else value


def valid_id(value: Any) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise InputError("🪪 没收到有效的 QQ 平台身份，先不动你的额度。请管理员检查官方机器人适配器。")
    return value


@dataclass(frozen=True)
class Identity:
    actor: dict
    context: dict

    @property
    def is_group(self) -> bool:
        return self.actor["scope"] == "qq_group"


def identify(event: Any, settings: Settings) -> Identity:
    if event.get_platform_name() not in OFFICIAL_PLATFORMS or event.get_platform_id() != settings.platform_id:
        raise InputError("🧭 走错门啦，此插件只接受所配置的 QQ 官方机器人。")
    raw = event.message_obj.raw_message
    author = raw_field(raw, "author", {})
    group = raw_field(raw, "group_openid")
    if group:
        group = valid_id(group)
        openid = valid_id(get(author, "member_openid"))
        scope = "qq_group"
        if event.get_group_id() != group:
            raise InputError("🔒 群身份对不上，已拒绝操作，先保护好小金库。")
    elif get(author, "user_openid") and event.is_private_chat():
        openid = valid_id(get(author, "user_openid"))
        scope = "qq_c2c"
    else:
        raise InputError("🧭 请到 QQ 官方机器人的群聊或 C2C 私聊来玩，暂不支持频道。")
    if event.get_sender_id() != openid:
        raise InputError("🔒 消息发送者身份对不上，已拒绝操作，不冒险动额度。")
    message_id = get(event.message_obj, "message_id")
    raw_id = raw_field(raw, "id")
    if (
        not isinstance(message_id, str)
        or not 1 <= len(message_id) <= 512
        or any(ord(char) < 32 or ord(char) == 127 for char in message_id)
        or (raw_id is not None and raw_id != message_id)
    ):
        raise InputError("🪪 这条消息缺少可靠的唯一 ID，本次已拒绝额度操作。请重新发送命令。")
    return Identity(
        actor={"provider": "qq_official", "app_id": settings.app_id, "scope": scope, "openid": openid},
        context={"platform_id": settings.platform_id, "group_openid": group or "", "message_id": message_id},
    )


def robbery_target(event: Any, identity: Identity, at_type: type, *, allow_self=False) -> dict:
    if not identity.is_group:
        raise InputError("🗡️ 私聊里可抢不到群友，打劫只能在 QQ 群内使用：/打劫 @一位群友")
    raw = event.message_obj.raw_message
    raw_mentions = get(get(raw, "raw_data", {}), "mentions")
    mentions = raw_mentions if isinstance(raw_mentions, list) else raw_field(raw, "mentions", [])
    if not isinstance(mentions, list):
        raise InputError("🧩 QQ 平台的 @ 数据有点乱，已拒绝操作，避免认错人。")
    bot_ids = {str(event.get_self_id()), "qq_official", "unknown_selfid", "all", "0"}
    mapping: dict[str, str] = {}
    targets: set[str] = set()
    for mention in mentions:
        if get(mention, "is_you") is True or get(mention, "bot") is True:
            bot_ids.update(str(v) for v in (get(mention, "id"), get(mention, "member_openid")) if v)
    for mention in mentions:
        mid = get(mention, "id")
        member = get(mention, "member_openid")
        if get(mention, "is_you") is True or get(mention, "bot") is True:
            continue
        # A C2C-only user_openid is not proof of a group member's identity.
        candidate = member or (mid if not get(mention, "user_openid") else None)
        if not candidate:
            raise InputError(
                "🪪 QQ 没有提供被 @ 成员的有效群身份，本次已拒绝操作。请管理员检查官方适配器和机器人权限。"
            )
        if candidate and str(candidate) not in bot_ids:
            target = valid_id(candidate)
            targets.add(target)
            if mid:
                mapping[str(mid)] = target
    for component in event.get_messages():
        if isinstance(component, at_type):
            candidate = str(component.qq)
            if candidate not in bot_ids:
                targets.add(valid_id(mapping.get(candidate, candidate)))
    if len(targets) != 1:
        raise InputError(
            "🎯 先认准一位目标！请使用 QQ 的真实 @ 选择且只选择一位群友：/打劫 @群友。纯文本昵称、QQ 号和引用消息不算 @；平台未下发目标时无法打劫。"
        )
    target = next(iter(targets))
    if target == identity.actor["openid"] and not allow_self:
        raise InputError("🪞 不能打劫自己，左口袋倒右口袋就免啦。")
    return {**identity.actor, "openid": target}


def recharge_input(event: Any, identity: Identity, at_type: type) -> dict:
    usage = "📝 补给单这样填：/充值 @一位已绑定群友 金额，或 /充值 站内邮箱 金额。金额须大于零，最多 6 位小数，不接受负数或科学计数。"
    parts = event.get_message_str().strip().split()
    if len(parts) < 2:
        raise InputError(usage)
    try:
        value = amount(parts[-1])
    except ConfigError as exc:
        raise InputError(usage) from exc
    if value <= 0:
        raise InputError(usage)
    emails = [p for p in parts[1:-1] if "@" in p and not p.startswith(("@", "<@"))]
    if emails:
        if len(parts) != 3 or len(emails) != 1:
            raise InputError(usage)
        raw = event.message_obj.raw_message
        mentions = raw_field(raw, "mentions", [])
        if any(not (get(m, "is_you") is True or get(m, "bot") is True) for m in mentions):
            raise InputError("🎯 一次只认一位收款人，邮箱和 @ 目标不能混用。")
        if any(
            isinstance(c, at_type)
            and str(c.qq) not in {str(event.get_self_id()), "qq_official", "unknown_selfid"}
            for c in event.get_messages()
        ):
            raise InputError("🎯 一次只认一位收款人，邮箱和 @ 目标不能混用。")
        recipient = {"email": binding_email("绑定 " + emails[0])}
    else:
        if any(re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", p) for p in parts[1:-1]):
            raise InputError(usage)
        try:
            recipient = {"identity": robbery_target(event, identity, at_type, allow_self=True)}
        except InputError as exc:
            raise InputError("🎯 没认准收款人：请在群内真实 @ 一位已绑定成员，或使用站内邮箱充值。") from exc
    return {"recipient": recipient, "amount": decimal_text(value)}


def binding_email(message: str) -> str:
    parts = message.strip().split()
    if len(parts) != 2:
        raise InputError(
            "📮 先递张名片吧，用法：/绑定 你的Sub2API邮箱。绑定必须在站内由账号本人确认；群聊发送邮箱会被群成员看到。"
        )
    email = parts[1]
    if len(email) > 254 or email.count("@") != 1 or not email.isascii():
        raise InputError("📮 这封信找不到收件地址，请输入有效的邮箱地址，检查 @ 和域名是否完整。")
    local, domain = email.rsplit("@", 1)
    if (
        not 1 <= len(local) <= 64
        or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or "." not in domain
        or any(
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in domain.split(".")
        )
    ):
        raise InputError("📮 这封信找不到收件地址，请输入有效的邮箱地址，检查 @ 和域名是否完整。")
    return local + "@" + domain.lower()
