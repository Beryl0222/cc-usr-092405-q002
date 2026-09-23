"""测试共享工具：固定时钟的 App 与消息投递简写。"""

from support.app import App

NOW = "2026-09-22T08:00"


def make_app(db=":memory:", now=NOW):
    return App(db, now_fn=lambda: now)


def send(app, message_id, command_type, **payload):
    return app.commands.handle(
        {"message_id": message_id, "type": command_type, "payload": payload}
    )


def assignment_resources(segment_detail, kind=None):
    return [
        a["resource_id"]
        for a in segment_detail["assignments"]
        if (kind is None or a["resource_kind"] == kind) and a["status"] != "released"
    ]
