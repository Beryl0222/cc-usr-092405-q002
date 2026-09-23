"""增量重排：分级变化、赛程延误、器材故障只动受影响行程。"""

import unittest

from tests.helpers import make_app, send


class ReplanTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.send = lambda mid, t, **p: send(self.app, mid, t, **p)
        self.send("t1", "register_team", team_id="T1", name="竞速轮椅队",
                  city="城市A", sport="竞速轮椅", leader="张领队")
        self.send("t2", "register_team", team_id="T2", name="二队", city="城市A",
                  sport="田径", leader="赵领队")
        self.send("p1", "register_person", person_id="P1", name="甲", team_id="T1",
                  city="城市A",
                  needs=["WHEELCHAIR", "WHEELCHAIR_SPORT", "TRANSFER_ASSIST"])
        self.send("p2", "register_person", person_id="P2", name="乙", team_id="T2",
                  city="城市A", needs=[])
        self.send("va", "register_venue", venue_id="VA", name="A馆", city="城市A",
                  features=["WHEELCHAIR", "VISION_GUIDE"])
        self.send("ca", "register_vehicle", vehicle_id="CA", city="城市A",
                  kind="accessible", features=["lift", "wheelchair_lock"], seats=8)
        self.send("cb", "register_vehicle", vehicle_id="CB", city="城市A",
                  kind="commuter", seats=40)
        self.send("vol", "register_volunteer", volunteer_id="VOL1", name="志A",
                  city="城市A",
                  skills=["wheelchair_handling", "transfer_assist", "sighted_guide"],
                  credential_until="2026-12-31T00:00")
        self.send("eq", "register_equipment", item_id="EQ1", city="城市A",
                  eq_type="sport_wheelchair", active_until="2026-12-31T00:00")
        self.send("e1", "register_event", event_id="E1", venue_id="VA", city="城市A",
                  sport="竞速轮椅", stage="competition", title="竞速轮椅决赛",
                  start="2026-09-25T09:00", end="2026-09-25T11:00", team_ids=["T1"])
        # P1 两段：训练/比赛段（挂 E1）、独立接送段
        self.send("scomp", "register_segment", segment_id="S_COMP", team_id="T1",
                  person_id="P1", kind="competition", city="城市A", venue_id="VA",
                  event_id="E1", start="2026-09-25T09:00", end="2026-09-25T11:00")
        self.send("sbus", "register_segment", segment_id="S_BUS", team_id="T1",
                  person_id="P1", kind="transfer", city="城市A",
                  start="2026-09-25T08:00", end="2026-09-25T09:00")
        # 另一队的无关行程，任何重排都不该动它
        self.send("sother", "register_segment", segment_id="S_OTHER", team_id="T2",
                  person_id="P2", kind="transfer", city="城市A",
                  start="2026-09-25T09:30", end="2026-09-25T10:30")
        self.send("c-all", "cover_team", team_id="T1")
        self.send("c-other", "cover_segment", segment_id="S_OTHER")

    def _assignment_ids(self, segment_id):
        return {a["assignment_id"] for a in
                self.app.store.active_assignments_for_segment(segment_id)}

    def test_classification_change_only_replans_person_future_segments(self):
        before_other = self._assignment_ids("S_OTHER")
        before_bus = self._assignment_ids("S_BUS")
        # 分级新增视障需求
        result = self.send("cc", "classification_change", person_id="P1",
                           needs=["WHEELCHAIR", "TRANSFER_ASSIST", "VISION_GUIDE"])["result"]
        self.assertEqual(set(result["affected_segments"]), {"S_COMP", "S_BUS"})
        # P1 段被重排（承诺 id 变化），但无空档（VOL1 具备 sighted_guide，场馆需补能力——先补）
        self.assertEqual(result["gaps"], [])
        self.assertNotEqual(self._assignment_ids("S_BUS"), before_bus)
        # 无关队 T2 的承诺完全不动
        self.assertEqual(self._assignment_ids("S_OTHER"), before_other)
        # 人员需求已更新，状态进入保障中
        self.assertEqual(self.app.store.get_person("P1")["status"], "保障中")

    def test_classification_change_can_open_gap_when_capability_missing(self):
        # 新增场馆不具备的需求
        result = self.send("cc2", "classification_change", person_id="P1",
                           needs=["WHEELCHAIR", "TRANSFER_ASSIST", "BARIATRIC"])["result"]
        comp_gaps = [g for g in result["gaps"]]
        # BARIATRIC 要求场馆能力 + 志愿者 bariatric_assist，当前不具备
        self.assertTrue(any(g["resource_kind"] == "venue" for g in comp_gaps))
        self.assertTrue(any(g["resource_kind"] == "volunteer" for g in comp_gaps))
        # 内部空档视图可见
        open_gaps = self.app.views.open_gaps()["gaps"]
        self.assertTrue(any(g["resource_kind"] == "venue" for g in open_gaps))

    def test_schedule_delay_shifts_only_linked_segments(self):
        result = self.send("delay", "schedule_delay", event_id="E1",
                           delay_minutes=45)["result"]
        self.assertEqual(result["affected_segments"], ["S_COMP"])
        comp = self.app.store.get_segment("S_COMP")
        bus = self.app.store.get_segment("S_BUS")
        self.assertEqual(comp["planned_start"], "2026-09-25T09:45")
        self.assertEqual(comp["planned_end"], "2026-09-25T11:45")
        # 接送段不随赛程平移
        self.assertEqual(bus["planned_start"], "2026-09-25T08:00")
        # 赛程累计延误
        self.assertEqual(self.app.store.get_event("E1")["delay_minutes"], 45)
        # 平移后若与既有用车冲突，应显式反映；此处 CA 9:45 后空闲，仍 covered
        self.assertEqual(comp["status"], "covered")

    def test_delay_may_create_gap_when_new_window_conflicts(self):
        # 新窗口 10:00–12:00 内，唯一具备 sighted_guide 的 VOL1 被他队占用；
        # P1 的比赛段分级上需要引导（先做分级变化），延误重排应暴露志愿者空档。
        self.send("cc-vis", "classification_change", person_id="P1",
                  needs=["WHEELCHAIR", "TRANSFER_ASSIST", "VISION_GUIDE"])
        self.send("p3", "register_person", person_id="P3", name="丙", team_id="T2",
                  city="城市A", needs=["VISION_GUIDE"])
        self.send("vblock", "register_segment", segment_id="S_BLOCK", team_id="T2",
                  person_id="P3", kind="competition", city="城市A", venue_id="VA",
                  start="2026-09-25T11:00", end="2026-09-25T12:00")
        self.send("cblock", "cover_segment", segment_id="S_BLOCK")
        result = self.send("delay2", "schedule_delay", event_id="E1",
                           delay_minutes=60)["result"]
        self.assertEqual(result["affected_segments"], ["S_COMP"])
        self.assertTrue(
            any(g["resource_kind"] == "volunteer" for g in result["gaps"]),
            f"延误后新窗口资源冲突应成为显式空档: {result['gaps']}",
        )

    def test_equipment_failure_only_replans_segments_using_item(self):
        # 故障前 S_COMP 使用 EQ1
        before = [a["resource_id"] for a in
                  self.app.store.active_assignments_for_segment("S_COMP")]
        self.assertIn("EQ1", before)
        result = self.send("broke", "equipment_failure", item_id="EQ1")["result"]
        self.assertEqual(result["affected_segments"], ["S_COMP"])
        self.assertTrue(any(g["resource_kind"] == "equipment" for g in result["gaps"]))
        self.assertEqual(self.app.store.get_equipment("EQ1")["service_status"], "broken")
        # 无器材段（接送、他队）未出现在影响清单
        self.assertNotIn("S_BUS", result["affected_segments"])
        self.assertNotIn("S_OTHER", result["affected_segments"])

    def test_every_change_is_audited_with_actual_impact(self):
        self.send("cc3", "classification_change", person_id="P1",
                  needs=["WHEELCHAIR", "TRANSFER_ASSIST", "VISION_GUIDE"])
        changes = self.app.views.change_log()["changes"]
        latest = changes[0]
        self.assertEqual(latest["trigger_type"], "classification_change")
        self.assertEqual(latest["trigger_ref"], "P1")
        self.assertIn("affected_segments", latest["impact"])
        self.assertIn("released", latest["impact"])
        self.assertIn("new_assignments", latest["impact"])
        self.assertTrue(latest["reason"])

    def test_past_segments_are_not_replanned(self):
        # 造一个已过去的段，分级变化不应触碰它
        self.send("sold", "register_segment", segment_id="S_OLD", team_id="T1",
                  person_id="P1", kind="transfer", city="城市A",
                  start="2026-09-20T08:00", end="2026-09-20T09:00")
        self.send("cold", "cover_segment", segment_id="S_OLD")
        old_assignments = self._assignment_ids("S_OLD")
        self.send("cc4", "classification_change", person_id="P1",
                  needs=["WHEELCHAIR", "TRANSFER_ASSIST", "VISION_GUIDE"])
        self.assertEqual(self._assignment_ids("S_OLD"), old_assignments)


if __name__ == "__main__":
    unittest.main()
