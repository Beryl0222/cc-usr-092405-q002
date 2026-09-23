"""跨城交接：个人支持在跨城交接处不能断档。"""

import unittest

from support.catalog import ValidationError
from tests.helpers import make_app, send


class HandoffTest(unittest.TestCase):
    def _ready_destination(self, prefix, city, venue_features, vehicle=True,
                           volunteer_skills=None):
        venue_id = f"{prefix}-venue"
        self.send(f"{prefix}-v", "register_venue", venue_id=venue_id, name=f"{city}馆",
                  city=city, features=venue_features)
        if vehicle:
            self.send(f"{prefix}-car", "register_vehicle", vehicle_id=f"{prefix}-car",
                      city=city, kind="accessible",
                      features=["lift", "wheelchair_lock"], seats=6)
        if volunteer_skills:
            self.send(f"{prefix}-vol", "register_volunteer",
                      volunteer_id=f"{prefix}-vol", name=f"{city}志愿者", city=city,
                      skills=volunteer_skills,
                      credential_until="2026-12-31T00:00")
        return venue_id

    def setUp(self):
        self.app = make_app()
        self.send = lambda mid, t, **p: send(self.app, mid, t, **p)
        self.send("t1", "register_team", team_id="T1", name="竞速轮椅队",
                  city="城市A", sport="竞速轮椅", leader="张领队")
        self.send("p1", "register_person", person_id="P1", name="甲", team_id="T1",
                  city="城市A",
                  needs=["WHEELCHAIR", "TRANSFER_ASSIST", "VISION_GUIDE"])

    def test_handoff_carries_functional_needs(self):
        result = self.send("hof1", "open_handoff", person_id="P1",
                           to_city="城市B")["result"]
        self.assertEqual(result["status"], "pending")
        self.assertEqual(set(result["needs"]),
                         {"WHEELCHAIR", "TRANSFER_ASSIST", "VISION_GUIDE"})
        handoff = self.app.store.get_handoff(result["handoff_id"])
        self.assertEqual(handoff["from_city"], "城市A")
        self.assertEqual(handoff["to_city"], "城市B")

    def test_accept_succeeds_only_when_incoming_support_is_covered(self):
        venue_b = self._ready_destination(
            "B", "城市B", ["WHEELCHAIR", "VISION_GUIDE"],
            volunteer_skills=["wheelchair_handling", "transfer_assist", "sighted_guide"],
        )
        self.send("sbin", "register_segment", segment_id="S_BIN", team_id="T1",
                  person_id="P1", kind="training", city="城市B", venue_id=venue_b,
                  start="2026-09-26T09:00", end="2026-09-26T11:00")
        opened = self.send("hof2", "open_handoff", person_id="P1",
                           to_city="城市B")["result"]
        accepted = self.send("acc2", "accept_handoff",
                             handoff_id=opened["handoff_id"],
                             incoming_segment_id="S_BIN")["result"]
        self.assertEqual(accepted["status"], "accepted")
        # 接入段的个人支持资源已排齐：车辆不属训练段，但志愿者/场馆已就位
        detail = self.app.views.segment_detail("S_BIN")
        self.assertEqual(detail["status"], "covered")
        self.assertTrue(accepted["assignments"])

    def test_accept_rejected_when_destination_cannot_cover(self):
        # 城市C：场馆具备能力，但没有任何志愿者 -> 个人支持会断档
        venue_c = self._ready_destination(
            "C", "城市C", ["WHEELCHAIR", "VISION_GUIDE"], vehicle=True,
            volunteer_skills=None,
        )
        self.send("scin", "register_segment", segment_id="S_CIN", team_id="T1",
                  person_id="P1", kind="training", city="城市C", venue_id=venue_c,
                  start="2026-09-26T09:00", end="2026-09-26T11:00")
        opened = self.send("hof3", "open_handoff", person_id="P1",
                           to_city="城市C")["result"]
        with self.assertRaises(ValidationError) as ctx:
            self.send("acc3", "accept_handoff", handoff_id=opened["handoff_id"],
                      incoming_segment_id="S_CIN")
        self.assertIn("资源未排齐", str(ctx.exception))
        # 交接仍停留在 pending，不能假装成功
        self.assertEqual(
            self.app.store.get_handoff(opened["handoff_id"])["status"], "pending"
        )

    def test_accept_rejects_segment_owned_by_another_person(self):
        venue_b = self._ready_destination("B2", "城市B", ["WHEELCHAIR", "VISION_GUIDE"])
        self.send("p2", "register_person", person_id="P2", name="乙", team_id="T1",
                  city="城市B", needs=[])
        self.send("sx", "register_segment", segment_id="S_X", team_id="T1",
                  person_id="P2", kind="training", city="城市B", venue_id=venue_b,
                  start="2026-09-26T09:00", end="2026-09-26T11:00")
        opened = self.send("hof4", "open_handoff", person_id="P1",
                           to_city="城市B")["result"]
        with self.assertRaises(ValidationError):
            self.send("acc4", "accept_handoff", handoff_id=opened["handoff_id"],
                      incoming_segment_id="S_X")

    def test_continuity_detects_unaccepted_handoff_at_due_time(self):
        venue_b = self._ready_destination(
            "B3", "城市B", ["WHEELCHAIR", "VISION_GUIDE"],
            volunteer_skills=["wheelchair_handling", "transfer_assist", "sighted_guide"],
        )
        # 固定时钟 2026-09-22T08:00：接入段就在当前时刻开始
        self.send("sdue", "register_segment", segment_id="S_DUE", team_id="T1",
                  person_id="P1", kind="training", city="城市B", venue_id=venue_b,
                  start="2026-09-22T08:00", end="2026-09-22T10:00")
        opened = self.send("hof5", "open_handoff", person_id="P1",
                           to_city="城市B")["result"]
        broken = self.app.engine.handoff_continuity()
        self.assertEqual([b["handoff_id"] for b in broken], [opened["handoff_id"]])
        # 接入后排空
        self.send("acc5", "accept_handoff", handoff_id=opened["handoff_id"],
                  incoming_segment_id="S_DUE")
        self.assertEqual(self.app.engine.handoff_continuity(), [])

    def test_future_pending_handoff_is_not_yet_a_break(self):
        venue_b = self._ready_destination(
            "B4", "城市B", ["WHEELCHAIR", "VISION_GUIDE"],
            volunteer_skills=["wheelchair_handling", "transfer_assist", "sighted_guide"],
        )
        self.send("sfuture", "register_segment", segment_id="S_FUT", team_id="T1",
                  person_id="P1", kind="training", city="城市B", venue_id=venue_b,
                  start="2026-09-30T09:00", end="2026-09-30T11:00")
        self.send("hof6", "open_handoff", person_id="P1", to_city="城市B")
        self.assertEqual(self.app.engine.handoff_continuity(), [])


if __name__ == "__main__":
    unittest.main()
