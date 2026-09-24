"""HTTP 层契约：健康检查、命令入口、公众/内部视图路由与错误码。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from service import Handler, health_payload
from tests.helpers import make_app, send


class HttpContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = make_app()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler.bind(cls.app))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.app.close()

    def _get(self, path):
        with urlopen(f"{self.base_url}{path}", timeout=2) as response:
            return response.status, json.load(response)

    def _post(self, path, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = Request(f"{self.base_url}{path}", data=data,
                      headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=2) as response:
            return response.status, json.load(response)

    def test_health_identity_unchanged(self):
        status, payload = self._get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload, health_payload())

    def test_command_roundtrip_and_duplicate(self):
        message = {"message_id": "HTTP-1", "type": "register_team",
                   "payload": {"team_id": "T1", "name": "一队", "city": "城市A",
                               "sport": "篮球"}}
        status, first = self._post("/commands", message)
        self.assertEqual(status, 200)
        self.assertEqual(first["result"], {"team_id": "T1"})
        _, second = self._post("/commands", message)
        self.assertTrue(second["deduplicated"])

    def test_message_id_reuse_conflict_is_409(self):
        message = {"message_id": "HTTP-C1", "type": "register_team",
                   "payload": {"team_id": "TC1", "name": "一队", "city": "城市A",
                               "sport": "篮球"}}
        status, _ = self._post("/commands", message)
        self.assertEqual(status, 200)
        # 同一编号配给另一条命令：409 冲突，携带首次/冲突摘要
        changed = {"message_id": "HTTP-C1", "type": "register_team",
                   "payload": {"team_id": "TC1", "name": "改名", "city": "城市A",
                               "sport": "篮球"}}
        with self.assertRaises(HTTPError) as ctx:
            self._post("/commands", changed)
        self.assertEqual(ctx.exception.code, 409)
        body = json.load(ctx.exception)
        ctx.exception.close()
        self.assertEqual(body["first_type"], "register_team")
        self.assertIn("conflict_payload_summary", body)
        # 冲突台账可经内部端点回看（交接追踪）
        status, conflicts = self._get("/internal/conflicts")
        self.assertEqual(status, 200)
        self.assertTrue(any(c["message_id"] == "HTTP-C1"
                            for c in conflicts["conflicts"]))
        # 被冲突的命令没有生效
        self.assertEqual(self.app.store.get_team("TC1")["name"], "一队")

    def test_validation_error_is_400_not_crash(self):
        with self.assertRaises(HTTPError) as ctx:
            self._post("/commands", {"message_id": "HTTP-BAD",
                                     "type": "register_person",
                                     "payload": {"person_id": "X", "诊断": "脊髓损伤"}})
        self.assertEqual(ctx.exception.code, 400)
        payload = json.load(ctx.exception)
        self.assertIn("诊断", payload["error"])
        ctx.exception.close()

    def test_public_schedule_route(self):
        send(self.app, "HTTP-V", "register_venue", venue_id="V1", name="A馆",
             city="城市A", features=[])
        send(self.app, "HTTP-E2", "register_event", event_id="E1", venue_id="V1",
             city="城市A", sport="田径", stage="competition", title="田径决赛",
             start="2026-09-23T09:00", end="2026-09-23T11:00")
        status, payload = self._get(f"/public/schedule?city={quote('城市A')}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["schedule"][0]["title"], "田径决赛")

    def test_internal_routes_exist(self):
        for path in ("/internal/changes", "/internal/notifications",
                     "/internal/gaps", "/internal/handoffs",
                     "/internal/utilization", "/internal/continuity"):
            status, _ = self._get(path)
            self.assertEqual(status, 200, path)

    def test_unknown_segment_is_404(self):
        with self.assertRaises(HTTPError) as ctx:
            self._get("/internal/segments/NOPE")
        self.assertEqual(ctx.exception.code, 404)
        ctx.exception.close()

    def test_unknown_route_and_method(self):
        with self.assertRaises(HTTPError) as ctx:
            self._get("/nope")
        self.assertEqual(ctx.exception.code, 404)
        ctx.exception.close()
        with self.assertRaises(HTTPError) as ctx:
            self._post("/nope", {})
        self.assertEqual(ctx.exception.code, 404)
        ctx.exception.close()


if __name__ == "__main__":
    unittest.main()
