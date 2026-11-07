from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At
from astrbot.api.star import Context, Star

from .bridge import BridgeClient, BridgeError
from .config import ConfigError, Settings
from .identity import OFFICIAL_PLATFORMS, InputError, binding_email, identify, recharge_input, robbery_target
from .presentation import ERROR_MESSAGES, duration, render, render_error


class Sub2APIPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.settings = None
        self.client = None
        try:
            self.settings = Settings.from_mapping(config)
            self.client = BridgeClient(self.settings)
        except ConfigError as exc:
            # Validation messages name fields only; do not log tokens, emails or payloads.
            logger.warning("Sub2API plugin configuration is incomplete: %s", exc)

    def accepts(self, event: AstrMessageEvent) -> bool:
        if event.get_platform_name() not in OFFICIAL_PLATFORMS:
            return False
        return self.settings is None or event.get_platform_id() == self.settings.platform_id

    async def execute(self, action: str, event: AstrMessageEvent) -> str:
        if self.settings is None or self.client is None:
            return "插件尚未正确配置。请管理员在插件面板填写 Sub2API 地址、bridge 令牌、QQ AppID 和平台 ID，然后重载插件。"
        try:
            identity = identify(event, self.settings)
            payload = {"actor": identity.actor, "context": identity.context, "policy": self.settings.policy}
            if action == "recharge":
                if not callable(getattr(event, "is_admin", None)) or event.is_admin() is not True:
                    return "充值仅允许 AstrBot 管理员操作，群管理员身份不等于机器人管理员。"
                if not self.settings.recharge_enabled:
                    return "管理员充值功能已关闭。请先配置 bridge 服务端管理员白名单，再开启插件充值开关。"
                payload.update(recharge_input(event, identity, At))
            elif action == "bind":
                if identity.is_group and not self.settings.allow_group_binding:
                    return "管理员已关闭群内邮箱绑定。请在站内验证群身份，或联系管理员；私聊绑定不会自动关联群身份。"
                payload["email"] = binding_email(event.get_message_str())
            elif action == "checkin":
                if not self.settings.policy["checkin"]["enabled"]:
                    return "签到功能已关闭。请等待服务端完成资金安全验收，并由管理员确认开放。绑定和账号查询不受影响。"
            elif action == "robbery":
                if not self.settings.policy["robbery"]["enabled"]:
                    return "打劫功能已关闭。请等待服务端完成资金安全验收，并由管理员确认开放。绑定和账号查询不受影响。"
                payload["target"] = robbery_target(event, identity, At)
            result = await self.client.request(action, payload)
            return render(action, result)
        except InputError as exc:
            return str(exc)
        except BridgeError as exc:
            # Only fixed error codes are logged; never echo backend response bodies.
            safe_code = exc.code if exc.code in ERROR_MESSAGES else "UNKNOWN_BACKEND_ERROR"
            logger.warning("Sub2API bridge action=%s code=%s", action, safe_code)
            return render_error(exc)
        except Exception as exc:
            logger.error("Sub2API handler failed: %s", type(exc).__name__)
            return "处理异常，结果尚未确认。请先查询账户状态，不要重复打劫；请管理员检查插件日志。"

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

    @filter.command("额度帮助", alias={"sub2api帮助"})
    async def help_command(self, event: AstrMessageEvent):
        if not self.accepts(event):
            return
        event.stop_event()
        message = (
            "Sub2API 账号互动\n"
            "/绑定 邮箱：申请绑定，登录站内「个人设置 → 管理 QQ 绑定」手动确认\n"
            "/签到：功能开放后每日领取一次随机额度\n"
            "/打劫 @群友：功能开放且双方已绑定后参与，成功获得额度，失败赔偿对方\n"
            "/我的账号：绑定、额度、签到及冷却信息\n"
            "/护盾 或 /冷却：查询保护和打劫剩余时间\n"
            "/充值 @已绑定群友 金额 或 /充值 站内邮箱 金额：仅机器人管理员，且须列入服务端白名单\n"
            "群内发送邮箱/查询余额会被群友看到。群聊、私聊身份需分别验证。"
        )
        if self.settings:
            checkin = self.settings.policy["checkin"]
            robbery = self.settings.policy["robbery"]
            message += (
                "\n以下仅为插件配置，不代表服务端已开放对应功能。"
                f"\n签到：{'开启' if checkin['enabled'] else '关闭'}，"
                f"奖励 {checkin['reward']['min']}～{checkin['reward']['max']}，按 {checkin['timezone']} 自然日。"
                f"\n打劫：{'开启' if robbery['enabled'] else '关闭'}，成功概率 {float(robbery['success_probability']) * 100:g}%。"
                f"\n成功额度 {robbery['success_amount']['min']}～{robbery['success_amount']['max']}；"
                f"失败赔偿 {robbery['failure_amount']['min']}～{robbery['failure_amount']['max']}。"
                f"\n打劫冷却 {duration(robbery['cooldown_seconds'])}；目标护盾 {duration(robbery['shield_seconds'])}。"
                "每次有效打劫无论成败均进入冷却，并给目标护盾；主动打劫会解除自己的护盾。"
                "实际转账不超过付款方可用额度，最终以服务端校验规则为准。"
            )
        yield event.plain_result(message).stop_event()

    async def terminate(self):
        if self.client is not None:
            await self.client.close()
