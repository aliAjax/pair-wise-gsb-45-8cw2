import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


CREATE_DATA = {'vessel': 'HaiYun', 'berth': 'B12', 'vessel_length_m': 180, 'berth_length_m': 220, 'draft_m': 10.2, 'berth_depth_m': 11.5, 'eta_hour': 6, 'etd_hour': 18, 'risk_level': 'medium', 'dangerous_goods': False, 'dangerous_class': ''}
CONTROLLER = Actor("operator", "port_controller")
DISPATCHER = Actor("dispatcher", "tug_dispatcher")
TUG_A = {'name': 'Tug-A', 'horsepower': 1200, 'available_from_hour': 0, 'available_to_hour': 24}
TUG_B = {'name': 'Tug-B', 'horsepower': 1200, 'available_from_hour': 0, 'available_to_hour': 24}

# (动作类型, 动作, 数据, 期望状态) —— escort 由测试直接调用服务方法
FLOW = [('action', 'confirm', {'pilot_id': 'P-01'}, 'confirmed'),
        ('action', 'berth', {'actual_draft_m': 10.3}, 'berthed'),
        ('action', 'depart', {'cargo_operation_complete': True}, 'departed')]


def register_tugs(service):
    service.register_tug(CONTROLLER, TUG_A)
    service.register_tug(CONTROLLER, TUG_B)


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _drive_flow(self, reference):
        register_tugs(self.service)
        record = self.service.create(Actor("creator", "port_controller"), reference, CREATE_DATA)
        self.assertEqual(record["state"], "draft")

        # 排船同时安排进/出港护航
        record = self.service.arrange_escorts(DISPATCHER, record["id"], record["version"])
        escort = record["payload"]["escort"]
        self.assertEqual(escort["required_hp"], 1440)
        self.assertEqual(escort["inbound"]["status"], "allocated")
        self.assertEqual(escort["outbound"]["status"], "allocated")

        record = self.service.act(CONTROLLER, record["id"], record["version"], "confirm", {'pilot_id': 'P-01'})
        self.assertEqual(record["state"], "confirmed")

        # 进港护航结束：写入实际马力、时长、费用
        record = self.service.complete_escort(
            DISPATCHER, record["id"], record["version"], "inbound",
            {'actual_horsepower': 1500, 'duration_hours': 2},
        )
        inbound = record["payload"]["escort"]["inbound"]
        self.assertEqual(inbound["status"], "completed")
        self.assertEqual(inbound["fee"], 4500.0)

        record = self.service.act(CONTROLLER, record["id"], record["version"], "berth", {'actual_draft_m': 10.3})
        self.assertEqual(record["state"], "berthed")

        record = self.service.complete_escort(
            DISPATCHER, record["id"], record["version"], "outbound",
            {'actual_horsepower': 1500, 'duration_hours': 2},
        )
        record = self.service.act(CONTROLLER, record["id"], record["version"], "depart", {'cargo_operation_complete': True})
        self.assertEqual(record["state"], "departed")
        return record

    def test_complete_workflow_and_audit(self):
        record = self._drive_flow("VOY-21001")
        timeline = self.service.timeline(Actor("creator", "port_controller"), record["id"])
        # created, escort_arranged, confirm, escort_completed, berth, escort_completed, depart
        self.assertEqual([event["action"] for event in timeline],
                         ['created', 'escort_arranged', 'confirm', 'escort_completed',
                          'berth', 'escort_completed', 'depart'])

    def test_berth_blocked_without_inbound_escort(self):
        register_tugs(self.service)
        record = self.service.create(Actor("creator", "port_controller"), "VOY-21002", CREATE_DATA)
        record = self.service.act(CONTROLLER, record["id"], record["version"], "confirm", {'pilot_id': 'P-01'})
        with self.assertRaises(Conflict):
            self.service.act(CONTROLLER, record["id"], record["version"], "berth", {'actual_draft_m': 10.3})
