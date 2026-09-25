from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.hydro.service import _digest, build_transport_input, insert_transport_run, param_set_snapshot
from app.services.audit import AuditContext, AuditService

# 草稿可重新提交复核的复核状态
RESUBMITTABLE_REVIEW_STATES = {"none", "rejected"}
ENDMEMBER_FIELDS = ("name", "isotope_d18o", "isotope_d2h", "solute_mg_l", "uncertainty")
SCALAR_FIELDS = ("porosity", "velocity_m_day", "dispersion_m2_day", "decay_per_day")
TASK_TABLES = {
    "inversion": "hydro_inversions",
    "transport": "hydro_transport_runs",
}


def _content_digest(fields: dict[str, Any], endmembers: list[dict[str, Any]]) -> str:
    """内容摘要只覆盖科学参数本身，是发布后不可变性的锚点。"""
    payload = {
        "porosity": fields["porosity"],
        "velocity_m_day": fields["velocity_m_day"],
        "dispersion_m2_day": fields["dispersion_m2_day"],
        "decay_per_day": fields["decay_per_day"],
        "endmembers": [
            {key: (member[key].strip() if key == "name" else round(float(member[key]), 10)) for key in ENDMEMBER_FIELDS}
            for member in sorted(endmembers, key=lambda item: item["name"].strip())
        ],
    }
    return _digest(payload)


class ParameterSetService:
    """参数集草稿、复核、发布、撤销与受影响结果重算。

    状态机：draft →(submit)→ in_review/pending →(approve)→ in_review/approved
    →(publish)→ published →(retract)→ retracted；复核退回回到 draft/rejected。
    所有写方法都假定运行在调用方开启的 IMMEDIATE 事务中，审计事件与业务变更同事务提交。
    """

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        # 注意：本类在调用方的事务内构造，绝不能在此执行 ensure_schema()
        # （executescript 会隐式提交挂起的事务）；表结构由应用启动时的 lifespan 保证。
        self.connection = connection
        self.clock = clock or SystemClock()
        self.audit = AuditService(connection, self.clock)

    # ------------------------------------------------------------------ 基础读取

    def _require_row(self, param_set_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM hydro_param_sets WHERE id=?", (param_set_id,)).fetchone()
        if row is None:
            raise NotFoundError("参数集不存在")
        return row

    def _endmembers(self, param_set_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM hydro_param_set_endmembers WHERE param_set_id=? ORDER BY position,id", (param_set_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def _latest_published(self, site_code: str, code: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM hydro_param_sets WHERE site_code=? AND code=? AND status='published' ORDER BY version DESC LIMIT 1",
            (site_code, code),
        ).fetchone()

    def _detail(self, param_set_id: int) -> dict[str, Any]:
        row = self._require_row(param_set_id)
        result = dict(row)
        result["endmembers"] = self._endmembers(param_set_id)
        result["review_history"] = [
            dict(item)
            for item in self.connection.execute(
                "SELECT * FROM hydro_param_set_reviews WHERE param_set_id=? ORDER BY id", (param_set_id,)
            ).fetchall()
        ]
        return result

    def detail(self, principal: Principal, param_set_id: int) -> dict[str, Any]:
        principal.require("hydro.read")
        return self._detail(param_set_id)

    def list_sets(
        self, principal: Principal, *, site_code: str | None, status: str | None, limit: int, offset: int
    ) -> dict[str, Any]:
        principal.require("hydro.read")
        conditions: list[str] = []
        params: list[Any] = []
        if site_code:
            conditions.append("site_code=?")
            params.append(site_code.strip().upper())
        if status:
            if status not in ("draft", "in_review", "published", "retracted"):
                raise ValidationError("未知的参数集状态")
            conditions.append("status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        total = int(self.connection.execute(f"SELECT COUNT(*) FROM hydro_param_sets{where}", params).fetchone()[0])
        rows = self.connection.execute(
            f"SELECT * FROM hydro_param_sets{where} ORDER BY site_code,code,version DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return {"total": total, "data": [dict(row) for row in rows]}

    def list_versions(self, principal: Principal, site_code: str, code: str) -> list[dict[str, Any]]:
        principal.require("hydro.read")
        rows = self.connection.execute(
            "SELECT * FROM hydro_param_sets WHERE site_code=? AND code=? ORDER BY version",
            (site_code.strip().upper(), code.strip().upper()),
        ).fetchall()
        if not rows:
            raise NotFoundError("该参数集分组没有任何版本")
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ 审计辅助

    def _trace(
        self,
        principal: Principal,
        action: str,
        row: sqlite3.Row,
        previous_status: str,
        *,
        comment: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        now = to_storage(self.clock.now())
        self.connection.execute(
            "INSERT INTO hydro_param_set_reviews(param_set_id,action,actor,comment,from_status,to_status,created_at) VALUES(?,?,?,?,?,?,?)",
            (row["id"], action, principal.username, comment, previous_status, row["status"], now),
        )
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action=f"hydro.params.{action}",
            resource_type="hydro_param_set",
            resource_id=row["id"],
            before={"status": previous_status},
            after={
                "site_code": row["site_code"],
                "code": row["code"],
                "version": row["version"],
                "status": row["status"],
                "review_state": row["review_state"],
                "content_digest": row["content_digest"],
            },
            metadata={"comment": comment, **(extra or {})},
        )

    def _replace_endmembers(self, param_set_id: int, endmembers: list[dict[str, Any]]) -> None:
        self.connection.execute("DELETE FROM hydro_param_set_endmembers WHERE param_set_id=?", (param_set_id,))
        for position, member in enumerate(endmembers):
            self.connection.execute(
                "INSERT INTO hydro_param_set_endmembers(param_set_id,name,isotope_d18o,isotope_d2h,solute_mg_l,uncertainty,source_endmember_id,position) VALUES(?,?,?,?,?,?,?,?)",
                (
                    param_set_id,
                    member["name"].strip(),
                    member["isotope_d18o"],
                    member["isotope_d2h"],
                    member["solute_mg_l"],
                    member["uncertainty"],
                    member.get("source_endmember_id"),
                    position,
                ),
            )

    # ------------------------------------------------------------------ 草稿

    def create(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("hydro.params.write")
        now = to_storage(self.clock.now())
        latest = self._latest_published(data["site_code"], data["code"])
        version_row = self.connection.execute(
            "SELECT MAX(version) AS version FROM hydro_param_sets WHERE site_code=? AND code=?",
            (data["site_code"], data["code"]),
        ).fetchone()
        version = int(version_row["version"] or 0) + 1
        digest = _content_digest(data, data["endmembers"])
        cursor = self.connection.execute(
            "INSERT INTO hydro_param_sets(site_code,code,version,status,review_state,porosity,velocity_m_day,"
            "dispersion_m2_day,decay_per_day,notes,content_digest,base_version_id,created_by,created_at,updated_at) "
            "VALUES(?,?,?,'draft','none',?,?,?,?,?,?,?,?,?,?)",
            (
                data["site_code"], data["code"], version,
                data["porosity"], data["velocity_m_day"], data["dispersion_m2_day"], data.get("decay_per_day", 0.0),
                data.get("notes", ""), digest, latest["id"] if latest else None, principal.username, now, now,
            ),
        )
        param_set_id = int(cursor.lastrowid)
        self._replace_endmembers(param_set_id, data["endmembers"])
        row = self._require_row(param_set_id)
        self._trace(principal, "create", row, "draft")
        return self._detail(param_set_id)

    def update(self, principal: Principal, param_set_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        principal.require("hydro.params.write")
        row = self._require_row(param_set_id)
        if row["status"] != "draft":
            raise ConflictError("只有草稿状态的参数集才能修改；已提交复核及之后的版本内容不可变")
        endmembers = changes.pop("endmembers", None)
        fields = {key: value for key, value in changes.items() if value is not None and key in (*SCALAR_FIELDS, "notes")}
        if endmembers is None and not fields:
            raise ValidationError("没有可更新的字段")
        merged = {key: fields.get(key, row[key]) for key in SCALAR_FIELDS}
        new_endmembers = endmembers if endmembers is not None else self._endmembers(param_set_id)
        digest = _content_digest(merged, new_endmembers)
        now = to_storage(self.clock.now())
        assignments = [f"{key}=?" for key in fields] + ["content_digest=?", "updated_at=?"]
        params: list[Any] = [*fields.values(), digest, now, param_set_id]
        self.connection.execute(f"UPDATE hydro_param_sets SET {','.join(assignments)} WHERE id=?", params)
        if endmembers is not None:
            self._replace_endmembers(param_set_id, endmembers)
        updated = self._require_row(param_set_id)
        self._trace(principal, "update", updated, "draft", extra={"changed_fields": sorted(fields), "endmembers_replaced": endmembers is not None})
        return self._detail(param_set_id)

    # ------------------------------------------------------------------ 复核流转

    def submit(self, principal: Principal, param_set_id: int, comment: str) -> dict[str, Any]:
        principal.require("hydro.params.write")
        row = self._require_row(param_set_id)
        if row["status"] != "draft" or row["review_state"] not in RESUBMITTABLE_REVIEW_STATES:
            raise ConflictError("只有未提交或被退回的草稿才能提交复核")
        now = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE hydro_param_sets SET status='in_review',review_state='pending',submitted_by=?,submitted_at=?,updated_at=? WHERE id=?",
            (principal.username, now, now, param_set_id),
        )
        updated = self._require_row(param_set_id)
        self._trace(principal, "submit", updated, "draft", comment=comment)
        return self._detail(param_set_id)

    def _review(self, principal: Principal, param_set_id: int, approved: bool, comment: str) -> dict[str, Any]:
        principal.require("hydro.params.review")
        row = self._require_row(param_set_id)
        if row["status"] != "in_review" or row["review_state"] != "pending":
            raise ConflictError("该参数集当前不在待复核状态")
        if row["submitted_by"] and row["submitted_by"] == principal.username:
            raise ValidationError("提交人与复核人不能是同一人")
        now = to_storage(self.clock.now())
        if approved:
            self.connection.execute(
                "UPDATE hydro_param_sets SET review_state='approved',reviewed_by=?,reviewed_at=?,review_comment=?,updated_at=? WHERE id=?",
                (principal.username, now, comment, now, param_set_id),
            )
        else:
            self.connection.execute(
                "UPDATE hydro_param_sets SET status='draft',review_state='rejected',reviewed_by=?,reviewed_at=?,review_comment=?,updated_at=? WHERE id=?",
                (principal.username, now, comment, now, param_set_id),
            )
        updated = self._require_row(param_set_id)
        self._trace(principal, "approve" if approved else "reject", updated, "in_review", comment=comment)
        return self._detail(param_set_id)

    def approve(self, principal: Principal, param_set_id: int, comment: str) -> dict[str, Any]:
        return self._review(principal, param_set_id, True, comment)

    def reject(self, principal: Principal, param_set_id: int, comment: str) -> dict[str, Any]:
        if not comment.strip():
            raise ValidationError("退回复核必须填写意见")
        return self._review(principal, param_set_id, False, comment)

    def rebase(self, principal: Principal, param_set_id: int) -> dict[str, Any]:
        principal.require("hydro.params.write")
        row = self._require_row(param_set_id)
        if row["status"] not in ("draft", "in_review"):
            raise ConflictError("只有未发布的参数集才能 rebase 到最新发布版本")
        latest = self._latest_published(row["site_code"], row["code"])
        if latest is None:
            raise ConflictError("该参数集还没有已发布版本，无需 rebase")
        if latest["id"] == row["base_version_id"] and row["status"] == "draft" and row["review_state"] in RESUBMITTABLE_REVIEW_STATES:
            raise ConflictError("草稿已经基于最新发布版本，无需 rebase")
        previous_status = row["status"]
        now = to_storage(self.clock.now())
        # 基准版本变化后复核结论一律作废，重新走草稿流程
        self.connection.execute(
            "UPDATE hydro_param_sets SET status='draft',review_state='none',base_version_id=?,updated_at=? WHERE id=?",
            (latest["id"], now, param_set_id),
        )
        updated = self._require_row(param_set_id)
        self._trace(
            principal, "rebase", updated, previous_status,
            extra={"new_base_version_id": latest["id"], "new_base_version": latest["version"]},
        )
        return self._detail(param_set_id)

    # ------------------------------------------------------------------ 发布与撤销

    def publish(self, principal: Principal, param_set_id: int, expected_base_version_id: int | None) -> dict[str, Any]:
        principal.require("hydro.params.publish")
        row = self._require_row(param_set_id)
        if row["status"] != "in_review" or row["review_state"] != "approved":
            raise ConflictError("只有复核通过的参数集才能发布")
        latest = self._latest_published(row["site_code"], row["code"])
        latest_id = latest["id"] if latest else None
        if expected_base_version_id != latest_id:
            raise ConflictError(
                "参数集基准版本已过期：该分组已被并发发布了更新的版本，请 rebase 后重新提交复核"
            )
        digest = _content_digest(dict(row), self._endmembers(param_set_id))
        if digest != row["content_digest"]:
            raise ConflictError("参数集内容摘要与存储内容不一致，拒绝发布")
        now = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE hydro_param_sets SET status='published',published_by=?,published_at=?,"
            "supersedes_version_id=?,updated_at=? WHERE id=?",
            (principal.username, now, latest_id, now, param_set_id),
        )
        published = self._require_row(param_set_id)
        affected = self._mark_affected_by_publish(published, now)
        self._trace(principal, "publish", published, "in_review", extra={"supersedes_version_id": latest_id, "affected": affected})
        result = self._detail(param_set_id)
        result["affected_results"] = affected
        return result

    def _mark_affected_by_publish(self, published: sqlite3.Row, now: str) -> dict[str, int]:
        """只给引用旧版本的结果打 stale 标记，绝不改写 result_json 历史内容。"""
        reason = f"superseded_by_param_set:{published['id']}"
        affected: dict[str, int] = {}
        for task_type, table in TASK_TABLES.items():
            cursor = self.connection.execute(
                f"UPDATE {table} SET stale=1,stale_reason=?,stale_since=COALESCE(stale_since,?) "
                f"WHERE status='done' AND stale=0 AND parameter_set_id IN ("
                f"SELECT id FROM hydro_param_sets WHERE site_code=? AND code=? AND version<?)",
                (reason, now, published["site_code"], published["code"], published["version"]),
            )
            affected[task_type] = cursor.rowcount
        return affected

    def retract(self, principal: Principal, param_set_id: int, reason: str) -> dict[str, Any]:
        principal.require("hydro.params.publish")
        row = self._require_row(param_set_id)
        if row["status"] != "published":
            raise ConflictError("只有已发布的参数集版本才能撤销")
        now = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE hydro_param_sets SET status='retracted',retracted_by=?,retracted_at=?,retract_reason=?,updated_at=? WHERE id=?",
            (principal.username, now, reason, now, param_set_id),
        )
        affected: dict[str, int] = {}
        for task_type, table in TASK_TABLES.items():
            cursor = self.connection.execute(
                f"UPDATE {table} SET stale=1,stale_reason='parameter_set_retracted',stale_since=COALESCE(stale_since,?) "
                "WHERE parameter_set_id=? AND status='done' AND stale=0",
                (now, param_set_id),
            )
            affected[task_type] = cursor.rowcount
        retracted = self._require_row(param_set_id)
        self._trace(principal, "retract", retracted, "published", comment=reason, extra={"affected": affected})
        result = self._detail(param_set_id)
        result["affected_results"] = affected
        return result

    # ------------------------------------------------------------------ 影响面与重算

    def _group_older_version_ids(self, target: sqlite3.Row) -> list[int]:
        rows = self.connection.execute(
            "SELECT id FROM hydro_param_sets WHERE site_code=? AND code=? AND version<?",
            (target["site_code"], target["code"], target["version"]),
        ).fetchall()
        return [item["id"] for item in rows]

    def affected_results(self, principal: Principal, param_set_id: int, limit: int = 100) -> dict[str, Any]:
        principal.require("hydro.read")
        target = self._require_row(param_set_id)
        candidate_ids = self._group_older_version_ids(target) + [param_set_id]
        placeholders = ",".join("?" for _ in candidate_ids)
        columns = "id,status,stale,stale_reason,stale_since,parameter_set_id,parameter_set_version,superseded_by_task_id,recompute_of_task_id"
        result: dict[str, Any] = {"parameter_set_id": param_set_id, "version": target["version"], "status": target["status"]}
        for task_type, table in TASK_TABLES.items():
            rows = self.connection.execute(
                f"SELECT {columns} FROM {table} WHERE parameter_set_id IN ({placeholders}) AND stale=1 ORDER BY id DESC LIMIT ?",
                (*candidate_ids, limit),
            ).fetchall()
            result[f"{task_type}s"] = [dict(row) for row in rows]
        return result

    def recompute(self, principal: Principal, param_set_id: int, task_types: list[str], limit: int) -> dict[str, Any]:
        principal.require("hydro.recompute")
        target = self._require_row(param_set_id)
        if target["status"] != "published":
            raise ValidationError("只能按已发布的参数集版本进行重算")
        latest = self._latest_published(target["site_code"], target["code"])
        if latest is None or latest["id"] != param_set_id:
            raise ConflictError("请使用该参数集分组的最新发布版本进行批量重算")
        older_ids = self._group_older_version_ids(target)
        created: dict[str, list[int]] = {"inversions": [], "transports": []}
        skipped: dict[str, int] = {"inversions": 0, "transports": 0}
        if older_ids:
            placeholders = ",".join("?" for _ in older_ids)
            snapshot = param_set_snapshot(self.connection, param_set_id)
            if "inversion" in task_types:
                self._recompute_inversions(older_ids, placeholders, snapshot, limit, created, skipped)
            if "transport" in task_types:
                self._recompute_transports(older_ids, placeholders, snapshot, limit, created, skipped)
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="hydro.params.recompute",
            resource_type="hydro_param_set",
            resource_id=param_set_id,
            metadata={"created": created, "skipped": skipped, "task_types": task_types},
        )
        return {"parameter_set_id": param_set_id, "version": target["version"], "created": created, "skipped": skipped}

    @staticmethod
    def _stale_candidates(connection: sqlite3.Connection, table: str, placeholders: str, older_ids: list[int], limit: int) -> list[sqlite3.Row]:
        return connection.execute(
            f"SELECT * FROM {table} WHERE parameter_set_id IN ({placeholders}) AND status='done' AND stale=1 "
            "AND superseded_by_task_id IS NULL ORDER BY id LIMIT ?",
            (*older_ids, limit),
        ).fetchall()

    def _recompute_inversions(
        self, older_ids: list[int], placeholders: str, snapshot: dict[str, Any], limit: int,
        created: dict[str, list[int]], skipped: dict[str, int],
    ) -> None:
        candidates = self._stale_candidates(self.connection, "hydro_inversions", placeholders, older_ids, limit)
        if len(snapshot["endmembers"]) < 2:
            skipped["inversions"] += len(candidates)
            return
        now = to_storage(self.clock.now())
        for old in candidates:
            old_data = json.loads(old["input_json"])
            # 沿用冻结的样本与计算设置，只替换参数集快照，保证差异完全来自参数修订
            input_data = {key: old_data[key] for key in ("method", "max_iterations", "tolerance", "model_version")}
            input_data.update({"parameter_set": snapshot, "sample": old_data["sample"], "endmembers": snapshot["endmembers"]})
            key = _digest(input_data)
            existing = self.connection.execute("SELECT id FROM hydro_inversions WHERE task_key=?", (key,)).fetchone()
            if existing:
                new_id = existing["id"]
                skipped["inversions"] += 1
            else:
                cursor = self.connection.execute(
                    "INSERT INTO hydro_inversions(sample_id,task_key,model_version,method,input_json,status,"
                    "parameter_set_id,parameter_set_version,recompute_of_task_id,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,'queued',?,?,?,?,?)",
                    (
                        old["sample_id"], key, input_data["model_version"], input_data["method"],
                        json.dumps(input_data, ensure_ascii=False), snapshot["id"], snapshot["version"],
                        old["id"], now, now,
                    ),
                )
                new_id = int(cursor.lastrowid)
                created["inversions"].append(new_id)
            self.connection.execute(
                "UPDATE hydro_inversions SET superseded_by_task_id=? WHERE id=? AND superseded_by_task_id IS NULL",
                (new_id, old["id"]),
            )

    def _recompute_transports(
        self, older_ids: list[int], placeholders: str, snapshot: dict[str, Any], limit: int,
        created: dict[str, list[int]], skipped: dict[str, int],
    ) -> None:
        for old in self._stale_candidates(self.connection, "hydro_transport_runs", placeholders, older_ids, limit):
            old_data = json.loads(old["input_json"])
            payload = {
                "parameter_set_id": snapshot["id"],
                "source_concentration": old_data["source_concentration"],
                "distance_m": old_data["distance_m"],
                "duration_days": old_data["duration_days"],
                "step_days": old_data["step_days"],
                "model_version": old_data["model_version"],
            }
            effective = build_transport_input(payload, snapshot)
            task, is_new = insert_transport_run(self.connection, old["well_id"], effective)
            new_id = int(task["id"])
            if is_new:
                self.connection.execute(
                    "UPDATE hydro_transport_runs SET recompute_of_task_id=? WHERE id=?", (old["id"], new_id)
                )
                created["transports"].append(new_id)
            else:
                skipped["transports"] += 1
            self.connection.execute(
                "UPDATE hydro_transport_runs SET superseded_by_task_id=? WHERE id=? AND superseded_by_task_id IS NULL",
                (new_id, old["id"]),
            )

    # ------------------------------------------------------------------ 前后差异

    def compare_parameter_sets(self, principal: Principal, old_id: int, new_id: int) -> dict[str, Any]:
        principal.require("hydro.read")
        old_snapshot = param_set_snapshot(self.connection, old_id, require_published=False)
        new_snapshot = param_set_snapshot(self.connection, new_id, require_published=False)
        changes = [
            {"field": key, "old": old_snapshot[key], "new": new_snapshot[key]}
            for key in SCALAR_FIELDS
            if old_snapshot[key] != new_snapshot[key]
        ]
        old_members = {item["name"]: item for item in old_snapshot["endmembers"]}
        new_members = {item["name"]: item for item in new_snapshot["endmembers"]}
        endmember_changes = []
        for name in sorted(set(old_members) | set(new_members)):
            before, after = old_members.get(name), new_members.get(name)
            if before is None:
                endmember_changes.append({"name": name, "change": "added", "new": after})
            elif after is None:
                endmember_changes.append({"name": name, "change": "removed", "old": before})
            else:
                fields = {
                    key: {"old": before[key], "new": after[key]}
                    for key in ("isotope_d18o", "isotope_d2h", "solute_mg_l", "uncertainty")
                    if before[key] != after[key]
                }
                if fields:
                    endmember_changes.append({"name": name, "change": "modified", "fields": fields})
        return {
            "old": {"id": old_id, "version": old_snapshot["version"], "content_digest": old_snapshot["content_digest"]},
            "new": {"id": new_id, "version": new_snapshot["version"], "content_digest": new_snapshot["content_digest"]},
            "parameter_changes": changes,
            "endmember_changes": endmember_changes,
        }

    def compare_results(self, principal: Principal, task_type: str, old_task_id: int, new_task_id: int) -> dict[str, Any]:
        principal.require("hydro.read")
        if task_type not in TASK_TABLES:
            raise ValidationError("task_type 必须是 inversion 或 transport")
        table = TASK_TABLES[task_type]
        old_row = self.connection.execute(f"SELECT * FROM {table} WHERE id=?", (old_task_id,)).fetchone()
        new_row = self.connection.execute(f"SELECT * FROM {table} WHERE id=?", (new_task_id,)).fetchone()
        if old_row is None or new_row is None:
            raise NotFoundError("对比的任务不存在")
        old_result = json.loads(old_row["result_json"] or "{}")
        new_result = json.loads(new_row["result_json"] or "{}")
        old_input = json.loads(old_row["input_json"])
        new_input = json.loads(new_row["input_json"])
        diff: dict[str, Any] = {
            "task_type": task_type,
            "old_task": {"id": old_task_id, "status": old_row["status"], "parameter_set_version": old_row["parameter_set_version"]},
            "new_task": {"id": new_task_id, "status": new_row["status"], "parameter_set_version": new_row["parameter_set_version"]},
            "parameter_diff": self.compare_parameter_sets(
                principal, old_input["parameter_set"]["id"], new_input["parameter_set"]["id"]
            ),
        }
        if task_type == "inversion":
            diff["metrics"] = self._inversion_metric_diff(old_result, new_result)
        else:
            diff["metrics"] = self._transport_metric_diff(old_result, new_result)
        return diff

    @staticmethod
    def _inversion_metric_diff(old_result: dict[str, Any], new_result: dict[str, Any]) -> dict[str, Any]:
        if not old_result or not new_result:
            return {"available": False, "reason": "至少一个任务尚未产生结果"}
        old_fractions = {item["name"]: item["fraction"] for item in old_result.get("fractions", [])}
        new_fractions = {item["name"]: item["fraction"] for item in new_result.get("fractions", [])}
        fraction_diff = []
        for name in sorted(set(old_fractions) | set(new_fractions)):
            before, after = old_fractions.get(name), new_fractions.get(name)
            fraction_diff.append(
                {"name": name, "old": before, "new": after, "delta": None if before is None or after is None else round(after - before, 10)}
            )
        return {
            "available": True,
            "rmse": {"old": old_result.get("rmse"), "new": new_result.get("rmse"),
                     "delta": round(new_result["rmse"] - old_result["rmse"], 10)},
            "mass_balance": {"old": old_result.get("mass_balance"), "new": new_result.get("mass_balance")},
            "converged": {"old": old_result.get("converged"), "new": new_result.get("converged")},
            "fractions": fraction_diff,
        }

    @staticmethod
    def _transport_metric_diff(old_result: dict[str, Any], new_result: dict[str, Any]) -> dict[str, Any]:
        if not old_result or not new_result:
            return {"available": False, "reason": "至少一个任务尚未产生结果"}
        old_peak, new_peak = old_result.get("peak", {}), new_result.get("peak", {})
        return {
            "available": True,
            "peak_concentration": {
                "old": old_peak.get("concentration"), "new": new_peak.get("concentration"),
                "delta": round(new_peak.get("concentration", 0) - old_peak.get("concentration", 0), 12),
            },
            "peak_time_days": {"old": old_peak.get("time_days"), "new": new_peak.get("time_days")},
            "arrival_time_days": {
                "old": old_result.get("arrival_time_days"), "new": new_result.get("arrival_time_days"),
                "delta": round(new_result["arrival_time_days"] - old_result["arrival_time_days"], 10),
            },
        }
