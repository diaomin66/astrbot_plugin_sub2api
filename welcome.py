"""QQ's authenticated GROUP_MEMBER_ADD event, never first-message guessing."""

import asyncio
import hashlib
import re
import sqlite3
import time
from pathlib import Path
from string import Formatter

from .config import ConfigError

MEMBER_INTENT = 1 << 24
DEFAULT_TEMPLATE = (
    "🎉 欢迎 {at} 加入小金库！\n"
    "🌐 站点：{site_url}\n"
    "🔗 先发送 /绑定 你的站内邮箱，再到站内亲自确认。\n"
    "🎁 首次确认绑定的站内账号可获 2 美元新人礼；每个账号仅一次，历史绑定不补发。\n"
    "📅 每天 /签到 领惊喜，🗡️ /打劫 @群友 试手气，📊 /战绩 看战果！\n"
    "输入 /额度帮助 查看玩法，祝你玩得开心 ✨"
)
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def validate_template(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1600:
        raise ConfigError("welcome.template 必须为 1 至 1600 字符的欢迎语")
    try:
        for _, field, spec, conversion in Formatter().parse(value):
            if field is not None and (field not in {"at", "site_url"} or spec or conversion):
                raise ValueError()
    except ValueError as exc:
        raise ConfigError("欢迎语仅支持 {at}、{site_url} 占位符；字面大括号使用 {{ 和 }}") from exc
    return value.strip()


class WelcomeSender:
    def __init__(self, settings, path: Path, logger):
        self.settings = settings
        self.path = path
        self.logger = logger
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS welcomes (key TEXT PRIMARY KEY, created INTEGER NOT NULL, state TEXT NOT NULL)"
            )

    async def handle(self, payload, api):
        if not self.settings.welcome_enabled or not isinstance(payload, dict):
            return False
        if payload.get("t") != "GROUP_MEMBER_ADD" or payload.get("op") != 0:
            return False
        data = payload.get("d")
        if not isinstance(data, dict):
            return False
        group, member, timestamp = (data.get(k) for k in ("group_openid", "member_openid", "timestamp"))
        if any(not isinstance(v, str) or not IDENTIFIER.fullmatch(v) for v in (group, member)):
            return False
        now = int(time.time())
        if type(timestamp) is not int or not now - 300 <= timestamp <= now + 60:
            return False
        text = self.settings.welcome_template.format(
            at=f'<qqbot-at-user id="{member}" />', site_url=self.settings.base_url
        )
        if len(text) > 2000:
            self.logger.warning("Sub2API welcome rendered message exceeds limit")
            return False
        key = hashlib.sha256(
            repr((self.settings.app_id, self.settings.platform_id, group, member, timestamp)).encode()
        ).hexdigest()
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM welcomes WHERE created < ?", (now - 604800,))
            inserted = db.execute(
                "INSERT OR IGNORE INTO welcomes VALUES (?, ?, 'sending')", (key, now)
            ).rowcount
        if not inserted:
            return False
        # Reserve durably BEFORE sending: never resend an ambiguous timeout after reload.
        kwargs = dict(group_openid=group, msg_type=0, content=text, msg_seq=1)
        event_id = payload.get("id")
        if isinstance(event_id, str) and re.fullmatch(r"[A-Za-z0-9_:/.-]{1,256}", event_id):
            kwargs["event_id"] = event_id
        try:
            await api.post_group_message(**kwargs)
            state = "sent"
        except Exception as exc:
            self.logger.warning(
                "Sub2API welcome send unconfirmed: %s; no automatic resend", type(exc).__name__
            )
            state = "unconfirmed"
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE welcomes SET state=? WHERE key=?", (state, key))
        return state == "sent"


class WelcomeListener:
    """Per-client parser bridge for old botpy releases; no global SDK patches."""

    def __init__(self, context, settings, sender, logger):
        self.context, self.settings, self.sender, self.logger = context, settings, sender, logger
        self.task = None
        self.sends = set()
        self.attachments = []
        self.clients = []
        self.ready_sessions = set()

    def start(self):
        self.task = asyncio.create_task(self.watch())

    async def watch(self):
        while True:
            try:
                adapter = self.context.get_platform_inst(self.settings.platform_id)
                if (
                    adapter is not None
                    and adapter.meta().name == "qq_official"
                    and str(getattr(adapter, "appid", "")) == self.settings.app_id
                ):
                    await self.attach(adapter.client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning("Sub2API welcome hook deferred: %s", type(exc).__name__)
            await asyncio.sleep(2)

    async def attach(self, client):
        if not any(item[0] is client for item in self.clients):
            self.clients.append((client, client.intents))
            client.intents |= MEMBER_INTENT
        connection = getattr(client, "_connection", None)
        if connection is None:
            return
        if not any(item[0] is connection for item in self.attachments):
            previous = connection.parser.get("group_member_add")

            def parser(payload):
                if previous is not None:
                    previous(payload)
                task = asyncio.create_task(self.sender.handle(payload, client.api))
                self.sends.add(task)
                task.add_done_callback(self.completed)

            connection.parser["group_member_add"] = parser
            self.attachments.append((connection, previous, parser))
            self.logger.info("Sub2API welcome GROUP_MEMBER_ADD parser installed")
        # Fresh authentication is necessary when adding an intent to an existing session.
        # The installed AstrBot ManagedBotWebSocket.close resets session_id on reconnect.
        for websocket in list(getattr(client, "_active_websockets", ())):
            session = websocket._session
            if not session["intent"] & MEMBER_INTENT:
                session["intent"] |= MEMBER_INTENT
                await websocket.close()
                self.logger.info("Sub2API welcome requested QQ intent re-identification")
            elif (
                session.get("session_id")
                and getattr(websocket, "_can_reconnect", False)
                and getattr(websocket, "_conn", None) is not None
                and not websocket._conn.closed
            ):
                ready = (id(client), session["session_id"])
                if ready not in self.ready_sessions:
                    self.ready_sessions.add(ready)
                    self.logger.info("Sub2API welcome QQ session ready with GROUP_MEMBER_EVENT intent")
        for session in getattr(connection, "_session_list", ()):
            if not session["intent"] & MEMBER_INTENT:
                session.update(intent=session["intent"] | MEMBER_INTENT, session_id="", last_seq=0)

    def completed(self, task):
        self.sends.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.logger.warning("Sub2API welcome handler failed: %s", type(task.exception()).__name__)

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for connection, previous, parser in self.attachments:
            if connection.parser.get("group_member_add") is parser:
                if previous is None:
                    connection.parser.pop("group_member_add", None)
                else:
                    connection.parser["group_member_add"] = previous
        for client, original in self.clients:
            client.intents = (client.intents & ~MEMBER_INTENT) | (original & MEMBER_INTENT)
        if self.sends:
            await asyncio.gather(*list(self.sends), return_exceptions=True)
