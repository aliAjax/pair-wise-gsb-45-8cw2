"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit_legs(plans: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "leg": plan["leg"],
            "status": plan["status"],
            "required_hp": plan["required_hp"],
            "assigned_hp": plan["assigned_hp"],
            "reason_code": plan.get("reason_code", ""),
            "tugs": [tug["name"] for tug in plan["tugs"]],
        }
        for plan in plans
    ]


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tugs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    horsepower INTEGER NOT NULL,
                    available_from_hour INTEGER NOT NULL,
                    available_to_hour INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS escort_legs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    leg TEXT NOT NULL,
                    status TEXT NOT NULL,
                    start_hour INTEGER NOT NULL,
                    end_hour INTEGER NOT NULL,
                    required_hp INTEGER NOT NULL,
                    assigned_hp INTEGER NOT NULL DEFAULT 0,
                    reason_code TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    actual_horsepower INTEGER,
                    duration_hours REAL,
                    fee REAL,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_by TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    UNIQUE(record_id, leg)
                );
                CREATE TABLE IF NOT EXISTS tug_assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    leg_id INTEGER NOT NULL REFERENCES escort_legs(id) ON DELETE CASCADE,
                    tug_id INTEGER NOT NULL REFERENCES tugs(id),
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    leg TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_tug_assign_lookup ON tug_assignments(tug_id, status);
                CREATE INDEX IF NOT EXISTS idx_escort_legs_record ON escort_legs(record_id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], release_escorts: bool = False) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            if release_escorts:
                self.release_escorts(connection, record_id)
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    # ---- 拖轮登记 ----------------------------------------------------------

    def create_tug(self, tug: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO tugs(name,horsepower,available_from_hour,available_to_hour,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (tug["name"], tug["horsepower"], tug["available_from_hour"], tug["available_to_hour"], actor_id, now),
                )
                row = connection.execute("SELECT * FROM tugs WHERE id=?", (int(cursor.lastrowid),)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("拖轮名称已登记") from exc
        return dict(row)

    def list_tugs(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM tugs ORDER BY horsepower DESC, id").fetchall()
        return [dict(row) for row in rows]

    # ---- 护航查询 ----------------------------------------------------------

    def escort_board(self) -> List[Dict[str, Any]]:
        """每条靠泊计划的航段行（含已配拖轮），供派工服务做时段计算。"""
        with self._connect() as connection:
            leg_rows = connection.execute(
                "SELECT * FROM escort_legs ORDER BY record_id, leg"
            ).fetchall()
            assignment_rows = connection.execute(
                "SELECT ta.*, t.name AS tug_name, t.horsepower AS tug_horsepower "
                "FROM tug_assignments ta JOIN tugs t ON t.id = ta.tug_id ORDER BY ta.id"
            ).fetchall()
        tugs_by_leg: Dict[int, List[Dict[str, Any]]] = {}
        for row in assignment_rows:
            tugs_by_leg.setdefault(int(row["leg_id"]), []).append(
                {
                    "assignment_id": int(row["id"]),
                    "id": int(row["tug_id"]),
                    "name": row["tug_name"],
                    "horsepower": int(row["tug_horsepower"]),
                    "status": row["status"],
                    "leg": row["leg"],
                    "record_id": int(row["record_id"]),
                }
            )
        result = []
        for row in leg_rows:
            item = dict(row)
            item["tugs"] = tugs_by_leg.get(int(row["id"]), [])
            result.append(item)
        return result

    def busy_windows(self, connection: sqlite3.Connection) -> Dict[int, List[tuple]]:
        """已配拖轮占用的时间窗（仅allocated计占用，已完成/已释放不挡后续计划）。"""
        rows = connection.execute(
            "SELECT ta.tug_id AS tug_id, l.start_hour AS start_hour, l.end_hour AS end_hour "
            "FROM tug_assignments ta JOIN escort_legs l ON l.id = ta.leg_id "
            "WHERE ta.status=? AND l.status=?",
            ("allocated", "allocated"),
        ).fetchall()
        busy: Dict[int, List[tuple]] = {}
        for row in rows:
            busy.setdefault(int(row["tug_id"]), []).append((int(row["start_hour"]), int(row["end_hour"])))
        return busy

    # ---- 护航派工事务 ------------------------------------------------------

    def arrange_escorts(
        self,
        record_id: int,
        expected_version: int,
        planner: Any,
        actor_id: str,
    ) -> Dict[str, Any]:
        """为同一计划的进/出港航段一次性原子派工：事务内计算冲突、建航段行、登记占用、更新版本。

        planner(connection) -> (payload, plans)：规则计算留在服务层，事务由仓储持有。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version, payload FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")

            payload, plans = planner(connection)
            if not plans:
                connection.rollback()
                raise Conflict("没有需要派工的护航航段")

            for plan in plans:
                leg_id = self._upsert_leg(connection, record_id, plan, now, actor_id)
                connection.execute("DELETE FROM tug_assignments WHERE leg_id=?", (leg_id,))
                for tug in plan["tugs"]:
                    connection.execute(
                        "INSERT INTO tug_assignments(leg_id,tug_id,record_id,leg,status,created_at) VALUES(?,?,?,?,?,?)",
                        (leg_id, tug["id"], record_id, plan["leg"], plan["status"], now),
                    )

            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "escort_arranged", actor_id, version,
                 json.dumps({"legs": _audit_legs(plans)}, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    @staticmethod
    def _upsert_leg(connection: sqlite3.Connection, record_id: int, plan: Dict[str, Any], now: str, actor_id: str) -> int:
        existing = connection.execute(
            "SELECT id, status FROM escort_legs WHERE record_id=? AND leg=?", (record_id, plan["leg"])
        ).fetchone()
        if existing is not None:
            if existing["status"] != "pending":
                from .domain import Conflict as _Conflict
                raise _Conflict("%s护航已配妥，不能重复派工" % plan["leg"])
            connection.execute(
                "UPDATE escort_legs SET status=?,start_hour=?,end_hour=?,required_hp=?,assigned_hp=?,"
                "reason_code=?,reason=?,actual_horsepower=NULL,duration_hours=NULL,fee=NULL,"
                "version=version+1,updated_by=?,updated_at=? WHERE id=?",
                (plan["status"], plan["start_hour"], plan["end_hour"], plan["required_hp"],
                 plan["assigned_hp"], plan.get("reason_code", ""), plan.get("reason", ""),
                 actor_id, now, existing["id"]),
            )
            return int(existing["id"])
        cursor = connection.execute(
            "INSERT INTO escort_legs(record_id,leg,status,start_hour,end_hour,required_hp,assigned_hp,"
            "reason_code,reason,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (record_id, plan["leg"], plan["status"], plan["start_hour"], plan["end_hour"],
             plan["required_hp"], plan["assigned_hp"], plan.get("reason_code", ""),
             plan.get("reason", ""), actor_id, now),
        )
        return int(cursor.lastrowid)

    def complete_escort(
        self,
        record_id: int,
        expected_version: int,
        leg: str,
        completion: Dict[str, Any],
        payload: Dict[str, Any],
        actor_id: str,
    ) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            leg_row = connection.execute(
                "SELECT id FROM escort_legs WHERE record_id=? AND leg=?", (record_id, leg)
            ).fetchone()
            if leg_row is None:
                connection.rollback()
                raise NotFound("护航航段不存在")
            connection.execute(
                "UPDATE escort_legs SET status='completed',actual_horsepower=?,duration_hours=?,fee=?,"
                "version=version+1,updated_by=?,updated_at=? WHERE id=?",
                (completion["actual_horsepower"], completion["duration_hours"], completion["fee"],
                 actor_id, now, leg_row["id"]),
            )
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "escort_completed", actor_id, version,
                 json.dumps({"leg": leg, **completion}, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def release_escorts(self, connection: sqlite3.Connection, record_id: int) -> None:
        """取消计划时立即释放该计划占用的全部拖轮（同一事务内调用）。"""
        connection.execute(
            "UPDATE tug_assignments SET status='released' WHERE record_id=? AND status='allocated'",
            (record_id,),
        )
        connection.execute(
            "UPDATE escort_legs SET status='released',version=version+1,updated_at=? "
            "WHERE record_id=? AND status IN ('pending','allocated')",
            (_now(), record_id),
        )

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
