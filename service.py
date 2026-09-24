"""残特奥赛事保障运行入口。

路由：
  GET  /health                 健康检查（稳定服务身份）
  POST /commands               命令消息入口（message_id 幂等）
  GET  /public/schedule        公众赛程（脱敏）
  GET  /internal/changes       改派审计（每次改派与实际影响）
  GET  /internal/notifications 通知记录（紧急占用须通知原负责人）
  GET  /internal/gaps          未解决的保障空档
  GET  /internal/handoffs      跨城交接状态
  GET  /internal/segments/<id> 行程段明细（含资源承诺）
  GET  /internal/utilization   车辆承诺时间线（核对无双占）
  GET  /internal/continuity    跨城交接断档检测
  GET  /internal/conflicts     消息编号复用冲突（首次与冲突摘要，供交接追踪）
"""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from support.app import App, SERVICE_ID, SERVICE_NAME
from support.catalog import ValidationError
from support.messages import MessageConflictError

DEFAULT_DB = os.environ.get("PARA_EVENT_DB", os.path.join("data", "para_event.db"))


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """HTTP 适配；业务逻辑全部在 support 包内。"""

    app = None  # 由 bind() 注入；懒加载默认实例

    def _app(self):
        # 懒加载写到具体子类上；bind() 注入的 app 优先
        cls = type(self)
        if cls.__dict__.get("app") is None:
            cls.app = App(DEFAULT_DB)
        return cls.app

    @classmethod
    def bind(cls, app):
        """返回绑定指定 App 的 handler 子类（测试用）。"""
        bound = type("BoundHandler", (cls,), {"app": app})
        return bound

    # ---------- 基础 ----------

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError(f"请求体不是合法 JSON: {exc}")
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return data

    def log_message(self, *_args):
        return

    # ---------- 路由 ----------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        app = self._app()
        try:
            if path == "/health":
                self._send_json(health_payload())
            elif path == "/public/schedule":
                self._send_json(app.views.public_schedule(city=query.get("city")))
            elif path == "/internal/changes":
                self._send_json(app.views.change_log(limit=int(query.get("limit", 100))))
            elif path == "/internal/notifications":
                self._send_json(app.views.notifications())
            elif path == "/internal/gaps":
                self._send_json(app.views.open_gaps(city=query.get("city")))
            elif path == "/internal/handoffs":
                self._send_json(app.views.handoffs(status=query.get("status")))
            elif path == "/internal/utilization":
                self._send_json(app.views.resource_utilization(city=query.get("city")))
            elif path == "/internal/continuity":
                self._send_json({"broken_handoffs": app.engine.handoff_continuity()})
            elif path == "/internal/conflicts":
                self._send_json(
                    app.views.command_conflicts(message_id=query.get("message_id")))
            elif path.startswith("/internal/segments/"):
                segment_id = path.rsplit("/", 1)[1]
                detail = app.views.segment_detail(segment_id)
                if detail is None:
                    self._send_json({"error": f"未知行程段: {segment_id}"}, status=404)
                else:
                    self._send_json(detail)
            else:
                self.send_error(404)
        except ValidationError as exc:
            self._send_json({"error": str(exc)}, status=400)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        app = self._app()
        try:
            data = self._read_json()
            if path == "/commands":
                self._send_json(app.commands.handle(data))
            else:
                self.send_error(404)
        except MessageConflictError as exc:
            # 编号复用冲突：409 + 可核验的双方摘要；新命令未执行
            self._send_json(exc.payload(), status=409)
        except ValidationError as exc:
            self._send_json({"error": str(exc)}, status=400)


def seed_from_file(app, path):
    """按 fixtures 种子文件批量投递命令消息。"""
    with open(path, encoding="utf-8") as handle:
        messages = json.load(handle)
    results = []
    for index, message in enumerate(messages):
        message.setdefault("message_id", f"seed-{index + 1}")
        results.append(app.commands.handle(message))
    return results


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--seed", help="从种子文件批量导入命令消息")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        app = App(":memory:")
        assert app.views.public_schedule() == {"schedule": []}
        app.close()
        print("基础检查通过")
        return
    app = App(args.db)
    if args.seed:
        seed_from_file(app, args.seed)
        print(f"种子导入完成: {args.seed}")
    handler_cls = Handler.bind(app)
    print(f"服务启动: db={args.db} port={args.port}")
    ThreadingHTTPServer(("0.0.0.0", args.port), handler_cls).serve_forever()


if __name__ == "__main__":
    main()
