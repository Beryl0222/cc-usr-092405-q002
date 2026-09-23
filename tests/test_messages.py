"""消息幂等与服务重启：重复消息不重复生效，重启后状态与去重表仍在。"""

import os
import tempfile
import threading
import unittest

from support.catalog import ValidationError
from tests.helpers import make_app, send


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()

    def test_duplicate_message_returns_first_result_and_applies_once(self):
        first = send(self.app, "MSG-1", "register_team", team_id="T1", name="一队",
                     city="城市A", sport="篮球", leader="张领队")
        second = send(self.app, "MSG-1", "register_team", team_id="T1", name="一队改名",
                      city="城市A", sport="篮球", leader="张领队")
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["result"], first["result"])
        # 改名内容没有第二次生效
        self.assertEqual(self.app.store.get_team("T1")["name"], "一队")

    def test_distinct_message_ids_both_apply(self):
        send(self.app, "A", "register_team", team_id="T1", name="一队", city="城市A",
             sport="篮球")
        send(self.app, "B", "register_team", team_id="T2", name="二队", city="城市A",
             sport="田径")
        self.assertEqual(len(self.app.store.list_teams()), 2)

    def test_message_requires_id_and_type(self):
        with self.assertRaises(ValidationError):
            self.app.commands.handle({"type": "register_team"})
        with self.assertRaises(ValidationError):
            self.app.commands.handle({"message_id": "x"})

    def test_concurrent_duplicate_messages_apply_once(self):
        errors = []

        def fire():
            try:
                send(self.app, "CONCURRENT-1", "register_team", team_id="TC",
                     name="并发队", city="城市A", sport="篮球")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=fire) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.app.store.list_teams()), 1)

    def test_state_and_dedup_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "para.db")
            app1 = make_app(db)
            send(app1, "BOOT-1", "register_team", team_id="T1", name="一队",
                 city="城市A", sport="篮球", leader="张领队")
            send(app1, "BOOT-2", "register_person", person_id="P1", name="甲",
                 team_id="T1", city="城市A", category="肢体")
            app1.close()

            app2 = make_app(db)
            # 重启后数据仍在
            self.assertEqual(app2.store.get_team("T1")["name"], "一队")
            person = app2.store.get_person("P1")
            self.assertIn("WHEELCHAIR", person["needs_json"])
            # 去重表仍在：旧消息不重复生效（且名称保持首次值）
            dup = send(app2, "BOOT-1", "register_team", team_id="T1", name="被忽略",
                       city="城市A", sport="篮球")
            self.assertTrue(dup["deduplicated"])
            self.assertEqual(app2.store.get_team("T1")["name"], "一队")
            app2.close()


if __name__ == "__main__":
    unittest.main()
