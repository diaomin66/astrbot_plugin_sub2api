from types import SimpleNamespace

from ..config import Settings


def config():
    return {
        "connection": {
            "base_url": "https://sub2api.example.test",
            "bridge_token": "test-only-not-a-real-service-token-123456",
            "qq_app_id": "123456",
            "platform_id": "qq-main",
            "token_env": "",
        }
    }


def settings():
    return Settings.from_mapping(config())


class At:
    def __init__(self, qq):
        self.qq = qq


class Result:
    def __init__(self, text):
        self.text = text
        self.stopped = False

    def stop_event(self):
        self.stopped = True
        return self


class Event:
    def __init__(self, text="签到", sender="member-a", group="group-a", mentions=None, message_id="msg-1"):
        self.text = text
        self.sender = sender
        self.group = group
        self.platform = "qq_official"
        self.platform_id = "qq-main"
        self.stopped = False
        self.components = [At("bot-id")]
        author = {"member_openid": sender} if group else {"user_openid": sender}
        raw = {"id": message_id, "author": author, "mentions": mentions or []}
        if group:
            raw["group_openid"] = group
        self.message_obj = SimpleNamespace(raw_message=raw, message_id=message_id)

    def get_platform_name(self):
        return self.platform

    def get_platform_id(self):
        return self.platform_id

    def get_sender_id(self):
        return self.sender

    def get_group_id(self):
        return self.group

    def get_self_id(self):
        return "bot-id"

    def is_private_chat(self):
        return not self.group

    def get_messages(self):
        return self.components

    def get_message_str(self):
        return self.text

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return Result(text)
