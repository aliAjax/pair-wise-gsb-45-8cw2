import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src.tug_rules import COMPLETED, PENDING, SHORTAGE, TIME_CONFLICT, TugRules


CONTROLLER = Actor("operator", "port_controller")
DISPATCHER = Actor("dispatcher", "tug_dispatcher")
OUTSIDER = Actor("outsider", "outsider")

PLAN = {'vessel': 'HaiYun', 'berth': 'B12', 'vessel_length_m': 180, 'berth_length_m': 220,
        'draft_m': 10.2, 'berth_depth_m': 11.5, 'eta_hour': 6, 'etd_hour': 18,
        'risk_level': 'medium', 'dangerous_goods': False, 'dangerous_class': ''}
PLAN_EARLY = dict(PLAN, berth='B13', eta_hour=2, etd_hour=4)


def tug(name, hp, frm=0, to=24):
    return {'name': name, 'horsepower': hp, 'available_from_hour': frm, 'available_to_hour': to}


class TugRuleTest(unittest.TestCase):
    def setUp(self):
        self.rules = TugRules()

    def test_required_horsepower_by_length_and_dangerous_class(self):
        self.assertEqual(self.rules.required_horsepower(180, False, ''), 1440)
        # 180米 * 8 = 1440，爆炸品/放射性系数1.5 -> 2160
        self.assertEqual(self.rules.required_horsepower(180, True, '1'), 2160)
        # 腐蚀品系数1.15，向上取整
        self.assertEqual(self.rules.required_horsepower(180, True, '8'), 1656)
        with self.assertRaises(ValidationError):
            self.rules.required_horsepower(180, True, 'X')

    def test_windows(self):
        windows = self.rules.leg_windows(6, 18)
        self.assertEqual(windows['inbound'], (6, 8))
        self.assertEqual(windows['outbound'], (16, 18))
        self.assertEqual(self.rules.leg_windows(23, 24)['inbound'], (23, 24))
        self.assertEqual(self.rules.leg_windows(0, 2)['outbound'], (0, 2))

    def test_allocation_shortage_and_conflict(self):
        tugs = [{'id': 1, **tug('small', 800)}]
        plan = self.rules.plan_leg('inbound', 1440, (6, 8), tugs, {})
        self.assertEqual(plan['status'], PENDING)
        self.assertEqual(plan['reason_code'], SHORTAGE)

        tugs = [{'id': 1, **tug('A', 1000)}, {'id': 2, **tug('B', 1000)}]
        # A在该窗口已被别的船占用，只剩B 1000 < 1440，但全队合计2000够用 -> 时段冲突
        plan = self.rules.plan_leg('inbound', 1440, (6, 8), tugs, {1: [(6, 8)]})
        self.assertEqual(plan['status'], PENDING)
        self.assertEqual(plan['reason_code'], TIME_CONFLICT)

        # A被占用窗口与护航窗不重叠，仍可参与，凑足马力
        plan = self.rules.plan_leg('inbound', 1440, (6, 8), tugs, {1: [(8, 10)]})
        self.assertEqual(plan['status'], 'allocated')

    def test_completion_fee(self):
        self.assertEqual(self.rules.completion_fee(1500, 2), 4500.0)
        leg = {'start_hour': 6, 'end_hour': 8}
        result = self.rules.validate_completion({'actual_horsepower': 1500, 'duration_hours': 2}, leg)
        self.assertEqual(result['fee'], 4500.0)
        with self.assertRaises(ValidationError):
            self.rules.validate_completion({'actual_horsepower': 1500, 'duration_hours': 3}, leg)


class TugDispatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _plan(self, ref, data=PLAN, berth=None):
        if berth is not None:
            data = dict(data, berth=berth)
        return self.service.create(Actor("creator", "port_controller"), ref, data)

    def test_register_permission_and_duplicate(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_tug(OUTSIDER, tug('A', 1000))
        self.service.register_tug(CONTROLLER, tug('A', 1000))
        with self.assertRaises(Conflict):
            self.service.register_tug(CONTROLLER, tug('A', 1000))

    def test_pending_board_when_fleet_too_weak(self):
        self.service.register_tug(CONTROLLER, tug('small', 500))
        record = self._plan('VOY-W1')
        record = self.service.arrange_escorts(DISPATCHER, record['id'], record['version'])
        escort = record['payload']['escort']
        self.assertEqual(escort['inbound']['status'], PENDING)
        self.assertEqual(escort['inbound']['reason_code'], SHORTAGE)
        board = self.service.escort_board(DISPATCHER, only_pending=True)
        self.assertEqual(len(board), 2)

        # 补登记大马力拖轮后重排，进港配齐
        self.service.register_tug(CONTROLLER, tug('big', 2000))
        record = self.service.arrange_escorts(DISPATCHER, record['id'], record['version'])
        self.assertEqual(record['payload']['escort']['inbound']['status'], 'allocated')
        self.assertEqual(record['payload']['escort']['outbound']['status'], 'allocated')
        self.assertEqual(self.service.escort_board(DISPATCHER, only_pending=True), [])

    def test_same_tug_not_double_booked(self):
        self.service.register_tug(CONTROLLER, tug('A', 1000))
        self.service.register_tug(CONTROLLER, tug('B', 1000))
        # 两船ETA相同，各需1440；两条1000的拖轮只能凑齐其中一条
        first = self._plan('VOY-C1', berth='B12')
        second = self._plan('VOY-C2', berth='B13')
        first = self.service.arrange_escorts(DISPATCHER, first['id'], first['version'])
        second = self.service.arrange_escorts(DISPATCHER, second['id'], second['version'])
        used_first = {t['id'] for t in first['payload']['escort']['inbound']['tugs']}
        self.assertEqual(first['payload']['escort']['inbound']['status'], 'allocated')
        self.assertEqual(second['payload']['escort']['inbound']['status'], PENDING)
        self.assertEqual(second['payload']['escort']['inbound']['reason_code'], TIME_CONFLICT)
        self.assertTrue(used_first)

        # 错峰船（ETA=2，与6-8不重叠）仍能派到同一条拖轮
        third = self._plan('VOY-C3', PLAN_EARLY)
        third = self.service.arrange_escorts(DISPATCHER, third['id'], third['version'])
        self.assertEqual(third['payload']['escort']['inbound']['status'], 'allocated')

    def test_cancel_releases_tugs_for_reassignment(self):
        self.service.register_tug(CONTROLLER, tug('A', 1000))
        self.service.register_tug(CONTROLLER, tug('B', 1000))
        first = self._plan('VOY-R1', berth='B12')
        second = self._plan('VOY-R2', berth='B13')
        first = self.service.arrange_escorts(DISPATCHER, first['id'], first['version'])
        second = self.service.arrange_escorts(DISPATCHER, second['id'], second['version'])
        self.assertEqual(second['payload']['escort']['inbound']['status'], PENDING)

        # 取消第一条计划，占用立即释放，第二条重排即可配齐
        self.service.act(CONTROLLER, first['id'], first['version'], 'cancel', {'cancel_reason': '变更'})
        board = self.service.escort_board(DISPATCHER)
        self.assertTrue(all(row['status'] == 'released' for row in board if row['record_id'] == first['id']))
        second = self.service.arrange_escorts(DISPATCHER, second['id'], second['version'])
        self.assertEqual(second['payload']['escort']['inbound']['status'], 'allocated')

    def test_completion_writes_actuals_and_frees_tug(self):
        self.service.register_tug(CONTROLLER, tug('A', 2000))
        record = self._plan('VOY-D1', berth='B12')
        record = self.service.arrange_escorts(DISPATCHER, record['id'], record['version'])
        record = self.service.complete_escort(
            DISPATCHER, record['id'], record['version'], 'inbound',
            {'actual_horsepower': 1900, 'duration_hours': 1.5},
        )
        inbound = record['payload']['escort']['inbound']
        self.assertEqual(inbound['status'], COMPLETED)
        self.assertEqual(inbound['actual_horsepower'], 1900)
        self.assertEqual(inbound['duration_hours'], 1.5)
        self.assertEqual(inbound['fee'], 4275.0)

        # 已完成的航段不能重复结束
        with self.assertRaises(Conflict):
            self.service.complete_escort(
                DISPATCHER, record['id'], record['version'], 'inbound',
                {'actual_horsepower': 1900, 'duration_hours': 1.5},
            )

        # 护航完成后该拖轮不再占用，别的船同窗口可派
        other = self._plan('VOY-D2', berth='B13')
        other = self.service.arrange_escorts(DISPATCHER, other['id'], other['version'])
        self.assertEqual(other['payload']['escort']['inbound']['status'], 'allocated')

    def test_completed_leg_not_rearranged(self):
        self.service.register_tug(CONTROLLER, tug('A', 2000))
        record = self._plan('VOY-N1')
        record = self.service.arrange_escorts(DISPATCHER, record['id'], record['version'])
        self.assertEqual(record['payload']['escort']['outbound']['status'], 'allocated')
        record = self.service.complete_escort(
            DISPATCHER, record['id'], record['version'], 'inbound',
            {'actual_horsepower': 2000, 'duration_hours': 2},
        )
        # 进港已完成、出港已配齐：重复派工直接拒绝，进港实际值保持不变
        with self.assertRaises(Conflict):
            self.service.arrange_escorts(DISPATCHER, record['id'], record['version'])
        record = self.service.get_record(DISPATCHER, record['id'])
        self.assertEqual(record['payload']['escort']['inbound']['status'], COMPLETED)
        self.assertEqual(record['payload']['escort']['inbound']['actual_horsepower'], 2000)
