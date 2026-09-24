"""消息幂等契约：完全相同的消息返回首次结果；编号复用为另一条命令则以冲突拒绝。

覆盖：同类型改字段、跨类型复用、规范化载荷等价、并发竞争、重启后再投递，
以及"冲突绝不产生部分资源分配"。
"""

import os
import tempfile
import threading
import unittest

from support.catalog import ValidationError
from support.messages import CommandConflict, canonical_text
from tests.helpers import make_app, send


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()

    # ---------- 完全相同的消息：返回首次结果 ----------

    def test_duplicate_message_returns_first_result_and_applies_once(self):
        first = send(self.app, "MSG-1", "register_team", team_id="T1", name="一队",
                     city="城市A", sport="篮球", leader="张领队")
        second = send(self.app, "MSG-1", "register_team", team_id="T1", name="一队",
                      city="城市A", sport="篮球", leader="张领队")
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["outcome"], "replay")
        self.assertEqual(second["result"], first["result"])
        self.assertEqual(self.app.store.get_team("T1")["name"], "一队")

    def test_replay_ignores_payload_key_order(self):
        """规范化载荷：键序不同但内容相同，视为同一条命令。"""
        first = self.app.commands.handle({
            "message_id": "MSG-K1", "type": "register_team",
            "payload": {"team_id": "T1", "name": "一队", "city": "城市A", "sport": "篮球"},
        })
        second = self.app.commands.handle({
            "message_id": "MSG-K1", "type": "register_team",
            "payload": {"sport": "篮球", "city": "城市A", "name": "一队", "team_id": "T1"},
        })
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["result"], first["result"])
        self.assertEqual(len(self.app.store.list_teams()), 1)

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

    # ---------- 编号复用：同类型改字段 ----------

    def test_same_type_changed_field_is_conflict_and_not_applied(self):
        send(self.app, "MSG-2", "register_team", team_id="T1", name="一队",
             city="城市A", sport="篮球")
        with self.assertRaises(CommandConflict) as ctx:
            send(self.app, "MSG-2", "register_team", team_id="T1", name="一队改名",
                 city="城市A", sport="篮球")
        body = ctx.exception.body
        self.assertEqual(body["first_type"], "register_team")
        self.assertEqual(body["conflict_type"], "register_team")
        self.assertNotEqual(body["first_payload_summary"],
                            body["conflict_payload_summary"])
        # 第二条命令没有生效
        self.assertEqual(self.app.store.get_team("T1")["name"], "一队")
        # 冲突摘要已落台账，供交接追踪
        conflicts = self.app.store.list_command_conflicts("MSG-2")
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["conflict_type"], "register_team")
        self.assertEqual(conflicts[0]["conflict_summary"],
                         body["conflict_payload_summary"])

    # ---------- 编号复用：跨类型 ----------

    def test_cross_type_reuse_is_conflict(self):
        send(self.app, "MSG-3", "register_team", team_id="T1", name="一队",
             city="城市A", sport="篮球")
        with self.assertRaises(CommandConflict) as ctx:
            send(self.app, "MSG-3", "register_vehicle", vehicle_id="V1",
                 city="城市A", kind="accessible", features=["lift"], seats=4)
        self.assertEqual(ctx.exception.body["first_type"], "register_team")
        self.assertEqual(ctx.exception.body["conflict_type"], "register_vehicle")
        # 车辆命令没有生效
        self.assertIsNone(self.app.store.get_vehicle("V1"))

    # ---------- 冲突不产生部分资源分配 ----------

    def test_conflict_never_allocates_partial_resources(self):
        send(self.app, "t", "register_team", team_id="T1", name="一队",
             city="城市A", sport="篮球")
        send(self.app, "v", "register_vehicle", vehicle_id="V1", city="城市A",
             kind="accessible", features=["lift", "wheelchair_lock"], seats=6)
        send(self.app, "s1", "register_segment", segment_id="S1", team_id="T1",
             kind="transfer", city="城市A",
             start="2026-09-23T09:00", end="2026-09-23T10:00")
        send(self.app, "s2", "register_segment", segment_id="S2", team_id="T1",
             kind="transfer", city="城市A",
             start="2026-09-23T09:00", end="2026-09-23T10:00")
        covered = send(self.app, "COV-1", "cover_segment", segment_id="S1")
        self.assertEqual(covered["result"]["status"], "covered")
        # 网关把已用过的 COV-1 配给了另一条 cover 命令：必须整体拒绝
        with self.assertRaises(CommandConflict):
            send(self.app, "COV-1", "cover_segment", segment_id="S2")
        # S2 没有任何资源承诺（无部分分配）
        detail = self.app.views.segment_detail("S2")
        self.assertEqual(detail["assignments"], [])
        self.assertEqual(detail["status"], "planned")
        # S1 的承诺保持原样
        self.assertEqual(len(self.app.views.segment_detail("S1")["assignments"]), 1)

    # ---------- 并发竞争 ----------

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

    def test_concurrent_conflicting_messages_single_winner(self):
        """同一编号并发携带不同载荷：恰有一条生效，其余全部冲突，无中间态。"""
        outcomes = {"ok": 0, "conflict": 0}
        lock = threading.Lock()

        def fire(i):
            try:
                send(self.app, "RACE-1", "register_team", team_id=f"TR{i}",
                     name=f"队{i}", city="城市A", sport="篮球")
                with lock:
                    outcomes["ok"] += 1
            except CommandConflict:
                with lock:
                    outcomes["conflict"] += 1

        threads = [threading.Thread(target=fire, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(outcomes["ok"], 1)
        self.assertEqual(outcomes["conflict"], 7)
        self.assertEqual(len(self.app.store.list_teams()), 1)
        # 7 次冲突全部落台账
        self.assertEqual(len(self.app.store.list_command_conflicts("RACE-1")), 7)

    # ---------- 重启恢复 ----------

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
            # 完全相同的消息重投：返回首次结果，不重复生效
            dup = send(app2, "BOOT-1", "register_team", team_id="T1", name="一队",
                       city="城市A", sport="篮球", leader="张领队")
            self.assertTrue(dup["deduplicated"])
            self.assertEqual(app2.store.get_team("T1")["name"], "一队")
            app2.close()

    def test_conflict_contract_survives_restart(self):
        """重启后再次投递被改写的编号：仍判冲突，且冲突台账可回看。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "para.db")
            app1 = make_app(db)
            send(app1, "BOOT-C1", "register_team", team_id="T1", name="一队",
                 city="城市A", sport="篮球")
            app1.close()

            app2 = make_app(db)
            with self.assertRaises(CommandConflict) as ctx:
                send(app2, "BOOT-C1", "register_team", team_id="T1", name="改名队",
                     city="城市A", sport="篮球")
            self.assertEqual(app2.store.get_team("T1")["name"], "一队")
            conflicts = app2.store.list_command_conflicts("BOOT-C1")
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0]["conflict_summary"],
                             ctx.exception.body["conflict_payload_summary"])
            app2.close()

            # 再重启：台账与首次结果都还在
            app3 = make_app(db)
            self.assertEqual(len(app3.store.list_command_conflicts("BOOT-C1")), 1)
            replay = send(app3, "BOOT-C1", "register_team", team_id="T1", name="一队",
                          city="城市A", sport="篮球")
            self.assertTrue(replay["deduplicated"])
            app3.close()

    # ---------- 规范化载荷 ----------

    def test_canonical_text_normalizes_key_order(self):
        a = canonical_text({"b": 1, "a": {"y": [1, 2], "x": True}})
        b = canonical_text({"a": {"x": True, "y": [1, 2]}, "b": 1})
        self.assertEqual(a, b)
        # 数组顺序是语义的一部分，不做排序
        self.assertNotEqual(canonical_text({"k": [1, 2]}),
                            canonical_text({"k": [2, 1]}))


if __name__ == "__main__":
    unittest.main()
