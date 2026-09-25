from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.database import get_connection, transaction
from app.services.audit import AuditContext, AuditService


SCHEMA = """
CREATE TABLE IF NOT EXISTS hydro_wells (
 id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 latitude REAL NOT NULL, longitude REAL NOT NULL, aquifer TEXT NOT NULL, screen_depth_m REAL NOT NULL,
 status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hydro_endmembers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, isotope_d18o REAL NOT NULL,
 isotope_d2h REAL NOT NULL, solute_mg_l REAL NOT NULL, uncertainty REAL NOT NULL,
 version TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), created_at TEXT NOT NULL,
 UNIQUE(name,version)
);
CREATE TABLE IF NOT EXISTS hydro_samples (
 id INTEGER PRIMARY KEY AUTOINCREMENT, well_id INTEGER NOT NULL REFERENCES hydro_wells(id) ON DELETE RESTRICT,
 sample_code TEXT NOT NULL UNIQUE, sampled_at TEXT NOT NULL, isotope_d18o REAL, isotope_d2h REAL,
 solute_mg_l REAL, detection_limit REAL NOT NULL, measurement_error REAL NOT NULL,
 quality_status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hydro_inversions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, sample_id INTEGER NOT NULL REFERENCES hydro_samples(id) ON DELETE RESTRICT,
 task_key TEXT NOT NULL UNIQUE, model_version TEXT NOT NULL, method TEXT NOT NULL,
 input_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
 worker_id TEXT NOT NULL DEFAULT '', result_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
 parameter_set_id INTEGER, parameter_revision INTEGER, parameter_hash TEXT,
 impact_status TEXT NOT NULL DEFAULT 'current', superseded_by_run_id INTEGER, recalculation_of INTEGER,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hydro_transport_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, well_id INTEGER NOT NULL REFERENCES hydro_wells(id) ON DELETE RESTRICT,
 task_key TEXT NOT NULL UNIQUE, model_version TEXT NOT NULL, input_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0, worker_id TEXT NOT NULL DEFAULT '',
 result_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
 parameter_set_id INTEGER, parameter_revision INTEGER, parameter_hash TEXT,
 impact_status TEXT NOT NULL DEFAULT 'current', superseded_by_run_id INTEGER, recalculation_of INTEGER,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hydro_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, resource_type TEXT NOT NULL, resource_id INTEGER,
 action TEXT NOT NULL, actor TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hydro_parameter_sets (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 site_code TEXT NOT NULL,
 revision INTEGER,
 status TEXT NOT NULL CHECK(status IN ('draft','in_review','published','revoked')),
 porosity REAL NOT NULL, velocity_m_day REAL NOT NULL, dispersion_m2_day REAL NOT NULL,
 content_json TEXT NOT NULL, content_hash TEXT NOT NULL,
 change_note TEXT NOT NULL DEFAULT '', review_comment TEXT NOT NULL DEFAULT '',
 base_published_id INTEGER REFERENCES hydro_parameter_sets(id),
 superseded_by_id INTEGER REFERENCES hydro_parameter_sets(id),
 created_by TEXT NOT NULL, created_at TEXT NOT NULL,
 submitted_by TEXT, submitted_at TEXT,
 reviewed_by TEXT, reviewed_at TEXT,
 published_by TEXT, published_at TEXT,
 revoked_by TEXT, revoked_at TEXT, revoke_reason TEXT NOT NULL DEFAULT '',
 UNIQUE(site_code, revision)
);
CREATE TABLE IF NOT EXISTS hydro_parameter_set_endmembers (
 parameter_set_id INTEGER NOT NULL REFERENCES hydro_parameter_sets(id) ON DELETE CASCADE,
 ordinal INTEGER NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL,
 isotope_d18o REAL NOT NULL, isotope_d2h REAL NOT NULL,
 solute_mg_l REAL NOT NULL, uncertainty REAL NOT NULL,
 PRIMARY KEY(parameter_set_id, code)
);
CREATE TABLE IF NOT EXISTS hydro_result_diffs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 kind TEXT NOT NULL CHECK(kind IN ('inversion','transport')),
 previous_task_id INTEGER NOT NULL, recalculated_task_id INTEGER NOT NULL,
 site_code TEXT NOT NULL,
 previous_parameter_set_id INTEGER NOT NULL, new_parameter_set_id INTEGER NOT NULL,
 summary_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(previous_task_id, recalculated_task_id)
);
CREATE INDEX IF NOT EXISTS idx_hydro_samples_well ON hydro_samples(well_id,sampled_at);
CREATE INDEX IF NOT EXISTS idx_hydro_inversions_status ON hydro_inversions(status,created_at);
CREATE INDEX IF NOT EXISTS idx_param_sets_site ON hydro_parameter_sets(site_code,status);
CREATE INDEX IF NOT EXISTS idx_param_endmembers_set ON hydro_parameter_set_endmembers(parameter_set_id);
"""

# 引用新增列的索引必须在旧库加列迁移之后再创建
INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_inversions_paramset ON hydro_inversions(parameter_set_id);
CREATE INDEX IF NOT EXISTS idx_transport_paramset ON hydro_transport_runs(parameter_set_id);
CREATE INDEX IF NOT EXISTS idx_result_diffs_newset ON hydro_result_diffs(new_parameter_set_id);
"""

# 旧库增量加列：任务必须固化所引用的不可变参数版本
COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "hydro_inversions": {
        "parameter_set_id": "INTEGER",
        "parameter_revision": "INTEGER",
        "parameter_hash": "TEXT",
        "impact_status": "TEXT NOT NULL DEFAULT 'current'",
        "superseded_by_run_id": "INTEGER",
        "recalculation_of": "INTEGER",
    },
    "hydro_transport_runs": {
        "parameter_set_id": "INTEGER",
        "parameter_revision": "INTEGER",
        "parameter_hash": "TEXT",
        "impact_status": "TEXT NOT NULL DEFAULT 'current'",
        "superseded_by_run_id": "INTEGER",
        "recalculation_of": "INTEGER",
    },
}

DRAFT = "draft"
IN_REVIEW = "in_review"
PUBLISHED = "published"
REVOKED = "revoked"

# 允许的状态迁移：每次状态变更都经此表校验并写审计
TRANSITIONS: dict[tuple[str, str], str] = {
    (DRAFT, IN_REVIEW): "submit",
    (IN_REVIEW, DRAFT): "reject",
    (IN_REVIEW, PUBLISHED): "publish",
    (PUBLISHED, REVOKED): "revoke",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema() -> None:
    connection = get_connection()
    connection.executescript(SCHEMA)
    for table, columns in COLUMN_MIGRATIONS.items():
        existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, declaration in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    connection.executescript(INDEX_SQL)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _canonical_content(site_code: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "site_code": site_code,
        "porosity": payload["porosity"],
        "velocity_m_day": payload["velocity_m_day"],
        "dispersion_m2_day": payload["dispersion_m2_day"],
        "endmembers": [
            {
                "code": item["code"].strip(),
                "name": item["name"],
                "isotope_d18o": item["isotope_d18o"],
                "isotope_d2h": item["isotope_d2h"],
                "solute_mg_l": item["solute_mg_l"],
                "uncertainty": item["uncertainty"],
            }
            for item in sorted(payload["endmembers"], key=lambda item: item["code"].strip())
        ],
    }


class ParameterSetService:
    """参数集草稿/复核/发布/撤销状态机。发布后内容不可变，只允许撤销。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()

    # -- 内部工具 ---------------------------------------------------------

    def _audit(self, connection: sqlite3.Connection, principal: Principal, **kwargs: Any) -> None:
        AuditService(connection, self.clock).record(
            AuditContext(principal.user_id, principal.display_name), **kwargs
        )

    def _get(self, connection: sqlite3.Connection, parameter_set_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM hydro_parameter_sets WHERE id=?", (parameter_set_id,)).fetchone()
        if row is None:
            raise NotFoundError("参数集不存在")
        return row

    def _require_site(self, connection: sqlite3.Connection, site_code: str) -> None:
        if connection.execute("SELECT 1 FROM hydro_wells WHERE code=?", (site_code,)).fetchone() is None:
            raise NotFoundError("场地（井点）不存在")

    def _current_published(self, connection: sqlite3.Connection, site_code: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM hydro_parameter_sets WHERE site_code=? AND status='published' "
            "ORDER BY revision DESC LIMIT 1",
            (site_code,),
        ).fetchone()

    def _detail(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["endmembers"] = [
            dict(item)
            for item in connection.execute(
                "SELECT code,name,isotope_d18o,isotope_d2h,solute_mg_l,uncertainty,ordinal "
                "FROM hydro_parameter_set_endmembers WHERE parameter_set_id=? ORDER BY ordinal",
                (row["id"],),
            ).fetchall()
        ]
        current = self._current_published(connection, row["site_code"])
        result["is_current"] = bool(current and current["id"] == row["id"])
        return result

    # -- 草稿 -------------------------------------------------------------

    def create_draft(self, principal: Principal, site_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        principal.require("hydro.parameters.draft")
        site_code = site_code.strip().upper()
        content = _canonical_content(site_code, payload)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            self._require_site(connection, site_code)
            current = self._current_published(connection, site_code)
            # 允许同一场地并行起草多个修订；是否冲突留待发布时按 base_published_id 检测
            cursor = connection.execute(
                "INSERT INTO hydro_parameter_sets(site_code,revision,status,porosity,velocity_m_day,"
                "dispersion_m2_day,content_json,content_hash,change_note,base_published_id,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    site_code, None, DRAFT, content["porosity"], content["velocity_m_day"],
                    content["dispersion_m2_day"], json.dumps(content, ensure_ascii=False),
                    _digest(content), payload.get("change_note", ""),
                    current["id"] if current else None, principal.display_name, now,
                ),
            )
            parameter_set_id = cursor.lastrowid
            self._replace_endmembers(connection, parameter_set_id, content["endmembers"])
            row = self._get(connection, parameter_set_id)
            self._audit(
                connection, principal, action="hydro.parameter_set.draft",
                resource_type="hydro_parameter_set", resource_id=parameter_set_id,
                after={"site_code": site_code, "status": DRAFT, "content_hash": row["content_hash"]},
                metadata={"base_published_id": row["base_published_id"]},
            )
            return self._detail(connection, row)

    def revise_draft(self, principal: Principal, parameter_set_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        principal.require("hydro.parameters.draft")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            row = self._get(connection, parameter_set_id)
            if row["status"] != DRAFT:
                raise ConflictError("只有草稿状态的参数集才能修改")
            # 乐观锁：base_hash 不匹配说明已被其他人修订
            if row["content_hash"] != payload["base_hash"]:
                self._audit(
                    connection, principal, action="hydro.parameter_set.revise",
                    resource_type="hydro_parameter_set", resource_id=parameter_set_id,
                    outcome="failure", metadata={"reason": "stale_base_hash"},
                )
                raise ConflictError("草稿已被他人修改，请刷新后基于最新内容重试")
            current = json.loads(row["content_json"])
            for key in ("porosity", "velocity_m_day", "dispersion_m2_day"):
                if payload.get(key) is not None:
                    current[key] = payload[key]
            if payload.get("endmembers") is not None:
                current = _canonical_content(row["site_code"], {**current, "endmembers": payload["endmembers"]})
            else:
                current = _canonical_content(row["site_code"], current)
            change_note = payload["change_note"] if payload.get("change_note") is not None else row["change_note"]
            self._replace_endmembers(connection, parameter_set_id, current["endmembers"])
            connection.execute(
                "UPDATE hydro_parameter_sets SET porosity=?,velocity_m_day=?,dispersion_m2_day=?,"
                "content_json=?,content_hash=?,change_note=? WHERE id=?",
                (current["porosity"], current["velocity_m_day"], current["dispersion_m2_day"],
                 json.dumps(current, ensure_ascii=False), _digest(current), change_note, parameter_set_id),
            )
            self._audit(
                connection, principal, action="hydro.parameter_set.revise",
                resource_type="hydro_parameter_set", resource_id=parameter_set_id,
                before={"content_hash": row["content_hash"]},
                after={"content_hash": _digest(current)},
            )
            return self._detail(connection, self._get(connection, parameter_set_id))

    def delete_draft(self, principal: Principal, parameter_set_id: int) -> None:
        principal.require("hydro.parameters.draft")
        with transaction(immediate=True) as connection:
            row = self._get(connection, parameter_set_id)
            if row["status"] != DRAFT:
                raise ConflictError("只有草稿状态的参数集才能删除")
            connection.execute("DELETE FROM hydro_parameter_sets WHERE id=?", (parameter_set_id,))
            self._audit(
                connection, principal, action="hydro.parameter_set.delete",
                resource_type="hydro_parameter_set", resource_id=parameter_set_id,
                before={"site_code": row["site_code"], "status": DRAFT},
            )

    # -- 复核与发布 -------------------------------------------------------

    def submit_for_review(self, principal: Principal, parameter_set_id: int) -> dict[str, Any]:
        return self._transition(principal, parameter_set_id, IN_REVIEW, "hydro.parameters.draft")

    def reject(self, principal: Principal, parameter_set_id: int, reason: str) -> dict[str, Any]:
        return self._transition(
            principal, parameter_set_id, DRAFT, "hydro.parameters.review", comment=reason,
        )

    def publish(self, principal: Principal, parameter_set_id: int, *, note: str = "", force: bool = False) -> dict[str, Any]:
        principal.require("hydro.parameters.publish")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            row = self._get(connection, parameter_set_id)
            self._check_transition(row["status"], PUBLISHED)
            if _digest(json.loads(row["content_json"])) != row["content_hash"]:
                raise ConflictError("参数内容与哈希不一致，拒绝发布")
            current = self._current_published(connection, row["site_code"])
            forced_conflict = False
            if (current["id"] if current else None) != row["base_published_id"]:
                # 并发发布冲突：本稿基于的旧版本已不是当前发布版
                if not force:
                    self._audit(
                        connection, principal, action="hydro.parameter_set.publish",
                        resource_type="hydro_parameter_set", resource_id=parameter_set_id,
                        outcome="failure",
                        metadata={"reason": "base_version_conflict",
                                  "base_published_id": row["base_published_id"],
                                  "current_published_id": current["id"] if current else None},
                    )
                    raise ConflictError(
                        "基准版本已被其他发布取代（旧版本冲突），请基于最新版本重新起草；确认覆盖请使用 force",
                        context={"current_published_id": current["id"] if current else None},
                    )
                forced_conflict = True
            revision = (
                connection.execute(
                    "SELECT COALESCE(MAX(revision),0)+1 FROM hydro_parameter_sets "
                    "WHERE site_code=? AND revision IS NOT NULL",
                    (row["site_code"],),
                ).fetchone()[0]
            )
            affected_inversions, affected_transports = self._mark_affected(connection, row["site_code"], current)
            if current is not None:
                connection.execute(
                    "UPDATE hydro_parameter_sets SET superseded_by_id=? WHERE id=?",
                    (parameter_set_id, current["id"]),
                )
            connection.execute(
                "UPDATE hydro_parameter_sets SET status='published',revision=?,published_by=?,published_at=?,"
                "reviewed_by=COALESCE(reviewed_by,?),reviewed_at=COALESCE(reviewed_at,?),change_note=? "
                "WHERE id=?",
                (revision, principal.display_name, now, principal.display_name, now,
                 note or row["change_note"], parameter_set_id),
            )
            self._audit(
                connection, principal, action="hydro.parameter_set.publish",
                resource_type="hydro_parameter_set", resource_id=parameter_set_id,
                before={"status": row["status"], "revision": None},
                after={"status": PUBLISHED, "revision": revision},
                metadata={"affected_inversions": affected_inversions,
                          "affected_transports": affected_transports,
                          "supersedes_id": current["id"] if current else None,
                          "forced_conflict": forced_conflict},
            )
            result = self._detail(connection, self._get(connection, parameter_set_id))
            result["affected"] = {"inversions": affected_inversions, "transports": affected_transports}
            return result

    def revoke(self, principal: Principal, parameter_set_id: int, reason: str) -> dict[str, Any]:
        return self._transition(
            principal, parameter_set_id, REVOKED, "hydro.parameters.revoke", comment=reason, revoke=True,
        )

    def _transition(
        self,
        principal: Principal,
        parameter_set_id: int,
        target: str,
        required_permission: str,
        *,
        comment: str = "",
        revoke: bool = False,
    ) -> dict[str, Any]:
        principal.require(required_permission)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            row = self._get(connection, parameter_set_id)
            self._check_transition(row["status"], target)
            updates = ["status=?"]
            params: list[Any] = [target]
            if target == IN_REVIEW:
                updates += ["submitted_by=?", "submitted_at=?"]
                params += [principal.display_name, now]
            if target == DRAFT:  # 复核退回
                updates += ["reviewed_by=?", "reviewed_at=?", "review_comment=?"]
                params += [principal.display_name, now, comment]
            if revoke:
                updates += ["revoked_by=?", "revoked_at=?", "revoke_reason=?"]
                params += [principal.display_name, now, comment]
            params.append(parameter_set_id)
            connection.execute(
                f"UPDATE hydro_parameter_sets SET {','.join(updates)} WHERE id=?", tuple(params)
            )
            self._audit(
                connection, principal, action=f"hydro.parameter_set.{TRANSITIONS[(row['status'], target)]}",
                resource_type="hydro_parameter_set", resource_id=parameter_set_id,
                before={"status": row["status"]}, after={"status": target},
                metadata={"comment": comment} if comment else None,
            )
            return self._detail(connection, self._get(connection, parameter_set_id))

    @staticmethod
    def _check_transition(source: str, target: str) -> None:
        if (source, target) not in TRANSITIONS:
            raise ConflictError(f"参数集状态不允许从 {source} 转到 {target}")

    @staticmethod
    def _replace_endmembers(connection: sqlite3.Connection, parameter_set_id: int, endmembers: list[dict]) -> None:
        connection.execute("DELETE FROM hydro_parameter_set_endmembers WHERE parameter_set_id=?", (parameter_set_id,))
        connection.executemany(
            "INSERT INTO hydro_parameter_set_endmembers(parameter_set_id,ordinal,code,name,isotope_d18o,"
            "isotope_d2h,solute_mg_l,uncertainty) VALUES(?,?,?,?,?,?,?,?)",
            [
                (parameter_set_id, ordinal, item["code"], item["name"], item["isotope_d18o"],
                 item["isotope_d2h"], item["solute_mg_l"], item["uncertainty"])
                for ordinal, item in enumerate(endmembers)
            ],
        )

    def _mark_affected(
        self, connection: sqlite3.Connection, site_code: str, previous: sqlite3.Row | None
    ) -> tuple[int, int]:
        """发布新版本时只打标记，绝不重写旧结果。"""
        if previous is None:
            return 0, 0
        # 所有引用旧版本的任务都打标记（含排队/失败），结果本身一行不改
        inversions = connection.execute(
            "UPDATE hydro_inversions SET impact_status='affected' WHERE id IN ("
            "SELECT i.id FROM hydro_inversions i JOIN hydro_samples s ON i.sample_id=s.id "
            "JOIN hydro_wells w ON s.well_id=w.id "
            "WHERE w.code=? AND i.parameter_set_id=? "
            "AND i.impact_status IN ('current','affected'))",
            (site_code, previous["id"]),
        ).rowcount
        transports = connection.execute(
            "UPDATE hydro_transport_runs SET impact_status='affected' WHERE id IN ("
            "SELECT t.id FROM hydro_transport_runs t JOIN hydro_wells w ON t.well_id=w.id "
            "WHERE w.code=? AND t.parameter_set_id=? "
            "AND t.impact_status IN ('current','affected'))",
            (site_code, previous["id"]),
        ).rowcount
        return inversions, transports

    # -- 查询 -------------------------------------------------------------

    def list_sets(self, site_code: str | None, status: str | None) -> list[dict]:
        conditions: list[str] = []
        params: list[Any] = []
        if site_code:
            conditions.append("site_code=?")
            params.append(site_code.strip().upper())
        if status:
            conditions.append("status=?")
            params.append(status)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = self.connection.execute(
            f"SELECT * FROM hydro_parameter_sets{where} ORDER BY site_code, COALESCE(revision, 999999), id",
            tuple(params),
        ).fetchall()
        return [self._detail(self.connection, row) for row in rows]

    def get_set(self, parameter_set_id: int) -> dict[str, Any]:
        return self._detail(self.connection, self._get(self.connection, parameter_set_id))

    def list_diffs(self, parameter_set_id: int) -> list[dict]:
        self._get(self.connection, parameter_set_id)
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM hydro_result_diffs WHERE new_parameter_set_id=? ORDER BY id",
                (parameter_set_id,),
            ).fetchall()
        ]

    def list_affected(self, site_code: str | None, kind: str | None) -> dict[str, list]:
        result: dict[str, list] = {"inversions": [], "transports": []}
        if kind in (None, "inversion"):
            clauses = ["impact_status='affected'"]
            params: list[Any] = []
            if site_code:
                clauses.append("w.code=?")
                params.append(site_code.strip().upper())
            sql = ("SELECT i.*,w.code AS site_code,p.revision AS parameter_revision_display FROM hydro_inversions i "
                   "JOIN hydro_samples s ON i.sample_id=s.id JOIN hydro_wells w ON s.well_id=w.id "
                   "JOIN hydro_parameter_sets p ON i.parameter_set_id=p.id WHERE " + " AND ".join(clauses) +
                   " ORDER BY i.id")
            result["inversions"] = [dict(r) for r in self.connection.execute(sql, tuple(params)).fetchall()]
        if kind in (None, "transport"):
            clauses = ["t.impact_status='affected'"]
            params = []
            if site_code:
                clauses.append("w.code=?")
                params.append(site_code.strip().upper())
            sql = ("SELECT t.*,w.code AS site_code FROM hydro_transport_runs t "
                   "JOIN hydro_wells w ON t.well_id=w.id WHERE " + " AND ".join(clauses) + " ORDER BY t.id")
            result["transports"] = [dict(r) for r in self.connection.execute(sql, tuple(params)).fetchall()]
        return result


class HydroService:
    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None):
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        ensure_schema()

    def _audit(self, connection: sqlite3.Connection, principal: Principal, **kwargs: Any) -> None:
        AuditService(connection, self.clock).record(
            AuditContext(principal.user_id, principal.display_name), **kwargs
        )

    def create_well(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        principal.require("hydro.sites.write")
        now = _now()
        with transaction(immediate=True) as connection:
            cursor = connection.execute("INSERT INTO hydro_wells(code,name,latitude,longitude,aquifer,screen_depth_m,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (payload["code"],payload["name"],payload["latitude"],payload["longitude"],payload["aquifer"],payload["screen_depth_m"],now,now))
            well_id = cursor.lastrowid
            connection.execute("INSERT INTO hydro_audit(resource_type,resource_id,action,actor,payload_json,created_at) VALUES('well',?,?,?,?,?)", (well_id,"create",principal.display_name,json.dumps(payload,ensure_ascii=False),now))
            self._audit(connection, principal, action="hydro.well.create", resource_type="hydro_well",
                        resource_id=well_id, after={"code": payload["code"]})
            return dict(connection.execute("SELECT * FROM hydro_wells WHERE id=?",(well_id,)).fetchone())

    def get_well(self, well_id: int) -> dict[str, Any] | None:
        well = self.connection.execute("SELECT * FROM hydro_wells WHERE id=?",(well_id,)).fetchone()
        if well is None: return None
        result = dict(well)
        result["samples"] = [dict(r) for r in self.connection.execute("SELECT * FROM hydro_samples WHERE well_id=? ORDER BY sampled_at,id",(well_id,)).fetchall()]
        return result

    def delete_well(self, well_id: int, principal: Principal) -> bool:
        principal.require("hydro.sites.write")
        with transaction(immediate=True) as connection:
            existing = connection.execute("SELECT code FROM hydro_wells WHERE id=?", (well_id,)).fetchone()
            cursor = connection.execute("DELETE FROM hydro_wells WHERE id=?",(well_id,))
            if cursor.rowcount == 0: raise NotFoundError("井点不存在")
            self._audit(connection, principal, action="hydro.well.delete", resource_type="hydro_well",
                        resource_id=well_id, before={"code": existing["code"]})
            return True

    def create_endmember(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        principal.require("hydro.sites.write")
        now=_now()
        with transaction(immediate=True) as connection:
            cursor=connection.execute("INSERT INTO hydro_endmembers(name,isotope_d18o,isotope_d2h,solute_mg_l,uncertainty,version,created_at) VALUES(?,?,?,?,?,?,?)",(payload["name"],payload["isotope_d18o"],payload["isotope_d2h"],payload["solute_mg_l"],payload["uncertainty"],payload["version"],now))
            row = dict(connection.execute("SELECT * FROM hydro_endmembers WHERE id=?",(cursor.lastrowid,)).fetchone())
            self._audit(connection, principal, action="hydro.endmember.create",
                        resource_type="hydro_endmember", resource_id=row["id"],
                        after={"name": row["name"], "version": row["version"]})
            return row

    def add_sample(self, well_id: int, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        principal.require("hydro.sites.write")
        if self.connection.execute("SELECT id FROM hydro_wells WHERE id=?",(well_id,)).fetchone() is None: raise NotFoundError("井点不存在")
        values=[payload.get("isotope_d18o"),payload.get("isotope_d2h"),payload.get("solute_mg_l")]
        quality="usable" if sum(v is not None for v in values)>=2 else "incomplete"
        now=_now()
        with transaction(immediate=True) as connection:
            cursor=connection.execute("INSERT INTO hydro_samples(well_id,sample_code,sampled_at,isotope_d18o,isotope_d2h,solute_mg_l,detection_limit,measurement_error,quality_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(well_id,payload["sample_code"],payload["sampled_at"],payload.get("isotope_d18o"),payload.get("isotope_d2h"),payload.get("solute_mg_l"),payload["detection_limit"],payload["measurement_error"],quality,now))
            sample_id = cursor.lastrowid
            self._audit(connection, principal, action="hydro.sample.create",
                        resource_type="hydro_sample", resource_id=sample_id,
                        after={"well_id": well_id, "sample_code": payload["sample_code"], "quality": quality})
            return dict(connection.execute("SELECT * FROM hydro_samples WHERE id=?",(sample_id,)).fetchone())

    # -- 参数版本解析（任务只能引用已发布且不可变版本） --------------------

    def _published_set(self, connection: sqlite3.Connection, parameter_set_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM hydro_parameter_sets WHERE id=?", (parameter_set_id,)).fetchone()
        if row is None:
            raise NotFoundError("参数集不存在")
        if row["status"] != PUBLISHED:
            raise ConflictError(f"参数集当前状态为 {row['status']}，只有已发布版本才能被计算任务引用")
        if _digest(json.loads(row["content_json"])) != row["content_hash"]:
            raise ConflictError("参数集内容哈希校验失败")
        return row

    def _impact_for(self, connection: sqlite3.Connection, row: sqlite3.Row) -> str:
        current = connection.execute(
            "SELECT id FROM hydro_parameter_sets WHERE site_code=? AND status='published' "
            "ORDER BY revision DESC LIMIT 1",
            (row["site_code"],),
        ).fetchone()
        return "current" if current and current["id"] == row["id"] else "affected"

    @staticmethod
    def _snapshot_endmembers(connection: sqlite3.Connection, parameter_set_id: int) -> list[sqlite3.Row]:
        # code 作为结果中的端元标识；快照行不可变，重算也使用同一份数据
        return connection.execute(
            "SELECT code AS id, code, name, isotope_d18o, isotope_d2h, solute_mg_l, uncertainty, ordinal "
            "FROM hydro_parameter_set_endmembers WHERE parameter_set_id=? ORDER BY ordinal",
            (parameter_set_id,),
        ).fetchall()

    def _project_simplex(self, values: list[float]) -> list[float]:
        clipped=[max(0.0,v) for v in values]
        total=sum(clipped)
        return [1/len(values)]*len(values) if total<=1e-15 else [v/total for v in clipped]

    def solve_mixture(self, sample: sqlite3.Row, endmembers: list[sqlite3.Row], max_iterations: int, tolerance: float) -> dict[str, Any]:
        observed=[sample["isotope_d18o"],sample["isotope_d2h"],sample["solute_mg_l"]]
        active=[i for i,v in enumerate(observed) if v is not None]
        if len(active)<2: raise ValidationError("insufficient_measurements")
        fractions=[1/len(endmembers)]*len(endmembers)
        scale=[20.0,100.0,max(1.0,float(sample["solute_mg_l"] or 1))]
        rate=0.08
        last=float("inf")
        iteration=0
        for iteration in range(max_iterations):
            predicted=[sum(fractions[j]*[e["isotope_d18o"],e["isotope_d2h"],e["solute_mg_l"]][k] for j,e in enumerate(endmembers)) for k in range(3)]
            residual=[(predicted[k]-float(observed[k]))/scale[k] if k in active else 0.0 for k in range(3)]
            objective=sum(r*r for r in residual)+((sum(fractions)-1.0)*10)**2
            if abs(last-objective)<tolerance: break
            last=objective
            gradient=[]
            for e in endmembers:
                vector=[e["isotope_d18o"],e["isotope_d2h"],e["solute_mg_l"]]
                gradient.append(2*sum(residual[k]*vector[k]/scale[k] for k in active))
            fractions=self._project_simplex([f-rate*g for f,g in zip(fractions,gradient)])
        predicted=[sum(fractions[j]*[e["isotope_d18o"],e["isotope_d2h"],e["solute_mg_l"]][k] for j,e in enumerate(endmembers)) for k in range(3)]
        rmse=math.sqrt(sum(((predicted[k]-float(observed[k]))/scale[k])**2 for k in active)/len(active))
        return {"fractions":[{"endmember_id":e["id"],"name":e["name"],"fraction":round(f,8)} for e,f in zip(endmembers,fractions)],"mass_balance":round(sum(fractions),10),"predicted":predicted,"rmse":rmse,"iterations":iteration+1,"converged":abs(last-objective)<tolerance}

    # -- 反演任务 ---------------------------------------------------------

    def enqueue_inversion(self, sample_id: int, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        principal.require("hydro.tasks.run")
        now=_now()
        with transaction(immediate=True) as connection:
            sample=connection.execute("SELECT * FROM hydro_samples WHERE id=?",(sample_id,)).fetchone()
            if sample is None: raise NotFoundError("样本不存在")
            parameter_set=self._published_set(connection, payload["parameter_set_id"])
            endmembers=self._snapshot_endmembers(connection, parameter_set["id"])
            if len(endmembers) < 2: raise ValidationError("参数集端元数量不足")
            input_data={
                "sample":dict(sample),
                "parameter_set_id":parameter_set["id"],
                "parameter_revision":parameter_set["revision"],
                "parameter_hash":parameter_set["content_hash"],
                "endmembers":[dict(e) for e in endmembers],
                "method":payload["method"],
                "max_iterations":payload["max_iterations"],
                "tolerance":payload["tolerance"],
            }
            key=_digest(input_data)
            old=connection.execute("SELECT * FROM hydro_inversions WHERE task_key=?",(key,)).fetchone()
            if old: return dict(old)
            impact=self._impact_for(connection, parameter_set)
            cursor=connection.execute(
                "INSERT INTO hydro_inversions(sample_id,task_key,model_version,method,input_json,"
                "parameter_set_id,parameter_revision,parameter_hash,impact_status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (sample_id,key,payload["model_version"],payload["method"],
                 json.dumps(input_data,ensure_ascii=False),parameter_set["id"],parameter_set["revision"],
                 parameter_set["content_hash"],impact,now,now))
            task_id=cursor.lastrowid
            self._audit(connection, principal, action="hydro.inversion.enqueue",
                        resource_type="hydro_inversion", resource_id=task_id,
                        after={"sample_id": sample_id, "parameter_set_id": parameter_set["id"],
                               "parameter_revision": parameter_set["revision"]},
                        metadata={"impact_status": impact})
            return dict(connection.execute("SELECT * FROM hydro_inversions WHERE id=?",(task_id,)).fetchone())

    def _execute_inversion(self, connection: sqlite3.Connection, task: sqlite3.Row) -> dict[str, Any]:
        data=json.loads(task["input_json"])
        now=_now()
        if task["parameter_set_id"]:
            # 不可变快照：即使端元目录被改动，任务仍按发布时的端元组成计算
            endmembers=self._snapshot_endmembers(connection, task["parameter_set_id"])
        else:
            ids=data["endmember_ids"]
            endmembers=connection.execute(
                f"SELECT id,name,isotope_d18o,isotope_d2h,solute_mg_l FROM hydro_endmembers "
                f"WHERE id IN ({','.join('?' for _ in ids)}) ORDER BY id", ids).fetchall()
        sample=connection.execute("SELECT * FROM hydro_samples WHERE id=?",(task["sample_id"],)).fetchone()
        result=self.solve_mixture(sample,endmembers,data["max_iterations"],data["tolerance"])
        connection.execute(
            "UPDATE hydro_inversions SET status='done',result_json=?,error='',updated_at=? WHERE id=?",
            (json.dumps(result,ensure_ascii=False),now,task["id"]))
        return dict(connection.execute("SELECT * FROM hydro_inversions WHERE id=?",(task["id"],)).fetchone())

    def run_inversion(self, task_id: int, worker_id: str, principal: Principal) -> dict[str, Any]:
        principal.require("hydro.tasks.run")
        with transaction(immediate=True) as connection:
            task=connection.execute("SELECT * FROM hydro_inversions WHERE id=?",(task_id,)).fetchone()
            if task is None: raise NotFoundError("任务不存在")
            if task["status"]=="done": return dict(task)
            connection.execute("UPDATE hydro_inversions SET status='running',attempts=attempts+1,worker_id=?,updated_at=? WHERE id=?",(worker_id,_now(),task_id))
        try:
            with transaction(immediate=True) as connection:
                result=self._execute_inversion(connection, task)
                self._audit(connection, principal, action="hydro.inversion.run",
                            resource_type="hydro_inversion", resource_id=task_id,
                            after={"status": "done", "parameter_revision": result.get("parameter_revision")},
                            metadata={"worker_id": worker_id})
                return result
        except Exception as exc:
            with transaction(immediate=True) as connection:
                connection.execute("UPDATE hydro_inversions SET status='failed',error=?,updated_at=? WHERE id=?",(str(exc)[:300],_now(),task_id))
            raise

    def get_inversion(self, task_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM hydro_inversions WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    # -- 迁移任务 ---------------------------------------------------------

    @staticmethod
    def _compute_transport(payload: dict[str, Any]) -> dict[str, Any]:
        points=[]; t=payload["step_days"]
        while t<=payload["duration_days"]+1e-12:
            d=payload["dispersion_m2_day"]; x=payload["distance_m"]; v=payload["velocity_m_day"]
            c=payload["source_concentration"]*math.exp(-((x-v*t)**2)/(4*d*t))*math.exp(-payload["decay_per_day"]*t)/math.sqrt(4*math.pi*d*t)
            points.append({"time_days":round(t,8),"concentration":c}); t+=payload["step_days"]
        peak=max(points,key=lambda p:p["concentration"])
        return {"points":points,"peak":peak,
                "arrival_time_days":payload["distance_m"]/payload["velocity_m_day"],
                "model_version":payload["model_version"]}

    def run_transport(self, well_id: int, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        principal.require("hydro.tasks.run")
        now=_now()
        with transaction(immediate=True) as connection:
            well=connection.execute("SELECT * FROM hydro_wells WHERE id=?",(well_id,)).fetchone()
            if well is None: raise NotFoundError("井点不存在")
            parameter_set=self._published_set(connection, payload["parameter_set_id"])
            effective={
                "source_concentration":payload["source_concentration"],
                "distance_m":payload["distance_m"],
                "decay_per_day":payload["decay_per_day"],
                "duration_days":payload["duration_days"],
                "step_days":payload["step_days"],
                "model_version":payload["model_version"],
                # 流速与弥散度来自不可变参数集，而非请求体
                "velocity_m_day":parameter_set["velocity_m_day"],
                "dispersion_m2_day":parameter_set["dispersion_m2_day"],
                "porosity":parameter_set["porosity"],
                "parameter_set_id":parameter_set["id"],
                "parameter_revision":parameter_set["revision"],
                "parameter_hash":parameter_set["content_hash"],
            }
            key=_digest({"well_id":well_id,**effective})
            old=connection.execute("SELECT * FROM hydro_transport_runs WHERE task_key=?",(key,)).fetchone()
            if old: return dict(old)
            result=self._compute_transport(effective)
            impact=self._impact_for(connection, parameter_set)
            cursor=connection.execute(
                "INSERT INTO hydro_transport_runs(well_id,task_key,model_version,input_json,status,"
                "result_json,parameter_set_id,parameter_revision,parameter_hash,impact_status,"
                "attempts,worker_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (well_id,key,payload["model_version"],json.dumps(effective,ensure_ascii=False),"done",
                 json.dumps(result,ensure_ascii=False),parameter_set["id"],parameter_set["revision"],
                 parameter_set["content_hash"],impact,1,principal.display_name,now,now))
            task_id=cursor.lastrowid
            self._audit(connection, principal, action="hydro.transport.run",
                        resource_type="hydro_transport", resource_id=task_id,
                        after={"well_id": well_id, "parameter_set_id": parameter_set["id"],
                               "parameter_revision": parameter_set["revision"]},
                        metadata={"impact_status": impact})
            return dict(connection.execute("SELECT * FROM hydro_transport_runs WHERE id=?",(task_id,)).fetchone())

    def get_transport(self, task_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM hydro_transport_runs WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    # -- 批量重算与前后差异（不重写历史，生成新任务行） --------------------

    def recalculate_affected(
        self, principal: Principal, parameter_set_id: int, kinds: list[str] | None = None
    ) -> dict[str, Any]:
        principal.require("hydro.tasks.run")
        kinds = kinds or ["inversion", "transport"]
        now = _now()
        recalculated: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        with transaction(immediate=True) as connection:
            parameter_set = self._published_set(connection, parameter_set_id)
            site_code = parameter_set["site_code"]
            base_published_id = parameter_set["base_published_id"]

            old_inversions = []
            if "inversion" in kinds and base_published_id is not None:
                # 只处理直接基于上一发布版的任务，保证修订链逐版推进、可重复调用
                old_inversions = connection.execute(
                    "SELECT i.* FROM hydro_inversions i JOIN hydro_samples s ON i.sample_id=s.id "
                    "JOIN hydro_wells w ON s.well_id=w.id "
                    "WHERE w.code=? AND i.status='done' AND i.parameter_set_id=? "
                    "AND i.impact_status IN ('affected','recalculated') ORDER BY i.id",
                    (site_code, base_published_id),
                ).fetchall()
            for old in old_inversions:
                if connection.execute(
                    "SELECT 1 FROM hydro_result_diffs WHERE previous_task_id=? AND new_parameter_set_id=?",
                    (old["id"], parameter_set_id),
                ).fetchone():
                    skipped.append({"kind": "inversion", "task_id": old["id"], "reason": "already_recalculated"})
                    continue
                new_task = self._clone_inversion(connection, old, parameter_set, principal, now)
                summary = self._diff_inversion(json.loads(old["result_json"]), json.loads(new_task["result_json"]))
                diff_id = self._save_diff(connection, "inversion", old["id"], new_task["id"], site_code,
                                         old["parameter_set_id"], parameter_set_id, summary, principal, now)
                connection.execute(
                    "UPDATE hydro_inversions SET impact_status='recalculated',superseded_by_run_id=? WHERE id=?",
                    (new_task["id"], old["id"]))
                recalculated.append({"kind": "inversion", "previous_task_id": old["id"],
                                     "recalculated_task_id": new_task["id"], "diff_id": diff_id, "summary": summary})

            old_transports = []
            if "transport" in kinds and base_published_id is not None:
                old_transports = connection.execute(
                    "SELECT t.* FROM hydro_transport_runs t JOIN hydro_wells w ON t.well_id=w.id "
                    "WHERE w.code=? AND t.status='done' AND t.parameter_set_id=? "
                    "AND t.impact_status IN ('affected','recalculated') ORDER BY t.id",
                    (site_code, base_published_id),
                ).fetchall()
            for old in old_transports:
                if connection.execute(
                    "SELECT 1 FROM hydro_result_diffs WHERE previous_task_id=? AND new_parameter_set_id=?",
                    (old["id"], parameter_set_id),
                ).fetchone():
                    skipped.append({"kind": "transport", "task_id": old["id"], "reason": "already_recalculated"})
                    continue
                new_task = self._clone_transport(connection, old, parameter_set, principal, now)
                summary = self._diff_transport(json.loads(old["result_json"]), json.loads(new_task["result_json"]))
                diff_id = self._save_diff(connection, "transport", old["id"], new_task["id"], site_code,
                                         old["parameter_set_id"], parameter_set_id, summary, principal, now)
                connection.execute(
                    "UPDATE hydro_transport_runs SET impact_status='recalculated',superseded_by_run_id=? WHERE id=?",
                    (new_task["id"], old["id"]))
                recalculated.append({"kind": "transport", "previous_task_id": old["id"],
                                     "recalculated_task_id": new_task["id"], "diff_id": diff_id, "summary": summary})

            self._audit(
                connection, principal, action="hydro.results.recalculate",
                resource_type="hydro_parameter_set", resource_id=parameter_set_id,
                after={"recalculated_count": len(recalculated), "skipped_count": len(skipped)},
                metadata={"kinds": kinds, "site_code": site_code,
                          "new_revision": parameter_set["revision"]},
            )
            return {"parameter_set_id": parameter_set_id, "parameter_revision": parameter_set["revision"],
                    "site_code": site_code, "recalculated": recalculated, "skipped": skipped}

    def _clone_inversion(
        self, connection: sqlite3.Connection, old: sqlite3.Row, parameter_set: sqlite3.Row,
        principal: Principal, now: str,
    ) -> dict[str, Any]:
        old_data = json.loads(old["input_json"])
        sample = connection.execute("SELECT * FROM hydro_samples WHERE id=?", (old["sample_id"],)).fetchone()
        endmembers = self._snapshot_endmembers(connection, parameter_set["id"])
        input_data = {
            "sample": dict(sample),
            "parameter_set_id": parameter_set["id"],
            "parameter_revision": parameter_set["revision"],
            "parameter_hash": parameter_set["content_hash"],
            "endmembers": [dict(e) for e in endmembers],
            "method": old["method"],
            "max_iterations": old_data.get("max_iterations", 500),
            "tolerance": old_data.get("tolerance", 1e-8),
            "recalculation_of": old["id"],
        }
        key = _digest(input_data)
        existing = connection.execute("SELECT * FROM hydro_inversions WHERE task_key=?", (key,)).fetchone()
        if existing:
            return dict(existing)
        cursor = connection.execute(
            "INSERT INTO hydro_inversions(sample_id,task_key,model_version,method,input_json,"
            "parameter_set_id,parameter_revision,parameter_hash,impact_status,recalculation_of,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (old["sample_id"], key, old["model_version"], old["method"],
             json.dumps(input_data, ensure_ascii=False), parameter_set["id"], parameter_set["revision"],
             parameter_set["content_hash"], "current", old["id"], now, now))
        task_row = connection.execute("SELECT * FROM hydro_inversions WHERE id=?", (cursor.lastrowid,)).fetchone()
        connection.execute("UPDATE hydro_inversions SET status='running',attempts=1,worker_id=? WHERE id=?",
                           (principal.display_name, cursor.lastrowid))
        result_row = self._execute_inversion(connection, task_row)
        self._audit(connection, principal, action="hydro.inversion.recalculate",
                    resource_type="hydro_inversion", resource_id=result_row["id"],
                    after={"parameter_set_id": parameter_set["id"], "recalculation_of": old["id"]})
        return result_row

    def _clone_transport(
        self, connection: sqlite3.Connection, old: sqlite3.Row, parameter_set: sqlite3.Row,
        principal: Principal, now: str,
    ) -> dict[str, Any]:
        old_payload = json.loads(old["input_json"])
        effective = {
            "source_concentration": old_payload["source_concentration"],
            "distance_m": old_payload["distance_m"],
            "decay_per_day": old_payload["decay_per_day"],
            "duration_days": old_payload["duration_days"],
            "step_days": old_payload["step_days"],
            "model_version": old["model_version"],
            "velocity_m_day": parameter_set["velocity_m_day"],
            "dispersion_m2_day": parameter_set["dispersion_m2_day"],
            "porosity": parameter_set["porosity"],
            "parameter_set_id": parameter_set["id"],
            "parameter_revision": parameter_set["revision"],
            "parameter_hash": parameter_set["content_hash"],
            "recalculation_of": old["id"],
        }
        key = _digest({"well_id": old["well_id"], **effective})
        existing = connection.execute("SELECT * FROM hydro_transport_runs WHERE task_key=?", (key,)).fetchone()
        if existing:
            return dict(existing)
        result = self._compute_transport(effective)
        cursor = connection.execute(
            "INSERT INTO hydro_transport_runs(well_id,task_key,model_version,input_json,status,result_json,"
            "parameter_set_id,parameter_revision,parameter_hash,impact_status,recalculation_of,"
            "attempts,worker_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (old["well_id"], key, old["model_version"], json.dumps(effective, ensure_ascii=False), "done",
             json.dumps(result, ensure_ascii=False), parameter_set["id"], parameter_set["revision"],
             parameter_set["content_hash"], "current", old["id"], 1, principal.display_name, now, now))
        row = dict(connection.execute("SELECT * FROM hydro_transport_runs WHERE id=?", (cursor.lastrowid,)).fetchone())
        self._audit(connection, principal, action="hydro.transport.recalculate",
                    resource_type="hydro_transport", resource_id=row["id"],
                    after={"parameter_set_id": parameter_set["id"], "recalculation_of": old["id"]})
        return row

    def _save_diff(
        self, connection: sqlite3.Connection, kind: str, previous_id: int, new_id: int, site_code: str,
        previous_set_id: int, new_set_id: int, summary: dict[str, Any], principal: Principal, now: str,
    ) -> int:
        cursor = connection.execute(
            "INSERT INTO hydro_result_diffs(kind,previous_task_id,recalculated_task_id,site_code,"
            "previous_parameter_set_id,new_parameter_set_id,summary_json,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (kind, previous_id, new_id, site_code, previous_set_id, new_set_id,
             json.dumps(summary, ensure_ascii=False), principal.display_name, now))
        return cursor.lastrowid

    @staticmethod
    def _diff_inversion(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        old_fractions = {item["endmember_id"]: item["fraction"] for item in old["fractions"]}
        new_fractions = {item["endmember_id"]: item["fraction"] for item in new["fractions"]}
        deltas = [
            {"endmember_id": code, "old": old_fractions.get(code), "new": new_fractions.get(code),
             "delta": None if code not in old_fractions or code not in new_fractions
             else round(new_fractions[code] - old_fractions[code], 8)}
            for code in sorted(set(old_fractions) | set(new_fractions))
        ]
        numeric = [item["delta"] for item in deltas if item["delta"] is not None]
        return {
            "fraction_deltas": deltas,
            "max_abs_fraction_delta": round(max((abs(v) for v in numeric), default=0.0), 8),
            "rmse_old": round(old.get("rmse", 0.0), 8),
            "rmse_new": round(new.get("rmse", 0.0), 8),
            "rmse_delta": round(new.get("rmse", 0.0) - old.get("rmse", 0.0), 8),
            "mass_balance_old": old.get("mass_balance"),
            "mass_balance_new": new.get("mass_balance"),
        }

    @staticmethod
    def _diff_transport(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        old_points = old.get("points", [])
        new_points = new.get("points", [])
        paired = min(len(old_points), len(new_points))
        concentration_deltas = [
            round(new_points[i]["concentration"] - old_points[i]["concentration"], 8) for i in range(paired)
        ]
        return {
            "peak_concentration_old": old.get("peak", {}).get("concentration"),
            "peak_concentration_new": new.get("peak", {}).get("concentration"),
            "peak_concentration_delta": round(
                new.get("peak", {}).get("concentration", 0.0) - old.get("peak", {}).get("concentration", 0.0), 8),
            "arrival_time_days_old": old.get("arrival_time_days"),
            "arrival_time_days_new": new.get("arrival_time_days"),
            "arrival_time_days_delta": round(
                new.get("arrival_time_days", 0.0) - old.get("arrival_time_days", 0.0), 8),
            "max_abs_concentration_delta": round(max((abs(v) for v in concentration_deltas), default=0.0), 8),
            "points_compared": paired,
        }
