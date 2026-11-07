"""Use adapter-authenticated IDs, never QQ numbers typed in message text."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .config import ID_PATTERN, Settings

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
        raise InputError("QQ 平台没有提供有效身份，请检查官方机器人适配器。")
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
        raise InputError("此插件只接受所配置的 QQ 官方机器人。")
    raw = event.message_obj.raw_message
    author = raw_field(raw, "author", {})
    group = raw_field(raw, "group_openid")
    if group:
        group = valid_id(group)
        openid = valid_id(get(author, "member_openid"))
        scope = "qq_group"
        if event.get_group_id() != group:
            raise InputError("群身份不一致，已拒绝操作。")
    elif get(author, "user_openid") and event.is_private_chat():
        openid = valid_id(get(author, "user_openid"))
        scope = "qq_c2c"
    else:
        raise InputError("仅支持 QQ 官方机器人的群聊和 C2C 私聊，暂不支持频道。")
    if event.get_sender_id() != openid:
        raise InputError("消息发送者身份不一致，已拒绝操作。")
    message_id = get(event.message_obj, "message_id")
    raw_id = raw_field(raw, "id")
    if (
        not isinstance(message_id, str)
        or not 1 <= len(message_id) <= 512
        or any(ord(char) < 32 for char in message_id)
        or (raw_id is not None and raw_id != message_id)
    ):
        raise InputError("消息缺少可靠的唯一 ID，已拒绝额度操作，请重新发送命令。")
    return Identity(
        actor={"provider": "qq_official", "app_id": settings.app_id, "scope": scope, "openid": openid},
        context={"platform_id": settings.platform_id, "group_openid": group or "", "message_id": message_id},
    )


def robbery_target(event: Any, identity: Identity, at_type: type) -> dict:
    if not identity.is_group:
        raise InputError("打劫只能在 QQ 群内使用：/打劫 @一位群友")
    raw = event.message_obj.raw_message
    raw_mentions = get(get(raw, "raw_data", {}), "mentions")
    mentions = raw_mentions if isinstance(raw_mentions, list) else raw_field(raw, "mentions", [])
    if not isinstance(mentions, list):
        raise InputError("QQ 平台的 @ 数据异常，已拒绝操作。")
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
            raise InputError("QQ 没有提供被 @ 成员的有效群身份，已拒绝操作。请检查官方适配器和机器人权限。")
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
            "请使用 QQ 的真实 @ 选择且只选择一位群友：/打劫 @群友。纯文本昵称、QQ 号和引用消息不算 @；平台未下发目标时无法打劫。"
        )
    target = next(iter(targets))
    if target == identity.actor["openid"]:
        raise InputError("不能打劫自己。")
    return {**identity.actor, "openid": target}


def binding_email(message: str) -> str:
    parts = message.strip().split()
    if len(parts) != 2:
        raise InputError(
            "用法：/绑定 你的Sub2API邮箱。绑定必须在站内由账号本人确认；群聊发送邮箱会被群成员看到。"
        )
    email = parts[1]
    if len(email) > 254 or email.count("@") != 1 or not email.isascii():
        raise InputError("请输入有效的邮箱地址。")
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
        raise InputError("请输入有效的邮箱地址。")
    return local + "@" + domain.lower()
