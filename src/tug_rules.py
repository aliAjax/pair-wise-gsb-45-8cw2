"""拖轮派工规则：最低马力、护航时段、选轮冲突与费用，全部为纯函数，不接触持久化。"""
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .domain import ValidationError, integer, number, text


INBOUND = "inbound"
OUTBOUND = "outbound"
LEGS = (INBOUND, OUTBOUND)
LEG_LABELS = {INBOUND: "进港", OUTBOUND: "出港"}

# 护航作业占用的时间窗长度（小时）
ESCORT_WINDOW_HOURS = 2
DAY_START = 0
DAY_END = 24

# 每米船长需要的基准马力：180米船 -> 1440马力
LENGTH_FACTOR_HP_PER_M = 8
# 危险品（IMDG 1~9 类）推力系数：高危品类需要更大推力
DANGEROUS_FACTORS = {
    "1": 1.50,  # 爆炸品
    "2": 1.30,  # 气体
    "3": 1.30,  # 易燃液体
    "4": 1.20,  # 易燃固体
    "5": 1.30,  # 氧化剂/过氧化物
    "6": 1.30,  # 毒害感染品
    "7": 1.50,  # 放射性
    "8": 1.15,  # 腐蚀品
    "9": 1.10,  # 杂类危险品
}
DANGEROUS_CLASSES = tuple(DANGEROUS_FACTORS)

# 单价：元 / (马力·小时)
RATE_PER_HP_HOUR = 1.5

PENDING = "pending"        # 待配
ALLOCATED = "allocated"    # 已配
COMPLETED = "completed"    # 护航结束
RELEASED = "released"      # 计划取消，已释放

SHORTAGE = "horsepower_shortage"
TIME_CONFLICT = "time_conflict"
CANCELLED = "cancelled"

Window = Tuple[int, int]


class TugRules:
    """拖轮资料校验与派工规则。"""

    # ---- 资料登记 ----------------------------------------------------------

    def validate_tug(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = text(payload, "name")
        horsepower = integer(payload, "horsepower", 1, 200000)
        available_from = integer(payload, "available_from_hour", DAY_START, DAY_END - 1)
        available_to = integer(payload, "available_to_hour", DAY_START + 1, DAY_END)
        if available_to <= available_from:
            raise ValidationError("可用时段结束必须晚于开始")
        return {
            "name": name,
            "horsepower": horsepower,
            "available_from_hour": available_from,
            "available_to_hour": available_to,
        }

    # ---- 最低马力 ----------------------------------------------------------

    def required_horsepower(self, vessel_length_m: float, dangerous_goods: bool, dangerous_class: str) -> int:
        """按船长与危险品等级计算护航最低总马力。"""
        base = float(vessel_length_m) * LENGTH_FACTOR_HP_PER_M
        factor = 1.0
        if dangerous_goods:
            factor = DANGEROUS_FACTORS.get(str(dangerous_class))
            if factor is None:
                raise ValidationError("危险品等级必须是1~9类")
        return int(math.ceil(base * factor))

    # ---- 时段 --------------------------------------------------------------

    def leg_windows(self, eta_hour: int, etd_hour: int) -> Dict[str, Window]:
        """进港护航覆盖抵港时刻，出港护航覆盖离港时刻，均为半开区间[起,止)。"""
        inbound_end = min(DAY_END, eta_hour + ESCORT_WINDOW_HOURS)
        outbound_start = max(DAY_START, etd_hour - ESCORT_WINDOW_HOURS)
        return {
            INBOUND: (int(eta_hour), int(inbound_end)),
            OUTBOUND: (int(outbound_start), int(etd_hour)),
        }

    @staticmethod
    def _overlap(first: Window, second: Window) -> bool:
        return first[0] < second[1] and second[0] < first[1]

    @staticmethod
    def _covers(availability: Window, window: Window) -> bool:
        return availability[0] <= window[0] and window[1] <= availability[1]

    # ---- 选轮 --------------------------------------------------------------

    def plan_leg(
        self,
        leg: str,
        required_hp: int,
        window: Window,
        tugs: Sequence[Dict[str, Any]],
        busy: Dict[int, List[Window]],
    ) -> Dict[str, Any]:
        """为一个航段挑拖轮；挑不出时返回待配及原因（推力不足/时段冲突）。"""
        label = LEG_LABELS.get(leg, leg)
        span = "%s-%s时" % window

        free: List[Dict[str, Any]] = []
        occupied: List[Dict[str, Any]] = []
        for tug in tugs:
            availability = (int(tug["available_from_hour"]), int(tug["available_to_hour"]))
            if not self._covers(availability, window):
                continue
            if any(self._overlap(window, item) for item in busy.get(int(tug["id"]), [])):
                occupied.append(tug)
            else:
                free.append(tug)

        # 大马力优先，尽快凑足总推力
        free.sort(key=lambda item: int(item["horsepower"]), reverse=True)
        picks: List[Dict[str, Any]] = []
        assigned_hp = 0
        for tug in free:
            if assigned_hp >= required_hp:
                break
            picks.append(tug)
            assigned_hp += int(tug["horsepower"])

        result: Dict[str, Any] = {
            "leg": leg,
            "start_hour": window[0],
            "end_hour": window[1],
            "required_hp": int(required_hp),
        }
        if assigned_hp >= required_hp:
            result.update(
                status=ALLOCATED,
                assigned_hp=assigned_hp,
                reason_code="",
                reason="",
                tugs=[
                    {"id": int(tug["id"]), "name": tug["name"], "horsepower": int(tug["horsepower"])}
                    for tug in picks
                ],
            )
            return result

        fleet_hp = sum(int(tug["horsepower"]) for tug in free) + sum(int(tug["horsepower"]) for tug in occupied)
        free_hp = sum(int(tug["horsepower"]) for tug in free)
        result.update(status=PENDING, assigned_hp=0, tugs=[])
        if fleet_hp < required_hp:
            result.update(
                reason_code=SHORTAGE,
                reason="%s护航推力不足：%s最低需%s马力，时段内拖轮合计仅%s马力"
                % (label, span, required_hp, fleet_hp),
            )
        else:
            names = "、".join("%s(%s马力)" % (tug["name"], tug["horsepower"]) for tug in occupied)
            result.update(
                reason_code=TIME_CONFLICT,
                reason="%s护航时段冲突：%s空闲拖轮合计%s马力低于需求%s，%s已被其他船舶占用"
                % (label, span, free_hp, required_hp, names),
            )
        return result

    # ---- 护航结束 ----------------------------------------------------------

    def validate_completion(self, data: Dict[str, Any], leg: Dict[str, Any]) -> Dict[str, Any]:
        actual_hp = integer(data, "actual_horsepower", 1)
        max_duration = int(leg["end_hour"]) - int(leg["start_hour"])
        duration_hours = number(data, "duration_hours", 0.01, float(max_duration))
        fee = self.completion_fee(actual_hp, duration_hours)
        return {
            "actual_horsepower": actual_hp,
            "duration_hours": round(duration_hours, 2),
            "fee": fee,
        }

    @staticmethod
    def completion_fee(actual_horsepower: int, duration_hours: float) -> float:
        return round(actual_horsepower * duration_hours * RATE_PER_HP_HOUR, 2)

    # ---- 结果整形 ----------------------------------------------------------

    @staticmethod
    def summarize(legs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """把航段行（含拖轮清单）整形为记录payload中的escort结构。"""
        summary: Dict[str, Any] = {}
        for row in legs:
            item: Dict[str, Any] = {
                "status": row["status"],
                "start_hour": int(row["start_hour"]),
                "end_hour": int(row["end_hour"]),
                "required_hp": int(row["required_hp"]),
                "assigned_hp": int(row["assigned_hp"] or 0),
                "reason_code": row.get("reason_code") or "",
                "reason": row.get("reason") or "",
                "tugs": list(row.get("tugs") or []),
            }
            if row["status"] == COMPLETED:
                item.update(
                    actual_horsepower=int(row["actual_horsepower"]),
                    duration_hours=float(row["duration_hours"]),
                    fee=float(row["fee"]),
                )
            summary[row["leg"]] = item
        return summary
