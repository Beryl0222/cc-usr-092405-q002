"""资源匹配与承诺互斥：能力/资质/期限、同车不承诺两队、容量拼车。"""

import unittest

from support.catalog import ValidationError
from tests.helpers import make_app, send, assignment_resources


class AssignmentTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.send = lambda mid, t, **p: send(self.app, mid, t, **p)
        # 两支同城队伍：T1 轮椅（需升降车、无障碍房），T3 普通（通勤即可）
        self.send("t1", "register_team", team_id="T1", name="轮椅队", city="城市A",
                  sport="竞速轮椅", leader="张领队")
        self.send("t3", "register_team", team_id="T3", name="田径队", city="城市A",
                  sport="田径", leader="钱领队")
        self.send("p1", "register_person", person_id="P1", name="甲", team_id="T1",
                  city="城市A", needs=["WHEELCHAIR", "TRANSFER_ASSIST", "COMPANION_SEAT"])
        self.send("p3", "register_person", person_id="P3", name="丙", team_id="T3",
                  city="城市A", needs=[])
        self.send("v1", "register_venue", venue_id="V1", name="A馆", city="城市A",
                  features=["WHEELCHAIR"])
        self.send("h1", "register_hotel", hotel_id="H1", name="无障碍酒店", city="城市A",
                  rooms=[{"room_id": "R1",
                          "features": ["accessible_room", "roll_in_shower",
                                       "low_level_access", "companion_bed"]}])
        self.send("car", "register_vehicle", vehicle_id="CAR1", city="城市A",
                  kind="accessible", features=["lift", "wheelchair_lock"], seats=6)
        self.send("bus", "register_vehicle", vehicle_id="BUS1", city="城市A",
                  kind="commuter", seats=40)
        self.send("vol", "register_volunteer", volunteer_id="VOL1", name="志愿者",
                  city="城市A",
                  skills=["wheelchair_handling", "transfer_assist"],
                  credential_until="2026-12-31T00:00")

    def _transfer(self, mid, sid, team, start, end, person=None):
        self.send(f"{mid}-seg", "register_segment", segment_id=sid, team_id=team,
                  person_id=person, kind="transfer", city="城市A", start=start, end=end)
        return self.send(mid, "cover_segment", segment_id=sid)["result"]

    def test_wheelchair_team_gets_lift_vehicle_with_companion_seat_count(self):
        result = self._transfer("c1", "S1", "T1", "2026-09-23T13:00",
                                "2026-09-23T14:00", person="P1")
        self.assertEqual(result["status"], "covered")
        detail = self.app.views.segment_detail("S1")
        vehicles = assignment_resources(detail, "vehicle")
        self.assertEqual(vehicles, ["CAR1"])
        vehicle_asg = [a for a in detail["assignments"]
                       if a["resource_kind"] == "vehicle"][0]
        self.assertEqual(vehicle_asg["seats_taken"], 2)  # 本人 + 陪护

    def test_general_team_prefers_commuter_fleet(self):
        result = self._transfer("c2", "S2", "T3", "2026-09-23T08:00",
                                "2026-09-23T09:00", person="P3")
        self.assertEqual(result["status"], "covered")
        self.assertEqual(
            assignment_resources(self.app.views.segment_detail("S2"), "vehicle"),
            ["BUS1"],
        )

    def test_same_accessible_vehicle_never_committed_to_two_teams(self):
        # T1 在 13:00–14:00 占用 CAR1
        self._transfer("c3", "S3", "T1", "2026-09-23T13:00", "2026-09-23T14:00",
                       person="P1")
        # T1 再来一支不同队（T3 临时也需要无障碍车）重叠窗口
        self.send("p3b", "register_person", person_id="P3B", name="丙b", team_id="T3",
                  city="城市A", needs=["WHEELCHAIR"])
        result = self._transfer("c4", "S4", "T3", "2026-09-23T13:30",
                                "2026-09-23T13:50", person="P3B")
        self.assertEqual(result["status"], "gap")
        self.assertTrue(any(g["resource_kind"] == "vehicle" for g in result["gaps"]))
        # 数据库层面也不存在同一窗口对两队的有效承诺
        commits = self.app.store.query(
            "SELECT team_id FROM assignments WHERE resource_kind='vehicle' "
            "AND resource_id='CAR1' AND status IN ('scheduled','fulfilled')"
        )
        self.assertEqual({r["team_id"] for r in commits}, {"T1"})

    def test_touching_windows_do_not_conflict(self):
        # 13:00–14:00 与 14:00–15:00 首尾相接，允许同一辆车
        self._transfer("c5", "S5", "T1", "2026-09-23T13:00", "2026-09-23T14:00",
                       person="P1")
        result = self._transfer("c6", "S6", "T1", "2026-09-23T14:00",
                                "2026-09-23T15:00", person="P1")
        self.assertEqual(result["status"], "covered")

    def test_same_team_shares_vehicle_within_capacity(self):
        # 同队第二个人重叠窗口可以拼同一辆车（另配一名志愿者）
        self.send("vol2-share", "register_volunteer", volunteer_id="VOL2", name="志愿者二",
                  city="城市A", skills=["wheelchair_handling"],
                  credential_until="2026-12-31T00:00")
        self.send("p2", "register_person", person_id="P2", name="乙", team_id="T1",
                  city="城市A", needs=["WHEELCHAIR"])
        self._transfer("c7", "S7", "T1", "2026-09-23T10:00", "2026-09-23T11:00",
                       person="P1")
        result = self._transfer("c8", "S8", "T1", "2026-09-23T10:00",
                                "2026-09-23T11:00", person="P2")
        self.assertEqual(result["status"], "covered")

    def test_expired_vehicle_and_volunteer_are_not_scheduled(self):
        self.send("oldcar", "register_vehicle", vehicle_id="OLD", city="城市A",
                  kind="accessible", features=["lift", "wheelchair_lock"], seats=6,
                  active_until="2026-09-01T00:00")
        self.send("oldvol", "register_volunteer", volunteer_id="OLDV", name="老志愿者",
                  city="城市A", skills=["wheelchair_handling", "transfer_assist"],
                  credential_until="2026-09-01T00:00")
        # 让唯一有效的车和志愿者都不可用
        self.app.store.execute("UPDATE vehicles SET service_status='broken' WHERE vehicle_id='CAR1'")
        self.app.store.execute("UPDATE vehicles SET service_status='broken' WHERE vehicle_id='BUS1'")
        self.app.store.execute(
            "UPDATE volunteers SET credential_until='2026-09-01T00:00' WHERE volunteer_id='VOL1'"
        )
        result = self._transfer("c9", "S9", "T1", "2026-09-24T10:00",
                                "2026-09-24T11:00", person="P1")
        self.assertEqual(result["status"], "gap")
        kinds = {g["resource_kind"] for g in result["gaps"]}
        self.assertIn("vehicle", kinds)
        self.assertIn("volunteer", kinds)

    def test_venue_without_capability_creates_venue_gap(self):
        self.send("v2", "register_venue", venue_id="V2", name="无坡道馆", city="城市A",
                  features=[])
        self.send("s-venue", "register_segment", segment_id="SV", team_id="T1",
                  person_id="P1", kind="competition", city="城市A", venue_id="V2",
                  start="2026-09-23T15:00", end="2026-09-23T16:00")
        result = self.send("cv", "cover_segment", segment_id="SV")["result"]
        self.assertEqual(result["status"], "gap")
        self.assertTrue(any(g["resource_kind"] == "venue" for g in result["gaps"]))

    def test_lodging_requires_matching_room(self):
        self.send("s-room", "register_segment", segment_id="SR", team_id="T1",
                  person_id="P1", kind="lodging", city="城市A", hotel_id="H1",
                  start="2026-09-23T18:00", end="2026-09-24T08:00")
        result = self.send("cr", "cover_segment", segment_id="SR")["result"]
        self.assertEqual(result["status"], "covered")
        self.assertIn("R1", assignment_resources(self.app.views.segment_detail("SR")))
        # 重叠时段第二队要同一间房 -> 空档
        self.send("p3c", "register_person", person_id="P3C", name="丙c", team_id="T3",
                  city="城市A", needs=["WHEELCHAIR"])
        self.send("s-room2", "register_segment", segment_id="SR2", team_id="T3",
                  person_id="P3C", kind="lodging", city="城市A", hotel_id="H1",
                  start="2026-09-23T20:00", end="2026-09-24T07:00")
        result2 = self.send("cr2", "cover_segment", segment_id="SR2")["result"]
        self.assertTrue(any(g["resource_kind"] == "room" for g in result2["gaps"]))

    def test_invalid_segment_kind_rejected(self):
        with self.assertRaises(ValidationError):
            self.send("badseg", "register_segment", segment_id="BAD", team_id="T1",
                      kind="banquet", city="城市A",
                      start="2026-09-23T12:00", end="2026-09-23T13:00")

    def test_lodging_segment_must_be_person_level(self):
        with self.assertRaises(ValidationError) as ctx:
            self.send("badroom", "register_segment", segment_id="BADROOM",
                      team_id="T1", kind="lodging", city="城市A", hotel_id="H1",
                      start="2026-09-23T18:00", end="2026-09-24T08:00")
        self.assertIn("具体个人", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
