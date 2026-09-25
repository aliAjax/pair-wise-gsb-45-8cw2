import tempfile
import unittest
from pathlib import Path

from app import build_services
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src.tug_rules import TugRules


ADMIN = Actor("admin", "admin")
CONTROLLER = Actor("controller", "port_controller")
TUG_OPERATOR = Actor("tug-op", "tug_dispatcher")


def plan_payload(**overrides):
    data = {'vessel': 'HaiYun', 'berth': 'B12', 'vessel_length_m': 180, 'berth_length_m': 220, 'draft_m': 10.2, 'berth_depth_m': 11.5, 'eta_hour': 6, 'etd_hour': 18, 'risk_level': 'medium', 'dangerous_goods': False, 'dangerous_class': ''}
    data.update(overrides)
    return data


def tug_payload(name="Tuo-1", hp=3000.0, start=0, end=24, rate=2.5):
    return {"name": name, "horsepower_hp": hp, "available_from_hour": start, "available_to_hour": end, "rate_per_hp_hour": rate}


class TugRulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = TugRules()

    def test_min_required_hp_uses_length_and_risk(self):
        self.assertEqual(self.rules.min_required_hp({"vessel_length_m": 180, "risk_level": "medium", "dangerous_goods": False}), 2700)
        self.assertEqual(self.rules.min_required_hp({"vessel_length_m": 180, "risk_level": "high", "dangerous_goods": True}), 4148)
        self.assertEqual(self.rules.min_required_hp({"vessel_length_m": 100, "risk_level": "low", "dangerous_goods": False}), 1200)

    def test_escort_windows_stay_within_day(self):
        windows = self.rules.escort_windows({"eta_hour": 6, "etd_hour": 18})
        self.assertEqual(windows["inbound"], (6.0, 8.0))
        self.assertEqual(windows["outbound"], (16.0, 18.0))
        clamped = self.rules.escort_windows({"eta_hour": 23, "etd_hour": 1})
        self.assertEqual(clamped["inbound"], (23.0, 24.0))
        self.assertEqual(clamped["outbound"], (0.0, 1.0))

    def test_select_tugs_prefers_smallest_sufficient_single(self):
        tugs = [
            {"id": 1, "name": "A", "horsepower_hp": 2000},
            {"id": 2, "name": "B", "horsepower_hp": 3000},
            {"id": 3, "name": "C", "horsepower_hp": 5000},
        ]
        selected = self.rules.select_tugs(2700, tugs)
        self.assertEqual([tug["id"] for tug in selected], [2])

    def test_select_tugs_accumulates_total_thrust(self):
        tugs = [
            {"id": 1, "name": "A", "horsepower_hp": 2000},
            {"id": 2, "name": "B", "horsepower_hp": 3000},
            {"id": 3, "name": "C", "horsepower_hp": 5000},
        ]
        selected = self.rules.select_tugs(9000, tugs)
        self.assertEqual([tug["id"] for tug in selected], [3, 2, 1])
        self.assertIsNone(self.rules.select_tugs(20000, tugs))
        self.assertIsNone(self.rules.select_tugs(100, []))

    def test_validate_tug_rejects_bad_input(self):
        with self.assertRaises(ValidationError):
            self.rules.validate_tug(tug_payload(start=10, end=10))
        with self.assertRaises(ValidationError):
            self.rules.validate_tug(tug_payload(hp=0))


class TugFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service, self.tugs = build_services(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def create_plan(self, reference="VOY-1", **overrides):
        return self.service.create(CONTROLLER, reference, plan_payload(**overrides))

    def escorts(self, record_id):
        return self.tugs.record_escorts(ADMIN, record_id)

    def test_dispatch_assigns_both_legs(self):
        self.tugs.register_tug(TUG_OPERATOR, tug_payload())
        record = self.create_plan()
        data = self.escorts(record["id"])
        self.assertEqual(data["escort_status"], "assigned")
        self.assertEqual(data["min_required_hp"], 2700)
        for leg in data["legs"]:
            self.assertEqual(leg["status"], "assigned")
            self.assertGreaterEqual(leg["assigned_hp"], leg["required_hp"])

    def test_insufficient_thrust_goes_pending_with_reason(self):
        self.tugs.register_tug(TUG_OPERATOR, tug_payload(hp=2000))
        record = self.create_plan()
        data = self.escorts(record["id"])
        self.assertEqual(data["escort_status"], "pending")
        for leg in data["legs"]:
            self.assertEqual(leg["status"], "pending")
            self.assertIn("总推力不足", leg["reason"])
        pending = self.tugs.pending(ADMIN)
        self.assertEqual(len(pending), 2)
        self.assertEqual(pending[0]["vessel"], "HaiYun")
        # 补充拖轮后重新派工成功，两段都满足总推力
        self.tugs.register_tug(TUG_OPERATOR, tug_payload(name="Tuo-2", hp=2500))
        data = self.tugs.retry_record(CONTROLLER, record["id"])
        self.assertEqual(data["escort_status"], "assigned")
        inbound = [leg for leg in data["legs"] if leg["direction"] == "inbound"][0]
        self.assertEqual(len(inbound["assignments"]), 2)
        self.assertEqual(inbound["assigned_hp"], 4500.0)

    def test_same_tug_cannot_serve_overlapping_plans(self):
        self.tugs.register_tug(TUG_OPERATOR, tug_payload())
        first = self.create_plan("VOY-1", berth="B12", eta_hour=6, etd_hour=18)
        second = self.create_plan("VOY-2", berth="B13", eta_hour=7, etd_hour=19)
        data = self.escorts(second["id"])
        self.assertEqual(data["escort_status"], "pending")
        for leg in data["legs"]:
            self.assertIn("无空闲拖轮", leg["reason"])
        # 取消第一条计划立即释放拖轮
        first = self.service.act(CONTROLLER, first["id"], first["version"], "cancel", {"cancel_reason": "改期"})
        self.assertEqual(first["state"], "cancelled")
        self.assertEqual(self.escorts(first["id"])["escort_status"], "released")
        # 重新派工后第二条计划获得拖轮
        data = self.tugs.retry_record(CONTROLLER, second["id"])
        self.assertEqual(data["escort_status"], "assigned")

    def test_availability_window_must_cover_escort(self):
        self.tugs.register_tug(TUG_OPERATOR, tug_payload(start=8, end=20))
        record = self.create_plan()
        legs = {leg["direction"]: leg for leg in self.escorts(record["id"])["legs"]}
        self.assertEqual(legs["inbound"]["status"], "pending")
        self.assertEqual(legs["outbound"]["status"], "assigned")

    def test_complete_escort_writes_actuals_and_cost(self):
        self.tugs.register_tug(TUG_OPERATOR, tug_payload(hp=3000, rate=2.5))
        record = self.create_plan()
        for leg in self.escorts(record["id"])["legs"]:
            for assignment in leg["assignments"]:
                result = self.tugs.complete_assignment(TUG_OPERATOR, assignment["id"], {"actual_hours": 2})
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["actual_hp"], 3000.0)
                self.assertEqual(result["actual_hours"], 2.0)
                self.assertEqual(result["cost"], 15000.0)
        self.assertEqual(self.escorts(record["id"])["escort_status"], "completed")

    def test_complete_with_actual_hp_override(self):
        self.tugs.register_tug(TUG_OPERATOR, tug_payload(hp=3000, rate=2.0))
        record = self.create_plan()
        assignment = self.escorts(record["id"])["legs"][0]["assignments"][0]
        result = self.tugs.complete_assignment(TUG_OPERATOR, assignment["id"], {"actual_hours": 1.5, "actual_hp": 2800})
        self.assertEqual(result["cost"], 8400.0)
        with self.assertRaises(Conflict):
            self.tugs.complete_assignment(TUG_OPERATOR, assignment["id"], {"actual_hours": 1})

    def test_complete_rejects_invalid_hours(self):
        self.tugs.register_tug(TUG_OPERATOR, tug_payload())
        record = self.create_plan()
        assignment = self.escorts(record["id"])["legs"][0]["assignments"][0]
        with self.assertRaises(ValidationError):
            self.tugs.complete_assignment(TUG_OPERATOR, assignment["id"], {"actual_hours": 0})

    def test_duplicate_tug_name_rejected(self):
        self.tugs.register_tug(TUG_OPERATOR, tug_payload())
        with self.assertRaises(Conflict):
            self.tugs.register_tug(TUG_OPERATOR, tug_payload())

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.tugs.register_tug(Actor("x", "outsider"), tug_payload())
        self.tugs.register_tug(TUG_OPERATOR, tug_payload())
        with self.assertRaises(PermissionDenied):
            self.service.create(TUG_OPERATOR, "VOY-9", plan_payload())
        # 拖轮调度员可以查看记录与拖轮
        self.service.list_records(TUG_OPERATOR)
        self.assertEqual(len(self.tugs.list_tugs(TUG_OPERATOR)), 1)
