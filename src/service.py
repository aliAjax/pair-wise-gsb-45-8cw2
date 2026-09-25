"""业务用例编排、权限检查与审计。"""
import json
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, NotFound, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules
from .tug_rules import (
    ALLOCATED,
    COMPLETED,
    INBOUND,
    LEGS,
    PENDING,
    TugRules,
)


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None, tug_rules: TugRules = None) -> None:
        self.repository = repository
        self.rules = rules
        self.tug_rules = tug_rules or TugRules()
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
            release_escorts=(action == "cancel"),
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---- 拖轮登记 ----------------------------------------------------------

    def register_tug(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "register_tug"):
            raise PermissionDenied("角色无权登记拖轮")
        tug = self.tug_rules.validate_tug(payload or {})
        return self.repository.create_tug(tug, actor.user_id)

    def list_tugs(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_tugs()

    # ---- 护航派工 ----------------------------------------------------------

    def arrange_escorts(self, actor: Actor, record_id: int, expected_version: int) -> Dict[str, Any]:
        """为一条靠泊计划安排进/出港护航；推力不足或时段冲突的航段留在待配区并写明原因。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "arrange_escort"):
            raise PermissionDenied("角色无权安排护航")
        record = self.repository.get(record_id)
        if record["state"] not in {"draft", "confirmed"}:
            raise Conflict("当前状态不允许安排护航")
        expected_version = int(expected_version)
        tug_rules = self.tug_rules
        repository = self.repository

        def planner(connection):
            p = json.loads(connection.execute(
                "SELECT payload FROM records WHERE id=?", (record_id,)
            ).fetchone()["payload"])
            required_hp = tug_rules.required_horsepower(
                float(p["vessel_length_m"]), bool(p.get("dangerous_goods")), str(p.get("dangerous_class", ""))
            )
            windows = tug_rules.leg_windows(int(p["eta_hour"]), int(p["etd_hour"]))

            # 事务内读取占用，同一拖轮不会同时被两船抢到
            busy = repository.busy_windows(connection)
            legs = connection.execute(
                "SELECT leg, status FROM escort_legs WHERE record_id=?", (record_id,)
            ).fetchall()
            existing = {row["leg"]: row["status"] for row in legs}

            tugs = [dict(row) for row in connection.execute("SELECT * FROM tugs").fetchall()]
            plans = []
            for leg in LEGS:
                if existing.get(leg) not in (None, PENDING):
                    continue  # 已配妥或已结束的航段不重排
                plans.append(tug_rules.plan_leg(leg, required_hp, windows[leg], tugs, busy))
                # 本轮新占用即时入账，保证进/出港两航段不会挑中同一条拖轮
                if plans[-1]["status"] == ALLOCATED:
                    for tug in plans[-1]["tugs"]:
                        busy.setdefault(int(tug["id"]), []).append(windows[leg])

            escort = dict(p.get("escort") or {})
            escort.update(tug_rules.summarize(plans))
            escort["required_hp"] = required_hp
            p["escort"] = escort
            return p, plans

        return repository.arrange_escorts(record_id, expected_version, planner, actor.user_id)

    def escort_board(self, actor: Actor, only_pending: bool = False) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        rows = self.repository.escort_board()
        if only_pending:
            rows = [row for row in rows if row["status"] == PENDING]
        return rows

    def complete_escort(self, actor: Actor, record_id: int, expected_version: int, leg: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """护航结束：写入实际马力、时长和费用，并释放该航段的拖轮占用。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "complete_escort"):
            raise PermissionDenied("角色无权结束护航")
        leg = text({"leg": leg}, "leg")
        if leg not in LEGS:
            from .domain import ValidationError
            raise ValidationError("leg只能是%s/%s" % LEGS)
        record = self.repository.get(record_id)
        escort = record["payload"].get("escort") or {}
        leg_info = escort.get(leg)
        if not leg_info:
            raise NotFound("护航航段尚未安排")
        if leg_info.get("status") != ALLOCATED:
            raise Conflict("护航航段不是已配状态，无法结束")
        completion = self.tug_rules.validate_completion(data or {}, leg_info)

        new_leg = dict(leg_info)
        new_leg.update(status=COMPLETED, **completion)
        escort[leg] = new_leg
        payload = dict(record["payload"])
        payload["escort"] = escort
        return self.repository.complete_escort(
            record_id=record_id,
            expected_version=int(expected_version),
            leg=leg,
            completion=completion,
            payload=payload,
            actor_id=actor.user_id,
        )
