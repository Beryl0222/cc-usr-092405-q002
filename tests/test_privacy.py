"""隐私边界：不登记诊断；公众视图不泄露敏感需求。"""

import unittest

from support.catalog import ValidationError
from tests.helpers import make_app, send


class PrivacyTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.send = lambda mid, t, **p: send(self.app, mid, t, **p)
        self.send("t1", "register_team", team_id="T1", name="轮椅篮球队",
                  city="城市A", sport="轮椅篮球", leader="张领队")
        self.send("v1", "register_venue", venue_id="V1", name="A馆", city="城市A",
                  features=["WHEELCHAIR"])
        self.send("e1", "register_event", event_id="E1", venue_id="V1", city="城市A",
                  sport="轮椅篮球", stage="competition", title="轮椅篮球决赛",
                  start="2026-09-23T14:00", end="2026-09-23T16:00", team_ids=["T1"])

    def test_diagnosis_fields_are_rejected_in_any_casing(self):
        for field in ("诊断", "医学诊断", "病历", "diagnosis", "Diagnosis", "DIAGNOSIS"):
            with self.assertRaises(ValidationError) as ctx:
                self.send(f"bad-{field}", "register_person", person_id=f"X-{field}",
                          name="x", city="城市A", **{field: "脊髓损伤"})
            self.assertIn("诊断", str(ctx.exception))

    def test_no_diagnosis_is_stored_anywhere(self):
        self.send("p1", "register_person", person_id="P1", name="李一", team_id="T1",
                  city="城市A", category="肢体")
        person = self.app.store.get_person("P1")
        self.assertEqual(person["category"], "肢体")  # 类别是资源选择依据，不是诊断
        raw = self.app.store.query("SELECT * FROM persons WHERE person_id='P1'")[0]
        joined = " ".join(str(v) for v in dict(raw).values())
        self.assertNotIn("脊髓", joined)

    def test_unknown_need_code_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.send("p2", "register_person", person_id="P2", name="x", city="城市A",
                      needs=["NOT_A_REAL_NEED"])

    def test_category_defaults_to_functional_needs_without_diagnosis(self):
        result = self.send("p3", "register_person", person_id="P3", name="王二",
                           team_id="T1", city="城市A", category="视力")
        self.assertEqual(result["result"]["needs"],
                         ["VISION_GUIDE", "COMPANION_SEAT"])

    def test_public_schedule_hides_sensitive_fields(self):
        # 内部有丰富的敏感安排
        self.send("p4", "register_person", person_id="P4", name="赵三", team_id="T1",
                  city="城市A", needs=["WHEELCHAIR", "MEDICAL_OXYGEN"])
        self.send("car", "register_vehicle", vehicle_id="CAR1", city="城市A",
                  kind="accessible", features=["lift"], seats=4)
        self.send("seg", "register_segment", segment_id="S1", team_id="T1",
                  person_id="P4", kind="transfer", city="城市A",
                  start="2026-09-23T13:00", end="2026-09-23T14:00")
        self.send("cover", "cover_segment", segment_id="S1")
        # 紧急医疗理由也是敏感信息
        self.send("em", "emergency_transport", team_id="T1", city="城市A",
                  start="2026-09-23T13:30", end="2026-09-23T13:45",
                  reason="急性损伤送医，需氧气")

        blob = repr(self.app.views.public_schedule())
        for leak in ("P4", "赵三", "WHEELCHAIR", "MEDICAL_OXYGEN", "needs",
                     "CAR1", "诊断", "急性损伤", "氧气"):
            self.assertNotIn(leak, blob, f"公众赛程泄露了 {leak}")

    def test_public_delay_shown_without_reason(self):
        self.send("d1", "schedule_delay", event_id="E1", delay_minutes=30)
        schedule = self.app.views.public_schedule()["schedule"]
        self.assertEqual(schedule[0]["status"], "delayed")
        self.assertEqual(schedule[0]["start"], "2026-09-23T14:30")
        self.assertNotIn("reason", schedule[0])

    def test_cancelled_events_hidden_from_public(self):
        self.app.store.execute("UPDATE events SET status='cancelled' WHERE event_id='E1'")
        self.assertEqual(self.app.views.public_schedule()["schedule"], [])


if __name__ == "__main__":
    unittest.main()
