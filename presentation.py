"""Friendly replies using validated facts, never raw backend messages or invented identity."""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .bridge import BridgeError, protocol_error
from .config import ConfigError, Settings, amount, decimal_text
from .identity import InputError, binding_email


@dataclass(frozen=True)
class ReplyContext:
    site_url: str = ""
    email: str = ""
    scope: str = ""
    flavor_key: str = ""


def money(data: dict, key: str) -> str:
    try:
        value = decimal_text(amount(data.get(key), key))
    except ConfigError as exc:
        raise protocol_error() from exc
    # Keep every significant decimal; never round a tiny reward or balance to zero.
    whole, _, fraction = value.partition(".")
    return whole + "." + fraction.rstrip("0").ljust(2, "0")


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


def masked_email(value: str) -> str:
    try:
        email = binding_email("绑定 " + value)
    except (InputError, TypeError):
        return ""
    local, domain = email.rsplit("@", 1)
    first = local[0] if local[0].isalnum() else "*"
    return first + "****@" + domain


def site_line(context: ReplyContext) -> str:
    value = context.site_url
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
            or "\\" in value
        ):
            return ""
    except ValueError:
        return ""
    return "🌐 站点：" + value.rstrip("/") + "\n"


def scope_line(context: ReplyContext) -> str:
    # QQ official adapters expose scoped OpenIDs, not necessarily the person's QQ number.
    label = {"qq_group": "当前 QQ 群身份", "qq_c2c": "当前 QQ 私聊身份"}.get(context.scope)
    return "👤 身份：" + label + "\n" if label else ""


def quip(kind: str, context: ReplyContext) -> str:
    choices = {
        "checkin": (
            "今日份小确幸到账，积少成多 ✨",
            "签到不缺席，小金库多一点 🍀",
            "今日份好运已签收，明天见 ☀️",
        ),
        "success": ("见好就收，别贪 😏", "这一手很稳，收好战利品再走 😎", "本次行动收工，记得低调 🤫"),
        "failure": (
            "这波血亏，先喝口水冷静一下 🥲",
            "手慢一步，钱包替你交学费了 🫠",
            "出门没看黄历，下次先深呼吸 😮‍💨",
        ),
        "recharge": (
            "补给到账，继续快乐探索 🚀",
            "小金库已补充，查收这份能量 ✨",
            "额度补给完成，安排得明明白白 📦",
        ),
    }[kind]
    index = int.from_bytes(hashlib.sha256((kind + context.flavor_key).encode()).digest()[:4], "big")
    return choices[index % len(choices)]


def gift_line(data: dict) -> str:
    gift = data.get("binding_gift")
    if gift is None:
        return ""
    if not isinstance(gift, dict) or gift.get("state") not in {
        "credited",
        "processing",
        "review_required",
        "failed",
    }:
        raise protocol_error()
    value = money(gift, "amount")
    if amount(gift["amount"]) != 2:
        raise protocol_error()
    if gift["state"] == "credited":
        return f"🎁 首绑新人礼：${value} 已到账！每个站内账号仅领一次，开局有粮啦 ✨\n"
    if gift["state"] == "processing":
        return f"🎁 首绑新人礼：${value} 发放处理中，稍后查账即可，不用重新绑定。\n"
    return "🧾 新人礼尚未确认到账，请管理员核对流水；不要解绑重领。\n"


def render(action: str, data: dict, *, context: ReplyContext | None = None) -> str:
    context = context or ReplyContext()
    if action == "stats":
        return render_stats(data)
    if action in {"reset_shields", "reset_cooldowns"}:
        expected = "shields" if action == "reset_shields" else "cooldowns"
        if (
            data.get("state") != "reset"
            or data.get("kind") != expected
            or data.get("scope") != "all_accounts"
        ):
            raise protocol_error()
        count = count_field(data, "affected_accounts")
        if action == "reset_shields":
            return (
                f"🛡️ 护盾已清场！已清除全站 {count} 个账号的当前护盾。\n"
                "余额、出手冷却和历史战绩保持不变。重置后新完成的打劫仍按规则产生护盾。"
            )
        return (
            f"⏱️ 出手计时归零！已清除全站 {count} 个账号的当前打劫冷却。\n"
            "所有账号无需再等原有出手冷却；下次有效出手后重新计时。\n"
            "🛡️ 目标护盾、余额门槛、未决交易和服务端规则仍然有效，不是无限制开抢哦。"
        )
    if action in {"checkin", "robbery", "recharge"} and data.get("state") in {
        "processing",
        "review_required",
        "refunded",
    }:
        messages = {
            "processing": "⏳ 账本正在处理中，先别连点！\n尚未确认完成，请勿重新发送签到、充值或打劫。稍后查询账号，或联系管理员。",
            "review_required": "🧾 这笔交易需要管理员核对。\n结果尚未确认，已阻止自动重复记账。请勿重新发起签到、充值或打劫。",
            "refunded": "↩️ 本次转账未完成，扣款已确认退回。\n这次没有完成打劫转账，未按得手结算。",
        }
        reference = data.get("operation_id", "")
        if not isinstance(reference, str) or not re.fullmatch(r"[0-9a-f-]{36}", reference):
            raise protocol_error()
        return messages[data["state"]] + "\n流水号：" + reference
    if action == "recharge":
        if data.get("state") != "credited":
            raise protocol_error()
        target = masked_email(context.email)
        recipient = f"📮 目标：{target}\n" if target else ""
        return (
            "✅ 充值成功，补给已到账！\n"
            + recipient
            + f"➕ 增加额度：{money(data, 'amount')}\n"
            + f"💰 目标当前可用额度：{money(data, 'balance')}\n"
            + quip("recharge", context)
        )
    if action == "bind":
        if data.get("state") != "pending":
            raise protocol_error()
        code = data.get("verification_code")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9-]{6,32}", code):
            raise protocol_error()
        ttl = seconds(data, "expires_in_seconds")
        if not 1 <= ttl <= 3600:
            raise protocol_error()
        email = masked_email(context.email)
        submitted = f"📮 申请邮箱：{email}\n" if email else ""
        return (
            "📨 绑定申请已递出，还差你亲自确认！\n"
            + scope_line(context)
            + submitted
            + site_line(context)
            + f"🔑 确认码：{code}\n⏳ 有效期：{duration(ttl)}\n"
            + "若邮箱对应账号存在，请登录你自己的 Sub2API 账号，打开「个人设置 → 管理 QQ 绑定」，"
            "核对申请并手动输入确认码。\n"
            "🔒 目前尚未完成绑定，确认前不会获得账户权限；不要确认非本人申请。\n"
            "群内原消息里的邮箱和确认码仍对群友可见。确认后发送下一行查询：\n/绑定状态"
        )
    if action == "account":
        state = data.get("state")
        if state == "unbound":
            return (
                "🧭 你的小金库还没接上：当前 QQ 身份尚未绑定。\n"
                "先发送下一行，把「你的邮箱」替换成站内邮箱：\n/绑定 你的邮箱\n"
                + site_line(context)
                + "然后到站内由本人确认。群聊与私聊身份需分别验证绑定，别串门啦 🔐"
            )
        if state == "pending":
            return (
                "⏳ 绑定待确认，就差最后一步啦！\n"
                + site_line(context)
                + "请登录你自己的 Sub2API 账号，在「个人设置 → 管理 QQ 绑定」中手动输入本次确认码。\n"
                + f"剩余时间：{duration(seconds(data, 'expires_in_seconds'))}。\n"
                + "当前尚未完成绑定；不要确认非本人发起的申请。"
            )
        if state != "bound" or type(data.get("checked_in_today")) is not bool:
            raise protocol_error()
        shield = seconds(data, "shield_remaining_seconds")
        cooldown = seconds(data, "robbery_cooldown_remaining_seconds")
        checkin = "已签到，今日份奖励已领" if data["checked_in_today"] else "未签到，今日份奖励等你来"
        shield_text = "还剩 " + duration(shield) if shield else "暂无护盾，小金库记得看好"
        cooldown_text = (
            "还剩 " + duration(cooldown) if cooldown else "无冷却，可尝试出手（仍需满足余额和目标条件）"
        )
        return (
            "✅ 绑定已生效！你的小金库快报来啦\n"
            + scope_line(context)
            + site_line(context)
            + gift_line(data)
            + f"💰 可用额度：{money(data, 'balance')}\n📅 今日签到：{checkin}\n"
            + f"🛡️ 护盾：{shield_text}\n⏳ 打劫冷却：{cooldown_text}\n"
            + (
                "明天继续签到，开头保留 /：\n/签到"
                if data["checked_in_today"]
                else "领取今日奖励，发送下一行：\n/签到"
            )
        )
    if action == "checkin":
        if data.get("state") not in {"credited", "already_checked_in"}:
            raise protocol_error()
        if data["state"] == "already_checked_in":
            return (
                "🍵 今天已经签到过啦，奖励不会重复发放。\n"
                + f"💰 当前可用额度：{money(data, 'balance')}\n"
                + "明天再来碰个好运，发送下一行即可（开头保留 /）：\n/签到"
            )
        return (
            f"✅ 签到成功，已发放 {money(data, 'reward')} 额度！\n"
            + f"💰 当前可用额度：{money(data, 'balance')}\n"
            + quip("checkin", context)
            + "\n明天继续签到，发送下一行即可（开头保留 /）：\n/签到"
        )
    if action == "robbery":
        outcome = data.get("outcome")
        if outcome not in {"success", "failure"}:
            raise protocol_error()
        transferred = money(data, "amount")
        cooldown = seconds(data, "robbery_cooldown_remaining_seconds")
        shield = seconds(data, "target_shield_remaining_seconds")
        zero = amount(data["amount"]) == 0
        if outcome == "success":
            headline = f"🗡️ 得手！打劫成功，从对方手里获得 {transferred} 额度。"
            ending = quip("success", context) if not zero else "本次转移为零，没有实际获得额度。"
            wait = (
                f"⏳ 你的打劫冷却：{duration(cooldown)}。" if cooldown else "⏳ 当前没有打劫冷却，别上头哦。"
            )
        else:
            headline = f"🚨 失手了！打劫失败，赔偿对方 {transferred} 额度。"
            ending = quip("failure", context) if not zero else "这次赔偿为零，钱包躲过一劫，算你走运 🍀"
            wait = (
                f"⚠️ 失手也算出手，{duration(cooldown)}后才能再抢。"
                if cooldown
                else "⏳ 本次没有打劫冷却，先稳稳心态。"
            )
        shield_text = f"🛡️ 对方获得 {duration(shield)}护盾。" if shield else "🛡️ 本次没有新增目标护盾。"
        return (
            headline
            + f"\n💰 你当前可用额度：{money(data, 'balance')}\n"
            + shield_text
            + "\n"
            + wait
            + "\n"
            + ending
        )
    raise protocol_error()


ERROR_MESSAGES = {
    "NOT_BOUND": "🧭 先认领小金库，再来玩！你还没有完成绑定。\n发送 /绑定 邮箱，并到站内由本人确认。",
    "BINDING_PENDING": "⏳ 申请在等你点头，绑定尚未确认。\n请登录自己的 Sub2API 账号，在「个人设置 → 管理 QQ 绑定」中手动输入确认码。",
    "ALREADY_BOUND": "🔗 已经牵好线啦，当前 QQ 身份已绑定。\n查看状态发送 /我的账号；换绑或解绑请在 Sub2API 账号设置中操作。",
    "TARGET_NOT_BOUND": "🧭 对方的小金库还没接通：对方没有完成绑定。\n先请对方绑定并到站内确认，再来互动吧。",
    "ADMIN_REQUIRED": "🔐 充值权限不足，这个补给按钮只交给管理员。\n须为 AstrBot 管理员，且 QQ 身份匹配 bridge 服务端充值白名单；群管理员不自动取得权限。",
    "APP_NOT_ALLOWED": "🔐 机器人应用还没拿到后端通行证。\n请管理员检查 bridge 令牌允许的 AppID，别急着重复充值。",
    "TARGET_NOT_FOUND": "🔎 没找到这个站内账号，请核对邮箱。\n本次未充值，检查拼写后再发起吧。",
    "ACCOUNT_TRANSACTION_PENDING": "🧾 相关账号还有一笔额度交易没核对完。\n请等待恢复或联系管理员对账，勿重复发起签到、充值或打劫。",
    "ADMIN_API_UNAVAILABLE": "🧾 管理员余额接口暂不可用，尚未确认记账结果。\n请联系管理员查询流水；先别重发，避免重复入账。",
    "SAME_ACCOUNT": "🪞 自己抢自己，左口袋倒右口袋可不算！\n不能打劫自己，也不能打劫绑定到同一 Sub2API 账号的身份。",
    "TARGET_SHIELDED": "🛡️ 对方的护盾还亮着，这一刀先收好。\n护盾期间不能打劫，等保护结束再来。",
    "ROBBERY_COOLDOWN": "☕ 先歇一会儿，你还处于打劫冷却中。\n冷却结束再出手，别急着连点。",
    "INSUFFICIENT_BALANCE": "👛 出手前得留点赔偿本钱！\n你的可用额度不足以覆盖最低失败赔偿，无法发起打劫；可以先查余额或签到。",
    "TARGET_EMPTY": "🍃 对方口袋比风还轻，已经没有可被打劫的额度。\n这次没有战利品可拿。",
    "ACCOUNT_BILLING_BUSY": "🧾 小金库正在结账，暂不能打劫。\n你或对方仍有模型请求、图片预留或待核实账单，需避免转走已用于消费的额度。请等待结算；长时间未恢复请联系管理员核对账单。",
    "ECONOMY_NOT_READY": "🚧 额度玩法的计费保护尚未完成上线验收。\n当前不会发放或转移额度，请管理员检查服务端经济功能状态。",
    "ACCOUNT_DISABLED": "🔒 账号目前不可用，先暂停一下。\n请到 Sub2API 查看账号状态，必要时联系站点管理员。",
    "FEATURE_DISABLED": "🚧 服务端尚未开放该功能，这个摊位暂时没开张。\n仅开启插件开关不会生效，请等待服务端验收并开放。",
    "RATE_LIMITED": "🐢 手速太快啦，操作过于频繁。\n请稍后再试；如果上一笔结果未确认，先查账号，别重复发起额度操作。",
    "POLICY_REJECTED": "🛠️ 玩法参数越过了服务端允许范围。\n请管理员检查奖励、概率、限额等配置，修好后再玩。",
    "IDEMPOTENCY_CONFLICT": "🛑 同一条消息出现了不同参数，已拒绝重复执行。\n请先查询账号状态，再联系管理员核对，别换消息重复充值。",
    "UNAUTHORIZED": "🔐 后端认证失败，通行证没通过校验。\n请管理员检查专用 bridge 令牌及权限；不要把令牌发到群里。",
    "INVALID_REQUEST": "🛠️ 后端没认出这份请求格式。\n请管理员核对 bridge 接口版本和身份字段。",
    "INVALID_IDEMPOTENCY_KEY": "🛑 请求的唯一标识没通过校验。\n请管理员检查接口兼容性，勿重复发起额度操作。",
    "UNSUPPORTED_VERSION": "🛠️ 插件和后端的协议版本没对上。\n请管理员核对并更新兼容版本，再继续使用。",
    "TLS_ERROR": "🔒 HTTPS 证书校验或 TLS 握手失败，已拒绝不安全连接。\n请管理员修复证书、域名或信任链，不要关闭证书校验；若此前已尝试操作，请先核对账户状态。",
    "HTTPS_REQUIRED": "🔒 后端要求安全的 HTTPS 连接。\n请管理员检查站点地址与反向代理配置，不要关闭安全校验。",
    "BRIDGE_NOT_INSTALLED": "🧭 没找到 QQ bridge 接口。\n可能尚未安装扩展，或接口地址不正确，请管理员检查部署。",
    "REDIRECT_REFUSED": "🔒 接口想带着令牌换个地址，已阻止转发。\n请管理员核对正确地址，不跟随不明重定向。",
    "INVALID_RESPONSE": "🧾 后端回执格式异常，结果尚未确认。\n请先用 /我的账号 核对，勿重复签到、充值或打劫；必要时联系管理员查流水。",
    "UNAVAILABLE": "📡 后端暂时不可用，但操作可能已执行。\n请先用 /我的账号 核对，勿重新发送签到、充值或打劫，避免重复记账。",
    "TEMPORARILY_UNAVAILABLE": "📡 后端暂时无法完成处理，结果尚未确认。\n请先查询账号或联系管理员核对流水，勿重复发起额度操作。",
}


def render_error(error: BridgeError, *, action: str = "") -> str:
    message = ERROR_MESSAGES.get(
        error.code, "🧾 请求未完成，结果尚未确认。\n请查询账号状态或联系管理员核对，勿重复发起额度操作。"
    )
    if error.code == "ADMIN_REQUIRED" and action in {"reset_shields", "reset_cooldowns"}:
        message = (
            "🔐 重置权限不足，全站开关不能随便碰。\n须为 AstrBot 管理员，并匹配 bridge 服务端精确身份白名单。"
        )
    if error.retry_after:
        message += f"\n⏳ 剩余等待：{duration(error.retry_after)}。"
    return message


def render_help(settings: Settings | None) -> str:
    message = (
        "🎮 小金库互动指南\n"
        "🔗 绑定账号：/绑定 你的邮箱\n"
        "登录站内「个人设置 → 管理 QQ 绑定」由本人手动确认，才算绑定生效。\n"
        "🎁 每日签到：/签到\n"
        "🗡️ 群内互动：/打劫 @一位已绑定群友\n"
        "💰 查看账号：/我的账号（也可用 /绑定状态、/余额、/额度）\n"
        "🛡️ 查看保护：/护盾 或 /冷却\n"
        "🏆 我的打劫战绩：/战绩（同一站内账号合并统计）\n"
        "🧹 管理员全站操作：/重置护盾、/重置出手\n"
        "🔐 管理员补给：/充值 @已绑定群友 金额，或 /充值 站内邮箱 金额\n"
        "充值仅限 AstrBot 管理员，并须匹配服务端白名单。\n"
        "⚠️ 成功拿额度，失败要赔偿；别把全部家当都当本钱。"
    )
    if settings:
        checkin, robbery = settings.policy["checkin"], settings.policy["robbery"]
        probability = decimal_text(amount(robbery["success_probability"]) * 100)
        if "." in probability:
            probability = probability.rstrip("0").rstrip(".")
        message += (
            "\n\n📋 当前插件规则（不代表服务端已开放对应功能）"
            f"\n🎁 签到：{'开启' if checkin['enabled'] else '关闭'}，奖励 {money(checkin['reward'], 'min')}～{money(checkin['reward'], 'max')} 额度；按 {checkin['timezone']} 自然日。"
            f"\n🗡️ 打劫：{'开启' if robbery['enabled'] else '关闭'}，成功概率 {probability}%。"
            f"\n成功额度 {money(robbery['success_amount'], 'min')}～{money(robbery['success_amount'], 'max')}；失败赔偿 {money(robbery['failure_amount'], 'min')}～{money(robbery['failure_amount'], 'max')}。"
            f"\n⏳ 打劫冷却 {duration(robbery['cooldown_seconds'])}；🛡️ 目标护盾 {duration(robbery['shield_seconds'])}。"
            f"\n🔐 管理员充值：{'开启' if settings.recharge_enabled else '关闭'}。"
            "\n每次有效打劫无论成败均进入冷却，并给目标护盾；主动打劫会解除自己的护盾。"
            "实际转账不超过付款方可用额度，最终以服务端校验为准。"
        )
    return message + (
        "\n\n🔒 群内原消息中的邮箱、确认码和余额对群友可见，回复脱敏不等于撤回原消息；群聊与私聊身份需分别验证。"
        "\n所有数额沿用站内额度单位，不是人民币换算。交易结果待确认时，先对账，别重复发指令。"
    )


def count_field(data: dict, key: str) -> int:
    value = data.get(key)
    if type(value) is not int or not 0 <= value <= 2147483647:
        raise protocol_error()
    return value


def stat_amount(value: object, *, signed: bool = False) -> str:
    pattern = (
        r"-?(?:0|[1-9][0-9]{0,19})(?:\.[0-9]{1,6})?" if signed else r"(?:0|[1-9][0-9]{0,19})(?:\.[0-9]{1,6})?"
    )
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise protocol_error()
    number = Decimal(value)
    whole, _, fraction = format(abs(number), "f").partition(".")
    prefix = ("-" if number < 0 else "+" if number > 0 else "") if signed else ""
    return prefix + whole + "." + fraction.rstrip("0").ljust(2, "0")


def render_stats(data: dict) -> str:
    if data.get("state") != "stats" or data.get("history") != "admin_api":
        raise protocol_error()
    attacks, wins, losses = (count_field(data, k) for k in ("attacks", "wins", "losses"))
    defenses = count_field(data, "defenses")
    pending, excluded = (count_field(data, k) for k in ("pending", "unsettled_excluded"))
    if wins + losses != attacks:
        raise protocol_error()
    amounts = {
        key: stat_amount(data.get(key), signed=key == "net")
        for key in ("earned", "compensation", "defense_loss", "defense_income", "net")
    }
    if Decimal(data["net"]) != Decimal(data["earned"]) - Decimal(data["compensation"]) + Decimal(
        data["defense_income"]
    ) - Decimal(data["defense_loss"]):
        raise protocol_error()
    timezone = data.get("timezone")
    try:
        if (
            not isinstance(timezone, str)
            or len(timezone) > 128
            or not re.fullmatch(r"[A-Za-z0-9_+/-]+", timezone)
        ):
            raise ValueError()
        ZoneInfo(timezone)
    except (ValueError, OSError, ZoneInfoNotFoundError):
        raise protocol_error() from None
    recent = data.get("recent")
    if not isinstance(recent, list) or len(recent) > 5 or len(recent) > attacks + defenses:
        raise protocol_error()
    rate = format(Decimal(wins) * 100 / attacks, ".1f") + "%" if attacks else "暂无"
    message = (
        "🏆 你的打劫战绩册\n"
        f"🗡️ 主动出手：{attacks} 次｜得手 {wins} 次｜失手 {losses} 次\n"
        f"🎯 得手率：{rate}\n"
        f"💰 主动抢得：{amounts['earned']}｜失手赔偿：{amounts['compensation']}\n"
        f"🛡️ 被出手：{defenses} 次｜被抢走：{amounts['defense_loss']}｜收到赔偿：{amounts['defense_income']}\n"
        f"📊 打劫净变化：{amounts['net']} 额度（不含签到、充值与消费）\n"
    )
    if recent:
        message += f"\n📜 最近 {len(recent)} 笔已结算（{timezone}）"
        for item in recent:
            if (
                not isinstance(item, dict)
                or item.get("role") not in {"attack", "defense"}
                or item.get("outcome") not in {"success", "failure"}
            ):
                raise protocol_error()
            at = item.get("at")
            if not isinstance(at, str) or not re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}", at
            ):
                raise protocol_error()
            try:
                datetime.strptime(at, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                raise protocol_error() from None
            delta = stat_amount(item.get("delta"), signed=True)
            label = {
                ("attack", "success"): "🗡️ 得手",
                ("attack", "failure"): "🚨 失手",
                ("defense", "success"): "💸 被抢",
                ("defense", "failure"): "🛡️ 收到赔偿",
            }[(item["role"], item["outcome"])]
            positive = (item["role"] == "attack") == (item["outcome"] == "success")
            if (positive and Decimal(item["delta"]) < 0) or (not positive and Decimal(item["delta"]) > 0):
                raise protocol_error()
            message += f"\n{at} · {label} · {delta}"
    else:
        message += "\n📖 还没有已结算的打劫记录，战绩册还是崭新的。"
    message += f"\n\n🧾 待核对 {pending} 笔；失败终止/已退款 {excluded} 笔，均不计胜负和收益。"
    return (
        message
        + "\n按当前绑定站内账号统计管理员 API 模式以来的战绩，跨群与私聊合并；重置护盾或出手不会清空战绩。"
    )
