"""拖轮与护航派工的SQLite持久化：拖轮登记、护航航段、派工任务。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TugRepository:
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
                CREATE TABLE IF NOT EXISTS tugs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    horsepower_hp REAL NOT NULL,
                    available_from_hour INTEGER NOT NULL,
                    available_to_hour INTEGER NOT NULL,
                    rate_per_hp_hour REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS escort_legs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    direction TEXT NOT NULL,
                    status TEXT NOT NULL,
                    window_start_hour REAL NOT NULL,
                    window_end_hour REAL NOT NULL,
                    required_hp REAL NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(record_id, direction)
                );
                CREATE TABLE IF NOT EXISTS escort_assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    leg_id INTEGER NOT NULL REFERENCES escort_legs(id) ON DELETE CASCADE,
                    tug_id INTEGER NOT NULL REFERENCES tugs(id),
                    status TEXT NOT NULL,
                    actual_hp REAL,
                    actual_hours REAL,
                    cost REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_escort_legs_record ON escort_legs(record_id);
                CREATE INDEX IF NOT EXISTS idx_escort_legs_status ON escort_legs(status);
                CREATE INDEX IF NOT EXISTS idx_escort_assign_leg ON escort_assignments(leg_id);
                CREATE INDEX IF NOT EXISTS idx_escort_assign_tug ON escort_assignments(tug_id, status);
                """
            )

    # ---- 拖轮登记 ----
    def create_tug(self, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO tugs(name,horsepower_hp,available_from_hour,available_to_hour,rate_per_hp_hour,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (data["name"], data["horsepower_hp"], data["available_from_hour"], data["available_to_hour"], data["rate_per_hp_hour"], actor_id, _now()),
                )
                row = connection.execute("SELECT * FROM tugs WHERE id=?", (int(cursor.lastrowid),)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("拖轮名称已存在") from exc
        return dict(row)

    def list_tugs(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM tugs ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def get_tug(self, tug_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tugs WHERE id=?", (tug_id,)).fetchone()
        if row is None:
            raise NotFound("拖轮不存在")
        return dict(row)

    # ---- 护航航段 ----
    def ensure_leg(self, record_id: int, direction: str, start: float, end: float, required_hp: float) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO escort_legs(record_id,direction,status,window_start_hour,window_end_hour,required_hp,reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (record_id, direction, "pending", start, end, required_hp, "", now, now),
            )
            row = connection.execute("SELECT * FROM escort_legs WHERE record_id=? AND direction=?", (record_id, direction)).fetchone()
        return dict(row)

    def get_leg(self, leg_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM escort_legs WHERE id=?", (leg_id,)).fetchone()
        if row is None:
            raise NotFound("护航航段不存在")
        return dict(row)

    def legs_for_record(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            legs = connection.execute("SELECT * FROM escort_legs WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
            result = []
            for leg in legs:
                item = dict(leg)
                rows = connection.execute(
                    "SELECT a.*, t.name AS tug_name, t.horsepower_hp AS tug_hp FROM escort_assignments a JOIN tugs t ON t.id=a.tug_id WHERE a.leg_id=? ORDER BY a.id",
                    (item["id"],),
                ).fetchall()
                item["assignments"] = [dict(row) for row in rows]
                result.append(item)
        return result

    def mark_leg(self, leg_id: int, status: str, reason: str = "") -> Dict[str, Any]:
        with self._connect() as connection:
            cursor = connection.execute("UPDATE escort_legs SET status=?, reason=?, updated_at=? WHERE id=?", (status, reason, _now(), leg_id))
            if cursor.rowcount == 0:
                raise NotFound("护航航段不存在")
        return self.get_leg(leg_id)

    def pending_legs(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT l.id AS leg_id, l.record_id, l.direction, l.status, l.window_start_hour, l.window_end_hour,
                       l.required_hp, l.reason, l.created_at, l.updated_at, r.reference, r.state, r.payload
                FROM escort_legs l JOIN records r ON r.id = l.record_id
                WHERE l.status = 'pending' AND r.state NOT IN ('cancelled','departed')
                ORDER BY l.id
                """
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            payload = json.loads(item.pop("payload"))
            item["vessel"] = payload.get("vessel", "")
            result.append(item)
        return result

    # ---- 派工任务 ----
    def active_assignments(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT a.id AS assignment_id, a.leg_id, a.tug_id, l.record_id, l.direction, l.window_start_hour, l.window_end_hour
                FROM escort_assignments a JOIN escort_legs l ON l.id = a.leg_id
                WHERE a.status = 'assigned'
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def assign_leg(self, leg_id: int, tug_ids: List[int]) -> Dict[str, Any]:
        """在单事务内复核时段冲突并写入派工，保证同一拖轮不会同时接两船。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            leg = connection.execute("SELECT * FROM escort_legs WHERE id=?", (leg_id,)).fetchone()
            if leg is None:
                connection.rollback()
                raise NotFound("护航航段不存在")
            if leg["status"] != "pending":
                connection.rollback()
                raise Conflict("护航航段不在待配状态")
            for tug_id in tug_ids:
                clash = connection.execute(
                    """
                    SELECT a.id FROM escort_assignments a
                    JOIN escort_legs l ON l.id = a.leg_id
                    WHERE a.status='assigned' AND a.tug_id=? AND l.id<>?
                      AND l.window_start_hour < ? AND l.window_end_hour > ?
                    """,
                    (tug_id, leg_id, leg["window_end_hour"], leg["window_start_hour"]),
                ).fetchone()
                if clash is not None:
                    connection.rollback()
                    raise Conflict("拖轮时段冲突")
            for tug_id in tug_ids:
                connection.execute(
                    "INSERT INTO escort_assignments(leg_id,tug_id,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (leg_id, tug_id, "assigned", now, now),
                )
            connection.execute("UPDATE escort_legs SET status='assigned', reason='', updated_at=? WHERE id=?", (now, leg_id))
            connection.commit()
        return self.get_leg(leg_id)

    def release_for_record(self, record_id: int) -> Dict[str, int]:
        """计划取消时立即释放未完成的护航派工。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE escort_assignments SET status='released', updated_at=? WHERE status='assigned' AND leg_id IN (SELECT id FROM escort_legs WHERE record_id=?)",
                (now, record_id),
            )
            assignments = cursor.rowcount
            cursor = connection.execute(
                "UPDATE escort_legs SET status='released', reason='计划取消，护航已释放', updated_at=? WHERE record_id=? AND status IN ('pending','assigned')",
                (now, record_id),
            )
            legs = cursor.rowcount
            connection.commit()
        return {"legs_released": legs, "assignments_released": assignments}

    def get_assignment(self, assignment_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT a.*, l.record_id, l.direction, l.window_start_hour, l.window_end_hour, l.required_hp,
                       t.name AS tug_name, t.horsepower_hp AS tug_hp, t.rate_per_hp_hour
                FROM escort_assignments a
                JOIN escort_legs l ON l.id = a.leg_id
                JOIN tugs t ON t.id = a.tug_id
                WHERE a.id=?
                """,
                (assignment_id,),
            ).fetchone()
        if row is None:
            raise NotFound("护航任务不存在")
        return dict(row)

    def complete_assignment(self, assignment_id: int, actual_hp: float, actual_hours: float, cost: float) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM escort_assignments WHERE id=?", (assignment_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("护航任务不存在")
            if row["status"] != "assigned":
                connection.rollback()
                raise Conflict("护航任务不在执行中")
            connection.execute(
                "UPDATE escort_assignments SET status='completed', actual_hp=?, actual_hours=?, cost=?, updated_at=? WHERE id=?",
                (actual_hp, actual_hours, cost, now, assignment_id),
            )
            remaining = connection.execute(
                "SELECT COUNT(*) AS total FROM escort_assignments WHERE leg_id=? AND status='assigned'", (row["leg_id"],)
            ).fetchone()["total"]
            if int(remaining) == 0:
                connection.execute("UPDATE escort_legs SET status='completed', reason='', updated_at=? WHERE id=?", (now, row["leg_id"]))
            connection.commit()
        return self.get_assignment(assignment_id)
