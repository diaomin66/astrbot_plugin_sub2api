"""Validated configuration; monetary values never pass through binary floats."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

AMOUNT_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,6})?\Z")
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class ConfigError(ValueError):
    pass


def amount(value: object, name: str = "amount") -> Decimal:
    if not isinstance(value, str) or not AMOUNT_PATTERN.fullmatch(value):
        raise ConfigError(f"{name} 必须是非负十进制字符串，最多 10 位整数、6 位小数")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ConfigError(f"{name} 不是有效额度") from exc


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def section(config: Mapping, name: str) -> Mapping:
    result = config.get(name, {})
    if not isinstance(result, Mapping):
        raise ConfigError(f"{name} 必须是配置对象")
    return result


def boolean(config: Mapping, name: str, default: bool) -> bool:
    result = config.get(name, default)
    if type(result) is not bool:
        raise ConfigError(f"{name} 必须是布尔值")
    return result


def integer(config: Mapping, name: str, default: int, low: int, high: int) -> int:
    result = config.get(name, default)
    if type(result) is not int or not low <= result <= high:
        raise ConfigError(f"{name} 必须是 {low} 至 {high} 之间的整数")
    return result


def text(config: Mapping, name: str, default: str = "") -> str:
    value = config.get(name, default)
    if not isinstance(value, str):
        raise ConfigError(f"{name} 必须是字符串")
    return value.strip()


def amount_range(config: Mapping, prefix: str, defaults: tuple[str, str], positive: bool) -> dict:
    low = amount(config.get(f"{prefix}_min", defaults[0]), f"{prefix}_min")
    high = amount(config.get(f"{prefix}_max", defaults[1]), f"{prefix}_max")
    if low > high or (positive and low <= 0):
        raise ConfigError(f"{prefix} 额度范围无效：下限不得高于上限，奖励下限须大于零")
    return {"min": decimal_text(low), "max": decimal_text(high)}


@dataclass(frozen=True)
class Settings:
    base_url: str
    token: str = field(repr=False)
    app_id: str
    platform_id: str
    timeout_seconds: int
    allow_group_binding: bool
    policy: dict

    @classmethod
    def from_mapping(cls, config: Mapping) -> Settings:
        connection = section(config, "connection")
        binding = section(config, "binding")
        checkin = section(config, "checkin")
        robbery = section(config, "robbery")
        url = text(connection, "base_url").rstrip("/")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise ConfigError("base_url 格式无效") from exc
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)
            or any(char.isspace() or ord(char) < 32 for char in url)
            or "\\" in url
        ):
            raise ConfigError("base_url 必须是无凭证、查询参数和片段的 Sub2API 站点地址")
        if parsed.scheme != "https" and not boolean(connection, "allow_insecure_http", False):
            raise ConfigError("base_url 须使用 HTTPS；仅可信内网可显式开启 allow_insecure_http")
        env_name = text(connection, "token_env", "SUB2API_QQ_BRIDGE_TOKEN")
        token = os.environ.get(env_name, "").strip() if env_name else ""
        token = token or text(connection, "bridge_token")
        if not re.fullmatch(r"[\x21-\x7e]{32,512}", token):
            raise ConfigError("bridge_token 需要 32 至 512 位非空白 ASCII 字符的专用服务令牌")
        app_id = text(connection, "qq_app_id")
        platform_id = text(connection, "platform_id")
        if not ID_PATTERN.fullmatch(app_id):
            raise ConfigError("请配置 QQ 开放平台的 qq_app_id")
        if not platform_id or len(platform_id) > 128 or any(ord(c) < 32 for c in platform_id):
            raise ConfigError("请配置此机器人的 AstrBot platform_id，防止多机器人身份串用")
        timezone = text(checkin, "timezone", "Asia/Shanghai")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError("timezone 必须是有效 IANA 时区，例如 Asia/Shanghai") from exc
        probability = amount(robbery.get("success_probability", "0.40"), "success_probability")
        if probability > 1:
            raise ConfigError("success_probability 必须介于 0 和 1 之间")
        policy = {
            "binding_ttl_seconds": integer(binding, "ttl_seconds", 600, 60, 3600),
            "checkin": {
                "enabled": boolean(checkin, "enabled", True),
                "timezone": timezone,
                "reward": amount_range(checkin, "reward", ("0.10", "0.50"), True),
            },
            "robbery": {
                "enabled": boolean(robbery, "enabled", True),
                "success_probability": decimal_text(probability),
                "success_amount": amount_range(robbery, "success", ("0.05", "0.20"), True),
                "failure_amount": amount_range(robbery, "failure", ("0.01", "0.05"), False),
                "cooldown_seconds": integer(robbery, "cooldown_seconds", 3600, 0, 2592000),
                "shield_seconds": integer(robbery, "shield_seconds", 7200, 0, 2592000),
            },
        }
        return cls(
            base_url=url,
            token=token,
            app_id=app_id,
            platform_id=platform_id,
            timeout_seconds=integer(connection, "timeout_seconds", 10, 1, 60),
            allow_group_binding=boolean(binding, "allow_group_binding", True),
            policy=policy,
        )
