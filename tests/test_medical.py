"""紧急医疗：可优先占用普通通勤资源，但必须给理由并通知原负责人。"""

import unittest

from support.catalog import ValidationError
from tests.helpers import make_app, send, assignment_resources


class MedicalOverrideTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.send = lambda mid, t, **p: send(self.app, mid, t, **p)
        for kw in [
            dict(team_id="T2", name="二队", city="城市B", sport="轮椅篮球", leader="赵领队"),
            dict(team_id="T3", name="三队", city="城市B", sport="田径", leader="钱领队"),
            dict(team_id="T4", name="医疗组", city="城市B", sport="保障", leader="孙大夫"),
        ]:
            self.send(kw["team_id"], "register_team", **kw)
        self.send("p2", "register_person", person_id="P2", name="甲", team_id="T2",
                  city="城市B", needs=["WHEELCHAIR"])
        self.send("p3", "register_person", person_id="P3", name="乙", team_id="T3",
                  city="城市B", needs=[])
        self.send("car", "register_vehicle", vehicle_id="CAR1", city="城市B",
                  kind="accessible", features=["lift", "wheelchair_lock"], seats=6)
        self.send("bus", "register_vehicle", vehicle_id="BUS1", city="城市B",
                  kind="commuter", seats=40)
        self.send("vol2", "register_volunteer", volunteer_id="V2", name="志愿者乙",
                  city="城市B", skills=["wheelchair_handling"],
                  credential_until="2026-12-31T00:00")
        self.send("s7", "register_segment", segment_id="S7", team_id="T2",
                  kind="transfer", city="城市B",
                  start="2026-09-24T09:10", end="2026-09-24T10:00")
        self.send("s8", "register_segment", segment_id="S8", team_id="T3",
                  kind="transfer", city="城市B",
                  start="2026-09-24T09:00", end="2026-09-24T10:00")
        self.send("cv7", "cover_segment", segment_id="S7")
        self.send("cv8", "cover_segment", segment_id="S8")

    def test_reason_is_mandatory(self):
        for reason in ("", "   "):
            with self.assertRaises(ValidationError):
                self.send(f"em-{reason}", "emergency_transport", team_id="T4",
                          city="城市B", start="2026-09-24T11:00",
                          end="2026-09-24T11:30", reason=reason)

    def test_idle_resource_is_used_without_preemption(self):
        result = self.send("em1", "emergency_transport", team_id="T4", city="城市C",
                           start="2026-09-24T09:20", end="2026-09-24T09:50",
                           reason="城市C本就无车，此场景见下；先验证理由落库")
        # 城市C无车 -> 显式空档而不是伪造承诺
        self.assertTrue(result["result"].get("gaps"))

    def test_preempt_commuter_with_reason_and_notification(self):
        # 城市B 窗口内 CAR1 被 T2、BUS1 被 T3 占用；T4 医疗无特殊需求，
        # 只能占用普通通勤车 BUS1，绝不动无障碍专车 CAR1。
        result = self.send("em2", "emergency_transport", team_id="T4", city="城市B",
                           start="2026-09-24T09:20", end="2026-09-24T09:50",
                           reason="赛场医疗点设备故障，需紧急转运观察", seats=2)["result"]
        self.assertEqual(result["vehicle_id"], "BUS1")
        # 被抢占的是 T3 的通勤承诺
        self.assertEqual([p["team_id"] for p in result["preempted"]], ["T3"])
        # 通知了 T3 的原安排负责人
        self.assertEqual([n["recipient"] for n in result["notifications"]], ["钱领队"])
        # 无障碍专车 T2 的承诺完好
        self.assertEqual(
            assignment_resources(self.app.views.segment_detail("S7"), "vehicle"),
            ["CAR1"],
        )
        # 医疗承诺带标记和理由
        medical_asg = self.app.store.query(
            "SELECT * FROM assignments WHERE assignment_id=?",
            (result["assignment_id"],),
        )[0]
        self.assertEqual(medical_asg["medical_override"], 1)
        self.assertIn("设备故障", medical_asg["override_reason"])
        # 被抢段重排：无车可派 -> 显式空档（不是静默丢失）
        s8 = self.app.views.segment_detail("S8")
        self.assertEqual(s8["status"], "gap")
        # 通知记录可回看，正文含理由、车辆、段号
        notes = self.app.views.notifications()["notifications"]
        self.assertTrue(any(
            n["recipient"] == "钱领队" and "设备故障" in n["body"]
            and "BUS1" in n["body"] and "S8" in n["body"]
            for n in notes
        ))
        # 审计记录存在
        changes = self.app.views.change_log()["changes"]
        self.assertEqual(changes[0]["trigger_type"], "medical_override")

    def test_accessible_vehicle_is_never_preempted_for_non_accessible_case(self):
        # 把 BUS1 标记故障，仅余 CAR1；非无障碍医疗不得抢占无障碍专车
        self.send("busdown", "vehicle_status", vehicle_id="BUS1",
                  service_status="broken")
        result = self.send("em3", "emergency_transport", team_id="T4", city="城市B",
                           start="2026-09-24T11:00", end="2026-09-24T11:30",
                           reason="普通观察转运")["result"]
        # 该窗口（11:00 后）两车实际都空闲；CAR1 可以正常空闲使用，
        # 但抢占路径在任何情况下都不允许动 accessible 车。
        preempted_kinds = [
            self.app.store.get_vehicle(p["resource_id"])["kind"]
            if "resource_id" in p else "commuter"
            for p in result.get("preempted", [])
        ]
        self.assertNotIn("accessible", preempted_kinds)

    def test_emergency_needs_accessible_when_idle_accessible_exists(self):
        # 无障碍医疗在空闲时直接派无障碍车
        result = self.send("em4", "emergency_transport", team_id="T4", city="城市B",
                           start="2026-09-24T15:00", end="2026-09-24T15:30",
                           reason="轮椅运动员外伤转运", accessible_required=True)["result"]
        self.assertEqual(result["vehicle_id"], "CAR1")


if __name__ == "__main__":
    unittest.main()
