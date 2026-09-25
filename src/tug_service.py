"""拖轮登记、护航派工、待配区与护航结算的用例编排。"""
from typing import Any, Dict, List

from .domain import Actor, Conflict, PermissionDenied, ValidationError, number
from .repository import Repository
from .tug_repository import TugRepository
from .tug_rules import DIRECTIONS, TugRules


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


class TugService:
    def __init__(self, repository: Repository, tug_repository: TugRepository, rules: TugRules) -> None:
        self.repository = repository
        self.tug_repository = tug_repository
        self.rules = rules

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _ensure_write_role(self, actor: Actor) -> None:
        if not self.rules.role_can_write(actor.role):
            raise PermissionDenied("角色无权执行拖轮操作")

    # ---- 拖轮登记 ----
    def register_tug(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_write_role(actor)
        data = self.rules.validate_tug(payload or {})
        return self.tug_repository.create_tug(data, actor.user_id)

    def list_tugs(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        tugs = self.tug_repository.list_tugs()
        busy: Dict[int, List[Dict[str, Any]]] = {}
        for item in self.tug_repository.active_assignments():
            busy.setdefault(int(item["tug_id"]), []).append(
                {
                    "assignment_id": item["assignment_id"],
                    "record_id": item["record_id"],
                    "direction": item["direction"],
                    "window_start_hour": item["window_start_hour"],
                    "window_end_hour": item["window_end_hour"],
                }
            )
        return [dict(tug, active_assignments=busy.get(int(tug["id"]), [])) for tug in tugs]

    # ---- 护航派工 ----
    def dispatch_new_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """新计划创建后立即为进出港各安排一次护航，失败则留在待配区。"""
        required = self.rules.min_required_hp(record["payload"])
        windows = self.rules.escort_windows(record["payload"])
        for direction in DIRECTIONS:
            start, end = windows[direction]
            leg = self.tug_repository.ensure_leg(record["id"], direction, start, end, required)
            self._attempt_leg(leg)
        return self._escorts_payload(self.repository.get(record["id"]))

    def retry_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_write_role(actor)
        record = self.repository.get(record_id)
        if record["state"] in {"cancelled", "departed"}:
            raise Conflict("计划已结束，无法重新派工")
        for leg in self.tug_repository.legs_for_record(record_id):
            if leg["status"] == "pending":
                self._attempt_leg(leg)
        return self._escorts_payload(record)

    def _attempt_leg(self, leg: Dict[str, Any]) -> None:
        start = float(leg["window_start_hour"])
        end = float(leg["window_end_hour"])
        required = float(leg["required_hp"])
        active = self.tug_repository.active_assignments()
        candidates = [
            tug
            for tug in self.tug_repository.list_tugs()
            if self.rules.covers(tug, start, end) and self.rules.tug_is_free(tug["id"], active, start, end)
        ]
        if not candidates:
            self.tug_repository.mark_leg(leg["id"], "pending", "可用时段内无空闲拖轮")
            return
        selected = self.rules.select_tugs(required, candidates)
        if selected is None:
            total = sum(float(tug["horsepower_hp"]) for tug in candidates)
            reason = "总推力不足：需要%s马力，时段内可用%s马力" % (_fmt(required), _fmt(total))
            self.tug_repository.mark_leg(leg["id"], "pending", reason)
            return
        try:
            self.tug_repository.assign_leg(leg["id"], [int(tug["id"]) for tug in selected])
        except Conflict:
            self.tug_repository.mark_leg(leg["id"], "pending", "派工时拖轮时段冲突，请重试")

    def release_for_record(self, record_id: int) -> Dict[str, int]:
        return self.tug_repository.release_for_record(record_id)

    # ---- 查询 ----
    def record_escorts(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._escorts_payload(self.repository.get(record_id))

    def _escorts_payload(self, record: Dict[str, Any]) -> Dict[str, Any]:
        legs = []
        for leg in self.tug_repository.legs_for_record(record["id"]):
            assigned_hp = sum(float(a["tug_hp"]) for a in leg["assignments"] if a["status"] == "assigned")
            legs.append(dict(leg, assigned_hp=assigned_hp))
        return {
            "record_id": record["id"],
            "reference": record["reference"],
            "state": record["state"],
            "min_required_hp": self.rules.min_required_hp(record["payload"]),
            "escort_status": self._summary_status(legs),
            "legs": legs,
        }

    @staticmethod
    def _summary_status(legs: List[Dict[str, Any]]) -> str:
        if not legs:
            return "none"
        statuses = {leg["status"] for leg in legs}
        if statuses == {"completed"}:
            return "completed"
        if statuses == {"released"}:
            return "released"
        if "pending" in statuses:
            return "pending"
        if statuses == {"assigned"}:
            return "assigned"
        return "mixed"

    def pending(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.tug_repository.pending_legs()

    # ---- 护航结算 ----
    def complete_assignment(self, actor: Actor, assignment_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_write_role(actor)
        data = dict(data or {})
        assignment = self.tug_repository.get_assignment(assignment_id)
        if assignment["status"] != "assigned":
            raise Conflict("护航任务不在执行中")
        hours = number(data, "actual_hours", 0)
        if hours <= 0:
            raise ValidationError("actual_hours必须大于0")
        if data.get("actual_hp") is None:
            actual_hp = float(assignment["tug_hp"])
        else:
            actual_hp = number(data, "actual_hp", 0)
            if actual_hp <= 0:
                raise ValidationError("actual_hp必须大于0")
        cost = self.rules.escort_cost(actual_hp, hours, float(assignment["rate_per_hp_hour"]))
        return self.tug_repository.complete_assignment(assignment_id, actual_hp, hours, cost)
