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

    def test_reused_id_with_different_payload_is_409_and_not_applied(self):
        first = {"message_id": "HTTP-IDEM", "type": "register_team",
                 "payload": {"team_id": "T1", "name": "一队", "city": "城市A",
                             "sport": "篮球"}}
        status, _ = self._post("/commands", first)
        self.assertEqual(status, 200)
        # 同类型改字段：409 + 可核验摘要
        changed = dict(first, payload=dict(first["payload"], name="冒名队"))
        with self.assertRaises(HTTPError) as ctx:
            self._post("/commands", changed)
        self.assertEqual(ctx.exception.code, 409)
        body = json.load(ctx.exception)
        ctx.exception.close()
        self.assertTrue(body["conflict"])
        self.assertEqual(body["mismatch"], "payload")
        self.assertEqual(body["message_id"], "HTTP-IDEM")
        self.assertEqual(body["first"]["type"], "register_team")
        self.assertNotEqual(body["first"]["payload_digest"],
                           body["rejected"]["payload_digest"])
        self.assertTrue(body["conflict_id"])
        # 冲突不毒化后续命令：新编号照常受理
        status, _ = self._post("/commands", {
            "message_id": "HTTP-READ", "type": "register_team",
            "payload": {"team_id": "T-READ", "name": "只读探测", "city": "城市A",
                        "sport": "田径"}})
        self.assertEqual(status, 200)
        # 冲突可经内部接口回看
        status, conflicts = self._get("/internal/conflicts")
        self.assertEqual(status, 200)
        record = next(c for c in conflicts["conflicts"]
                      if c["message_id"] == "HTTP-IDEM")
        self.assertEqual(record["conflict_id"], body["conflict_id"])
        self.assertIn("冒名队", record["conflict_payload"])

    def test_reused_id_with_different_type_is_409(self):
        message = {"message_id": "HTTP-XTYPE", "type": "register_team",
                   "payload": {"team_id": "T9", "name": "九队", "city": "城市A",
                               "sport": "篮球"}}
        self._post("/commands", message)
        other = {"message_id": "HTTP-XTYPE", "type": "register_venue",
                 "payload": {"venue_id": "V9", "name": "九馆", "city": "城市A"}}
        with self.assertRaises(HTTPError) as ctx:
            self._post("/commands", other)
        self.assertEqual(ctx.exception.code, 409)
        body = json.load(ctx.exception)
        ctx.exception.close()
        self.assertEqual(body["mismatch"], "command_type")
        self.assertEqual(body["first"]["type"], "register_team")
        self.assertEqual(body["rejected"]["type"], "register_venue")

    def test_identical_redelivery_after_conflict_still_replays_first(self):
        message = {"message_id": "HTTP-MIX", "type": "register_team",
                   "payload": {"team_id": "T8", "name": "八队", "city": "城市A",
                               "sport": "篮球"}}
        _, first = self._post("/commands", message)
        with self.assertRaises(HTTPError) as ctx:
            self._post("/commands", dict(message, type="register_hotel",
                                         payload={"hotel_id": "H8", "name": "酒店",
                                                  "city": "城市A"}))
        self.assertEqual(ctx.exception.code, 409)
        ctx.exception.close()
        # 冲突之后，完全相同的消息仍回放首次结果——两种结论并存且稳定
        _, replay = self._post("/commands", message)
        self.assertTrue(replay["deduplicated"])
        self.assertEqual(replay["result"], first["result"])

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
                     "/internal/utilization", "/internal/continuity",
                     "/internal/conflicts"):
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
