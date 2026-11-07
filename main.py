from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At
from astrbot.api.star import Context, Star

from .bridge import BridgeClient, BridgeError, idempotency_key
from .config import ConfigError, Settings
from .identity import OFFICIAL_PLATFORMS, InputError, binding_email, identify, recharge_input, robbery_target
from .presentation import ERROR_MESSAGES, ReplyContext, render, render_error, render_help


class Sub2APIPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.settings = None
        self.client = None
        self.welcome_listener = None
        try:
            self.settings = Settings.from_mapping(config)
            self.client = BridgeClient(self.settings)
        except ConfigError as exc:
            # Validation messages name fields only; do not log tokens, emails or payloads.
            logger.warning("Sub2API plugin configuration is incomplete: %s", exc)

    async def initialize(self):
        if self.settings is None or not self.settings.welcome_enabled:
            return
        from astrbot.api.star import StarTools

        from .welcome import WelcomeListener, WelcomeSender

        sender = WelcomeSender(
            self.settings,
            StarTools.get_data_dir("astrbot_plugin_sub2api") / "welcomes.sqlite3",
            logger,
        )
        self.welcome_listener = WelcomeListener(self.context, self.settings, sender, logger)
        self.welcome_listener.start()

    def accepts(self, event: AstrMessageEvent) -> bool:
        if event.get_platform_name() not in OFFICIAL_PLATFORMS:
            return False
        return self.settings is None or event.get_platform_id() == self.settings.platform_id

    async def execute(self, action: str, event: AstrMessageEvent) -> str:
        if self.settings is None or self.client is None:
            return "🛠️ 小金库还没接好，插件尚未正确配置。\n请管理员填写 Sub2API 地址、bridge 令牌、QQ AppID 和平台 ID，再重载插件；令牌不要发到群里。"
        try:
            identity = identify(event, self.settings)
            payload = {"actor": identity.actor, "context": identity.context, "policy": self.settings.policy}
            if action in {"reset_shields", "reset_cooldowns"}:
                if not callable(getattr(event, "is_admin", None)) or event.is_admin() is not True:
                    return "🔐 全站重置仅允许 AstrBot 管理员操作，群管理员不自动取得权限。"
                command = "重置护盾" if action == "reset_shields" else "重置出手"
                if len(event.get_message_str().strip().split()) != 1:
                    return f"📝 此指令作用于全站所有账号，不接收目标或其他参数。用法：/{command}"
            elif action == "stats":
                if len(event.get_message_str().strip().split()) != 1:
                    return "🔎 战绩仅查看当前绑定账号，不接收其他用户参数。直接发送 /战绩 即可。"
            elif action == "recharge":
                if not callable(getattr(event, "is_admin", None)) or event.is_admin() is not True:
                    return "🔐 补给按钮有权限锁：充值仅允许 AstrBot 管理员操作。\n群管理员身份不等于机器人管理员。"
                if not self.settings.recharge_enabled:
                    return "🚧 管理员充值功能已关闭，补给站暂未营业。\n请先配置 bridge 服务端管理员白名单，再开启插件充值开关。"
                payload.update(recharge_input(event, identity, At))
            elif action == "bind":
                if identity.is_group and not self.settings.allow_group_binding:
                    return "🔒 管理员已关闭群内邮箱绑定，先别把邮箱发到群里。\n请联系管理员了解群身份绑定方式；私聊绑定不会自动关联群身份。"
                payload["email"] = binding_email(event.get_message_str())
            elif action == "checkin":
                if not self.settings.policy["checkin"]["enabled"]:
                    return "🎁 签到功能已关闭，今日奖励摊位还没开张。\n请等待服务端验收并由管理员开放；绑定和账号查询不受影响。"
            elif action == "robbery":
                if not self.settings.policy["robbery"]["enabled"]:
                    return "🗡️ 打劫功能已关闭，这把刀先收好。\n请等待服务端验收并由管理员开放；绑定和账号查询不受影响。"
                payload["target"] = robbery_target(event, identity, At)
            result = await self.client.request(action, payload)
            reply_context = ReplyContext(
                site_url=self.settings.base_url,
                email=payload.get("email", "") or payload.get("recipient", {}).get("email", ""),
                scope=identity.actor["scope"],
                flavor_key=idempotency_key(action, payload),
            )
            return render(action, result, context=reply_context)
        except InputError as exc:
            return str(exc)
        except BridgeError as exc:
            # Only fixed error codes are logged; never echo backend response bodies.
            safe_code = exc.code if exc.code in ERROR_MESSAGES else "UNKNOWN_BACKEND_ERROR"
            logger.warning("Sub2API bridge action=%s code=%s", action, safe_code)
            return render_error(exc, action=action)
        except Exception as exc:
            logger.error("Sub2API handler failed: %s", type(exc).__name__)
            return "🧾 处理遇到异常，结果尚未确认。\n请先用 /我的账号 核对，勿重复签到、充值或打劫；请管理员检查插件日志和流水。"

    @filter.command("绑定")
    async def bind(self, event: AstrMessageEvent):
        """绑定 Sub2API 邮箱，等待站内确认。"""
        if not self.accepts(event):
            return
        event.stop_event()
        yield event.plain_result(await self.execute("bind", event)).stop_event()

    @filter.command("签到")
    async def checkin(self, event: AstrMessageEvent):
        """领取每日随机额度。"""
        if not self.accepts(event):
            return
        event.stop_event()
        yield event.plain_result(await self.execute("checkin", event)).stop_event()

    @filter.command("打劫")
    async def robbery(self, event: AstrMessageEvent):
        """打劫一位已绑定的群友，失败可能赔偿。"""
        if not self.accepts(event):
            return
        event.stop_event()
        yield event.plain_result(await self.execute("robbery", event)).stop_event()

    @filter.command("我的账号", alias={"绑定状态", "余额", "额度", "护盾", "冷却"})
    async def account(self, event: AstrMessageEvent):
        """查询绑定、可用额度、签到、护盾与冷却。"""
        if not self.accepts(event):
            return
        event.stop_event()
        yield event.plain_result(await self.execute("account", event)).stop_event()

    @filter.command("充值")
    async def recharge(self, event: AstrMessageEvent):
        """管理员充值：/充值 @已绑定群友或站内邮箱 金额。"""
        if not self.accepts(event):
            return
        event.stop_event()
        yield event.plain_result(await self.execute("recharge", event)).stop_event()

    @filter.command("重置护盾")
    async def reset_shields(self, event: AstrMessageEvent):
        """管理员清除全站所有当前护盾，不改变余额。"""
        if not self.accepts(event):
            return
        event.stop_event()
        yield event.plain_result(await self.execute("reset_shields", event)).stop_event()

    @filter.command("重置出手")
    async def reset_cooldowns(self, event: AstrMessageEvent):
        """管理员清除全站出手冷却，保留护盾与其他风控。"""
        if not self.accepts(event):
            return
        event.stop_event()
        yield event.plain_result(await self.execute("reset_cooldowns", event)).stop_event()

    @filter.command("战绩")
    async def stats(self, event: AstrMessageEvent):
        """查看当前绑定站内账号的已结算打劫战绩。"""
        if not self.accepts(event):
            return
        event.stop_event()
        yield event.plain_result(await self.execute("stats", event)).stop_event()

    @filter.command("额度帮助", alias={"sub2api帮助"})
    async def help_command(self, event: AstrMessageEvent):
        if not self.accepts(event):
            return
        event.stop_event()
        yield event.plain_result(render_help(self.settings)).stop_event()

    async def terminate(self):
        if self.welcome_listener is not None:
            await self.welcome_listener.stop()
        if self.client is not None:
            await self.client.close()
