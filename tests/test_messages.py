"""消息幂等契约：相同消息回放首结果；编号被不同命令复用以冲突拒绝并留痕。

契约（并发到达、SQLite 重启恢复、HTTP 调用结论一致）：
* 相同 message_id + 相同命令类型 + 相同规范化载荷 → 只生效一次，回放首结果；
* 相同 message_id 但类型或规范化载荷不同 → MessageConflictError（HTTP 409），
  新命令绝不执行（不产生任何部分资源分配），首次与冲突摘要落库供交接追踪。
"""

import json
import os
import sqlite3
import tempfile
import threading
import unittest

from support.catalog import ValidationError
from support.messages import MessageConflictError
from support.store import digest
from tests.helpers import make_app, send


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()

    # ---------- 相同消息：只生效一次，回放首结果 ----------

    def test_identical_duplicate_returns_first_result_and_applies_once(self):
        first = send(self.app, "MSG-1", "register_team", team_id="T1", name="一队",
                     city="城市A", sport="篮球", leader="张领队")
        second = send(self.app, "MSG-1", "register_team", team_id="T1", name="一队",
                      city="城市A", sport="篮球", leader="张领队")
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["result"], first["result"])
        self.assertEqual(len(self.app.store.list_teams()), 1)

    def test_equivalent_payload_key_order_still_deduplicates(self):
        # 规范化载荷：键序/空白差异不改变消息身份
        self.app.commands.handle({"message_id": "MSG-ORD", "type": "register_team",
                                  "payload": {"team_id": "T1", "name": "一队",
                                              "city": "城市A", "sport": "篮球"}})
        again = self.app.commands.handle(
            {"message_id": "MSG-ORD", "type": "register_team",
             "payload": {"sport": "篮球", "city": "城市A", "name": "一队",
                         "team_id": "T1"}})
        self.assertTrue(again["deduplicated"])
        self.assertEqual(len(self.app.store.list_teams()), 1)
        self.assertEqual(self.app.store.list_command_conflicts(), [])

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

    # ---------- 编号复用：冲突拒绝，新命令不生效 ----------

    def test_same_type_changed_field_is_conflict_and_not_applied(self):
        first_payload = {"team_id": "T1", "name": "一队", "city": "城市A",
                         "sport": "篮球", "leader": "张领队"}
        send(self.app, "MSG-2", "register_team", **first_payload)
        changed_payload = first_payload | {"name": "改名队"}
        with self.assertRaises(MessageConflictError) as ctx:
            send(self.app, "MSG-2", "register_team", **changed_payload)
        exc = ctx.exception
        self.assertEqual(exc.mismatch, "payload")
        self.assertEqual(exc.first_type, "register_team")
        self.assertEqual(exc.conflict_type, "register_team")
        # 首次结果保持有效，改名未生效
        self.assertEqual(self.app.store.get_team("T1")["name"], "一队")
        # 冲突双方摘要落库，供交接追踪
        records = self.app.store.list_command_conflicts(message_id="MSG-2")
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["conflict_id"], exc.conflict_id)
        self.assertEqual(record["mismatch"], "payload")
        self.assertEqual(record["first_command_type"], "register_team")
        self.assertEqual(record["conflict_command_type"], "register_team")
        self.assertEqual(record["first_payload_digest"], digest(first_payload))
        self.assertEqual(record["conflict_payload_digest"], digest(changed_payload))
        self.assertTrue(record["first_response_digest"])
        self.assertIn("改名队", record["conflict_payload"])
        self.assertTrue(record["detected_at"])
        # 内部视图同样可见（交接追踪入口）
        view = self.app.views.command_conflicts()["conflicts"]
        self.assertEqual([c["conflict_id"] for c in view], [record["conflict_id"]])

    def test_cross_type_reuse_is_conflict_and_not_applied(self):
        send(self.app, "MSG-3", "register_team", team_id="T1", name="一队",
             city="城市A", sport="篮球")
        with self.assertRaises(MessageConflictError) as ctx:
            send(self.app, "MSG-3", "register_person", person_id="P1", name="甲",
                 team_id="T1", city="城市A")
        self.assertEqual(ctx.exception.mismatch, "command_type")
        # 新命令未执行：没有登记出任何人
        self.assertIsNone(self.app.store.get_person("P1"))
        self.assertEqual(self.app.store.list_persons(), [])
        record = self.app.store.list_command_conflicts(message_id="MSG-3")[0]
        self.assertEqual(record["mismatch"], "command_type")
        self.assertEqual(record["first_command_type"], "register_team")
        self.assertEqual(record["conflict_command_type"], "register_person")

    def test_conflict_creates_no_partial_resource_allocation(self):
        # 首次命令产生真实资源承诺（车辆 + 志愿者）
        send(self.app, "t1", "register_team", team_id="T1", name="一队",
             city="城市A", sport="篮球")
        send(self.app, "p1", "register_person", person_id="P1", name="甲",
             team_id="T1", city="城市A", needs=["WHEELCHAIR"])
        send(self.app, "car", "register_vehicle", vehicle_id="CAR1", city="城市A",
             kind="accessible", features=["lift", "wheelchair_lock"], seats=6)
        send(self.app, "vol", "register_volunteer", volunteer_id="VOL1", name="志愿者",
             city="城市A", skills=["wheelchair_handling"],
             credential_until="2026-12-31T00:00")
        send(self.app, "s1", "register_segment", segment_id="S1", team_id="T1",
             person_id="P1", kind="transfer", city="城市A",
             start="2026-09-23T13:00", end="2026-09-23T14:00")
        send(self.app, "s2", "register_segment", segment_id="S2", team_id="T1",
             person_id="P1", kind="transfer", city="城市A",
             start="2026-09-23T15:00", end="2026-09-23T16:00")
        first = send(self.app, "MSG-COVER", "cover_segment", segment_id="S1")
        self.assertEqual(first["result"]["status"], "covered")
        assignments_before = self.app.store.list_assignments()
        self.assertTrue(assignments_before)
        # 同一编号被改派去覆盖另一段：拒绝，且 S2 不得出现任何承诺/空档
        with self.assertRaises(MessageConflictError):
            send(self.app, "MSG-COVER", "cover_segment", segment_id="S2")
        self.assertEqual(self.app.store.list_assignments(), assignments_before)
        self.assertEqual(self.app.store.active_assignments_for_segment("S2"), [])
        self.assertEqual(self.app.store.get_segment("S2")["status"], "planned")
        self.assertEqual(self.app.store.get_segment("S1")["status"], "covered")
        self.assertEqual(self.app.store.list_open_gaps(), [])

    def test_each_conflicting_redelivery_leaves_a_trace(self):
        send(self.app, "MSG-4", "register_team", team_id="T1", name="一队",
             city="城市A", sport="篮球")
        for _ in range(3):
            with self.assertRaises(MessageConflictError):
                send(self.app, "MSG-4", "register_team", team_id="T1", name="冒名队",
                     city="城市A", sport="篮球")
        self.assertEqual(
            len(self.app.store.list_command_conflicts(message_id="MSG-4")), 3)
        self.assertEqual(self.app.store.get_team("T1")["name"], "一队")

    # ---------- 并发到达：同一结论 ----------

    def test_concurrent_identical_messages_apply_once(self):
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
        self.assertEqual(self.app.store.list_command_conflicts(), [])

    def test_concurrent_conflicting_messages_exactly_one_applies(self):
        # 同一编号被并发配给 8 条不同命令：恰一条生效，其余全部冲突拒绝
        barrier = threading.Barrier(8)
        outcomes = []
        outcomes_lock = threading.Lock()

        def fire(index):
            try:
                barrier.wait(timeout=5)
                result = send(self.app, "CONCURRENT-X", "register_team",
                              team_id=f"T{index}", name=f"队{index}",
                              city="城市A", sport="篮球")
                with outcomes_lock:
                    outcomes.append(("applied", result))
            except MessageConflictError as exc:
                with outcomes_lock:
                    outcomes.append(("conflict", exc))
            except Exception as exc:  # noqa: BLE001
                with outcomes_lock:
                    outcomes.append(("error", exc))

        threads = [threading.Thread(target=fire, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        kinds = [kind for kind, _ in outcomes]
        self.assertEqual(kinds.count("applied"), 1, outcomes)
        self.assertEqual(kinds.count("conflict"), 7, outcomes)
        self.assertEqual(kinds.count("error"), 0, outcomes)
        # 只有胜出的一条落库；冲突不产生任何副作用
        self.assertEqual(len(self.app.store.list_teams()), 1)
        self.assertEqual(
            len(self.app.store.list_command_conflicts(message_id="CONCURRENT-X")), 7)
        # 竞争落定的首次内容此后支配回放：相同消息仍得首结果
        winner = [result for kind, result in outcomes if kind == "applied"][0]
        winner_index = winner["result"]["team_id"][1:]
        replay = send(self.app, "CONCURRENT-X", "register_team",
                      team_id=f"T{winner_index}", name=f"队{winner_index}",
                      city="城市A", sport="篮球")
        self.assertTrue(replay["deduplicated"])
        self.assertEqual(replay["result"], winner["result"])

    # ---------- 重启恢复：结论不变 ----------

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
            # 去重表仍在：相同消息回放首结果，不重复生效
            dup = send(app2, "BOOT-1", "register_team", team_id="T1", name="一队",
                       city="城市A", sport="篮球", leader="张领队")
            self.assertTrue(dup["deduplicated"])
            self.assertEqual(app2.store.get_team("T1")["name"], "一队")
            app2.close()

    def test_conflict_and_its_record_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "para.db")
            app1 = make_app(db)
            send(app1, "BOOT-9", "register_team", team_id="T1", name="一队",
                 city="城市A", sport="篮球")
            # 重启前已有一笔冲突留痕
            with self.assertRaises(MessageConflictError):
                send(app1, "BOOT-9", "register_team", team_id="T1", name="冒名队",
                     city="城市A", sport="篮球")
            app1.close()

            app2 = make_app(db)
            # 重启后冲突记录仍在，交接追踪不丢
            records = app2.store.list_command_conflicts(message_id="BOOT-9")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["conflict_command_type"], "register_team")
            # 重启后同一编号再被复用（跨类型）：同一契约拒绝，不当新命令执行
            with self.assertRaises(MessageConflictError) as ctx:
                send(app2, "BOOT-9", "register_vehicle", vehicle_id="CAR1",
                     city="城市A", kind="commuter", seats=40)
            self.assertEqual(ctx.exception.mismatch, "command_type")
            self.assertIsNone(app2.store.get_vehicle("CAR1"))
            self.assertEqual(app2.store.get_team("T1")["name"], "一队")
            self.assertEqual(
                len(app2.store.list_command_conflicts(message_id="BOOT-9")), 2)
            app2.close()

    def test_legacy_commands_table_is_migrated_and_contract_holds(self):
        # 旧版库：commands 表没有摘要列；升级后同一契约必须继续成立
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "para.db")
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE commands (message_id TEXT PRIMARY KEY,"
                " command_type TEXT NOT NULL, payload TEXT NOT NULL,"
                " response TEXT, created_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO commands VALUES(?,?,?,?,?)",
                ("OLD-1", "register_team",
                 json.dumps({"team_id": "T1", "name": "一队", "city": "城市A",
                             "sport": "篮球"}, ensure_ascii=False, sort_keys=True),
                 json.dumps({"type": "register_team", "message_id": "OLD-1",
                             "result": {"team_id": "T1"}}, ensure_ascii=False),
                 "2026-09-01T00:00:00Z"),
            )
            conn.commit()
            conn.close()

            app = make_app(db)
            # 迁移补齐摘要；相同消息仍回放首结果
            replay = send(app, "OLD-1", "register_team", team_id="T1", name="一队",
                          city="城市A", sport="篮球")
            self.assertTrue(replay["deduplicated"])
            self.assertEqual(replay["result"], {"team_id": "T1"})
            # 改字段则按契约冲突，而不是静默回放
            with self.assertRaises(MessageConflictError):
                send(app, "OLD-1", "register_team", team_id="T1", name="改名",
                     city="城市A", sport="篮球")
            app.close()


if __name__ == "__main__":
    unittest.main()
