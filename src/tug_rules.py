"""拖轮护航领域规则：最低马力、护航时间窗、选船与费用计算。纯规则，不接触持久化。"""
import math
from typing import Any, Dict, List, Optional, Tuple

from .domain import ValidationError, integer, number, text


ESCORT_DURATION_HOURS = 2
BASE_HP_PER_METER = 12.0
RISK_FACTOR = {"low": 1.0, "medium": 1.25, "high": 1.6}
DANGEROUS_GOODS_FACTOR = 1.2
DIRECTIONS = ("inbound", "outbound")
DAY_START_HOUR = 0
DAY_END_HOUR = 24

TUG_READ_ROLES = {"port_controller", "tug_dispatcher"}
TUG_WRITE_ROLES = {"port_controller", "tug_dispatcher"}


class TugRules:
    def known_role(self, role: str) -> bool:
        return role == "admin" or role in TUG_READ_ROLES

    def role_can_write(self, role: str) -> bool:
        return role == "admin" or role in TUG_WRITE_ROLES

    def validate_tug(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        p["name"] = text(p, "name")
        p["horsepower_hp"] = number(p, "horsepower_hp", 1)
        p["available_from_hour"] = integer(p, "available_from_hour", DAY_START_HOUR, DAY_END_HOUR - 1)
        p["available_to_hour"] = integer(p, "available_to_hour", DAY_START_HOUR + 1, DAY_END_HOUR)
        p["rate_per_hp_hour"] = number(p, "rate_per_hp_hour", 0)
        if p["available_to_hour"] <= p["available_from_hour"]:
            raise ValidationError("可用时段结束必须晚于开始")
        return p

    def min_required_hp(self, record_payload: Dict[str, Any]) -> int:
        """按船长与危险品等级计算护航最低总马力。"""
        length = float(record_payload.get("vessel_length_m", 0))
        risk = str(record_payload.get("risk_level", "medium"))
        factor = RISK_FACTOR.get(risk, RISK_FACTOR["medium"])
        if record_payload.get("dangerous_goods"):
            factor *= DANGEROUS_GOODS_FACTOR
        return int(math.ceil(length * BASE_HP_PER_METER * factor))

    def escort_windows(self, record_payload: Dict[str, Any]) -> Dict[str, Tuple[float, float]]:
        """进港护航自eta开始，出港护航在etd前完成，均限制在0-24时内。"""
        eta = int(record_payload.get("eta_hour", DAY_START_HOUR))
        etd = int(record_payload.get("etd_hour", DAY_END_HOUR))
        inbound = (float(max(DAY_START_HOUR, eta)), float(min(DAY_END_HOUR, eta + ESCORT_DURATION_HOURS)))
        outbound = (float(max(DAY_START_HOUR, etd - ESCORT_DURATION_HOURS)), float(min(DAY_END_HOUR, etd)))
        return {"inbound": inbound, "outbound": outbound}

    def covers(self, tug: Dict[str, Any], start: float, end: float) -> bool:
        return int(tug["available_from_hour"]) <= start and int(tug["available_to_hour"]) >= end

    @staticmethod
    def overlaps(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
        return a_start < b_end and a_end > b_start

    def tug_is_free(self, tug_id: int, active: List[Dict[str, Any]], start: float, end: float) -> bool:
        for item in active:
            if int(item["tug_id"]) != int(tug_id):
                continue
            if self.overlaps(start, end, float(item["window_start_hour"]), float(item["window_end_hour"])):
                return False
        return True

    def select_tugs(self, required_hp: float, candidates: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
        """优先选最小可独立完成的拖轮，否则按马力从大到小累加直至总推力达标。"""
        if not candidates:
            return None
        singles = [tug for tug in candidates if float(tug["horsepower_hp"]) >= required_hp]
        if singles:
            return [min(singles, key=lambda tug: (float(tug["horsepower_hp"]), str(tug["name"])))]
        ordered = sorted(candidates, key=lambda tug: (-float(tug["horsepower_hp"]), str(tug["name"])))
        selected: List[Dict[str, Any]] = []
        total = 0.0
        for tug in ordered:
            selected.append(tug)
            total += float(tug["horsepower_hp"])
            if total >= required_hp:
                return selected
        return None

    def escort_cost(self, actual_hp: float, actual_hours: float, rate_per_hp_hour: float) -> float:
        return round(float(actual_hp) * float(actual_hours) * float(rate_per_hp_hour), 2)
