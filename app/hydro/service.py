from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.core.errors import NotFoundError, ValidationError
from app.database import get_connection, transaction


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
 parameter_set_id INTEGER, parameter_set_version INTEGER,
 stale INTEGER NOT NULL DEFAULT 0, stale_reason TEXT NOT NULL DEFAULT '', stale_since TEXT,
 superseded_by_task_id INTEGER, recompute_of_task_id INTEGER,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hydro_transport_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, well_id INTEGER NOT NULL REFERENCES hydro_wells(id) ON DELETE RESTRICT,
 task_key TEXT NOT NULL UNIQUE, model_version TEXT NOT NULL, input_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', result_json TEXT NOT NULL DEFAULT '{}',
 parameter_set_id INTEGER, parameter_set_version INTEGER,
 stale INTEGER NOT NULL DEFAULT 0, stale_reason TEXT NOT NULL DEFAULT '', stale_since TEXT,
 superseded_by_task_id INTEGER, recompute_of_task_id INTEGER,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hydro_param_sets (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 site_code TEXT NOT NULL, code TEXT NOT NULL, version INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','in_review','published','retracted')),
 review_state TEXT NOT NULL DEFAULT 'none' CHECK(review_state IN ('none','pending','approved','rejected')),
 porosity REAL NOT NULL, velocity_m_day REAL NOT NULL, dispersion_m2_day REAL NOT NULL,
 decay_per_day REAL NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '',
 content_digest TEXT NOT NULL,
 base_version_id INTEGER REFERENCES hydro_param_sets(id),
 supersedes_version_id INTEGER REFERENCES hydro_param_sets(id),
 created_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 submitted_by TEXT, submitted_at TEXT,
 reviewed_by TEXT, reviewed_at TEXT, review_comment TEXT,
 published_by TEXT, published_at TEXT,
 retracted_by TEXT, retracted_at TEXT, retract_reason TEXT,
 UNIQUE(site_code,code,version)
);
CREATE TABLE IF NOT EXISTS hydro_param_set_endmembers (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 param_set_id INTEGER NOT NULL REFERENCES hydro_param_sets(id) ON DELETE CASCADE,
 name TEXT NOT NULL, isotope_d18o REAL NOT NULL, isotope_d2h REAL NOT NULL,
 solute_mg_l REAL NOT NULL, uncertainty REAL NOT NULL,
 source_endmember_id INTEGER, position INTEGER NOT NULL,
 UNIQUE(param_set_id,name)
);
CREATE TABLE IF NOT EXISTS hydro_param_set_reviews (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 param_set_id INTEGER NOT NULL REFERENCES hydro_param_sets(id) ON DELETE CASCADE,
 action TEXT NOT NULL CHECK(action IN ('create','update','submit','approve','reject','rebase','publish','retract')),
 actor TEXT NOT NULL, comment TEXT NOT NULL DEFAULT '',
 from_status TEXT NOT NULL, to_status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hydro_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, resource_type TEXT NOT NULL, resource_id INTEGER,
 action TEXT NOT NULL, actor TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hydro_samples_well ON hydro_samples(well_id,sampled_at);
CREATE INDEX IF NOT EXISTS idx_hydro_inversions_status ON hydro_inversions(status,created_at);
CREATE INDEX IF NOT EXISTS idx_hydro_param_sets_group ON hydro_param_sets(site_code,code,status,version);
CREATE INDEX IF NOT EXISTS idx_hydro_param_set_endmembers_set ON hydro_param_set_endmembers(param_set_id,position);
"""

# 旧库缺列时按定义补齐，保证已部署的数据库可以就地升级
TASK_TABLE_COLUMNS: dict[str, dict[str, str]] = {
    "hydro_inversions": {
        "parameter_set_id": "parameter_set_id INTEGER",
        "parameter_set_version": "parameter_set_version INTEGER",
        "stale": "stale INTEGER NOT NULL DEFAULT 0",
        "stale_reason": "stale_reason TEXT NOT NULL DEFAULT ''",
        "stale_since": "stale_since TEXT",
        "superseded_by_task_id": "superseded_by_task_id INTEGER",
        "recompute_of_task_id": "recompute_of_task_id INTEGER",
    },
    "hydro_transport_runs": {
        "parameter_set_id": "parameter_set_id INTEGER",
        "parameter_set_version": "parameter_set_version INTEGER",
        "stale": "stale INTEGER NOT NULL DEFAULT 0",
        "stale_reason": "stale_reason TEXT NOT NULL DEFAULT ''",
        "stale_since": "stale_since TEXT",
        "superseded_by_task_id": "superseded_by_task_id INTEGER",
        "recompute_of_task_id": "recompute_of_task_id INTEGER",
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def ensure_schema() -> None:
    connection = get_connection()
    connection.executescript(SCHEMA)
    for table, columns in TASK_TABLE_COLUMNS.items():
        for column, definition in columns.items():
            _ensure_column(connection, table, column, definition)
    # 依赖补齐列的索引必须在 ALTER TABLE 之后创建，否则旧库迁移会失败
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_hydro_inversions_stale ON hydro_inversions(stale,parameter_set_id);
        CREATE INDEX IF NOT EXISTS idx_hydro_transport_stale ON hydro_transport_runs(stale,parameter_set_id);
        """
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def param_set_snapshot(connection: sqlite3.Connection, param_set_id: int, *, require_published: bool = True) -> dict[str, Any]:
    """读取参数集及其端元快照；任务引用时强制要求已发布的不可变版本。"""
    row = connection.execute("SELECT * FROM hydro_param_sets WHERE id=?", (param_set_id,)).fetchone()
    if row is None:
        raise NotFoundError("参数集不存在")
    if require_published and row["status"] != "published":
        raise ValidationError("反演与迁移任务必须引用已发布的参数集版本")
    endmembers = connection.execute(
        "SELECT * FROM hydro_param_set_endmembers WHERE param_set_id=? ORDER BY position,id", (param_set_id,)
    ).fetchall()
    return {
        "id": row["id"],
        "site_code": row["site_code"],
        "code": row["code"],
        "version": row["version"],
        "content_digest": row["content_digest"],
        "porosity": row["porosity"],
        "velocity_m_day": row["velocity_m_day"],
        "dispersion_m2_day": row["dispersion_m2_day"],
        "decay_per_day": row["decay_per_day"],
        "endmembers": [dict(item) for item in endmembers],
    }


def build_transport_input(payload: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """迁移任务的水动力参数一律取自参数集快照，请求体不再接受流速等字段。"""
    return {
        "parameter_set_id": snapshot["id"],
        "source_concentration": payload["source_concentration"],
        "distance_m": payload["distance_m"],
        "duration_days": payload["duration_days"],
        "step_days": payload["step_days"],
        "model_version": payload["model_version"],
        "velocity_m_day": snapshot["velocity_m_day"],
        "dispersion_m2_day": snapshot["dispersion_m2_day"],
        "decay_per_day": snapshot["decay_per_day"],
        "parameter_set": snapshot,
    }


def compute_transport_result(effective: dict[str, Any]) -> dict[str, Any]:
    points=[]; t=effective["step_days"]
    while t<=effective["duration_days"]+1e-12:
        d=effective["dispersion_m2_day"]; x=effective["distance_m"]; v=effective["velocity_m_day"]
        c=effective["source_concentration"]*math.exp(-((x-v*t)**2)/(4*d*t))*math.exp(-effective["decay_per_day"]*t)/math.sqrt(4*math.pi*d*t)
        points.append({"time_days":round(t,8),"concentration":c}); t+=effective["step_days"]
    peak=max(points,key=lambda p:p["concentration"])
    return {"points":points,"peak":peak,"arrival_time_days":effective["distance_m"]/effective["velocity_m_day"],"model_version":effective["model_version"]}


def insert_transport_run(connection: sqlite3.Connection, well_id: int, effective: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """按任务键去重并写入迁移结果；返回 (任务行, 是否新建)。调用方负责事务。"""
    key=_digest({"well_id":well_id,**effective})
    old=connection.execute("SELECT * FROM hydro_transport_runs WHERE task_key=?",(key,)).fetchone()
    if old: return dict(old), False
    result=compute_transport_result(effective)
    now=_now()
    snapshot=effective["parameter_set"]
    cursor=connection.execute(
        "INSERT INTO hydro_transport_runs(well_id,task_key,model_version,input_json,status,result_json,parameter_set_id,parameter_set_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (well_id,key,effective["model_version"],json.dumps(effective,ensure_ascii=False),"done",json.dumps(result,ensure_ascii=False),snapshot["id"],snapshot["version"],now,now))
    return dict(connection.execute("SELECT * FROM hydro_transport_runs WHERE id=?",(cursor.lastrowid,)).fetchone()), True


class HydroService:
    def __init__(self, connection: sqlite3.Connection | None = None):
        self.connection = connection or get_connection()
        ensure_schema()

    def create_well(self, payload: dict[str, Any], actor: str = "researcher") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            cursor = connection.execute("INSERT INTO hydro_wells(code,name,latitude,longitude,aquifer,screen_depth_m,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (payload["code"],payload["name"],payload["latitude"],payload["longitude"],payload["aquifer"],payload["screen_depth_m"],now,now))
            well_id = cursor.lastrowid
            connection.execute("INSERT INTO hydro_audit(resource_type,resource_id,action,actor,payload_json,created_at) VALUES('well',?,?,?,?,?)", (well_id,"create",actor,json.dumps(payload,ensure_ascii=False),now))
            return dict(connection.execute("SELECT * FROM hydro_wells WHERE id=?",(well_id,)).fetchone())

    def get_well(self, well_id: int) -> dict[str, Any] | None:
        well = self.connection.execute("SELECT * FROM hydro_wells WHERE id=?",(well_id,)).fetchone()
        if well is None: return None
        result = dict(well)
        result["samples"] = [dict(r) for r in self.connection.execute("SELECT * FROM hydro_samples WHERE well_id=? ORDER BY sampled_at,id",(well_id,)).fetchall()]
        return result

    def delete_well(self, well_id: int) -> bool:
        with transaction(immediate=True) as connection:
            cursor = connection.execute("DELETE FROM hydro_wells WHERE id=?",(well_id,))
            if cursor.rowcount == 0: raise KeyError("well_not_found")
            return True

    def create_endmember(self, payload: dict[str, Any]) -> dict[str, Any]:
        now=_now()
        with transaction(immediate=True) as connection:
            cursor=connection.execute("INSERT INTO hydro_endmembers(name,isotope_d18o,isotope_d2h,solute_mg_l,uncertainty,version,created_at) VALUES(?,?,?,?,?,?,?)",(payload["name"],payload["isotope_d18o"],payload["isotope_d2h"],payload["solute_mg_l"],payload["uncertainty"],payload["version"],now))
            return dict(connection.execute("SELECT * FROM hydro_endmembers WHERE id=?",(cursor.lastrowid,)).fetchone())

    def add_sample(self, well_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        if self.connection.execute("SELECT id FROM hydro_wells WHERE id=?",(well_id,)).fetchone() is None: raise KeyError("well_not_found")
        values=[payload.get("isotope_d18o"),payload.get("isotope_d2h"),payload.get("solute_mg_l")]
        quality="usable" if sum(v is not None for v in values)>=2 else "incomplete"
        now=_now()
        with transaction(immediate=True) as connection:
            cursor=connection.execute("INSERT INTO hydro_samples(well_id,sample_code,sampled_at,isotope_d18o,isotope_d2h,solute_mg_l,detection_limit,measurement_error,quality_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(well_id,payload["sample_code"],payload["sampled_at"],payload.get("isotope_d18o"),payload.get("isotope_d2h"),payload.get("solute_mg_l"),payload["detection_limit"],payload["measurement_error"],quality,now))
            return dict(connection.execute("SELECT * FROM hydro_samples WHERE id=?",(cursor.lastrowid,)).fetchone())

    def _project_simplex(self, values: list[float]) -> list[float]:
        clipped=[max(0.0,v) for v in values]
        total=sum(clipped)
        return [1/len(values)]*len(values) if total<=1e-15 else [v/total for v in clipped]

    def solve_mixture(self, sample: sqlite3.Row | dict[str, Any], endmembers: list[dict[str, Any]], max_iterations: int, tolerance: float) -> dict[str, Any]:
        observed=[sample["isotope_d18o"],sample["isotope_d2h"],sample["solute_mg_l"]]
        active=[i for i,v in enumerate(observed) if v is not None]
        if len(active)<2: raise ValueError("insufficient_measurements")
        fractions=[1/len(endmembers)]*len(endmembers)
        scale=[20.0,100.0,max(1.0,float(sample["solute_mg_l"] or 1))]
        rate=0.08
        last=float("inf")
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

    def enqueue_inversion(self, sample_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        sample=self.connection.execute("SELECT * FROM hydro_samples WHERE id=?",(sample_id,)).fetchone()
        if sample is None: raise KeyError("sample_not_found")
        snapshot=param_set_snapshot(self.connection, payload["parameter_set_id"])
        endmembers=snapshot["endmembers"]
        if len(endmembers)<2: raise ValueError("insufficient_endmembers")
        input_data={key:payload[key] for key in ("method","max_iterations","tolerance","model_version")}
        input_data.update({"parameter_set":snapshot,"sample":dict(sample),"endmembers":endmembers})
        key=_digest(input_data); now=_now()
        with transaction(immediate=True) as connection:
            old=connection.execute("SELECT * FROM hydro_inversions WHERE task_key=?",(key,)).fetchone()
            if old: return dict(old)
            cursor=connection.execute(
                "INSERT INTO hydro_inversions(sample_id,task_key,model_version,method,input_json,parameter_set_id,parameter_set_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (sample_id,key,payload["model_version"],payload["method"],json.dumps(input_data,ensure_ascii=False),snapshot["id"],snapshot["version"],now,now))
            return dict(connection.execute("SELECT * FROM hydro_inversions WHERE id=?",(cursor.lastrowid,)).fetchone())

    def run_inversion(self, task_id: int, worker_id: str) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            task=connection.execute("SELECT * FROM hydro_inversions WHERE id=?",(task_id,)).fetchone()
            if task is None: raise KeyError("task_not_found")
            if task["status"]=="done": return dict(task)
            connection.execute("UPDATE hydro_inversions SET status='running',attempts=attempts+1,worker_id=?,updated_at=? WHERE id=?",(worker_id,_now(),task_id))
        data=json.loads(task["input_json"])
        # 端元与样本一律取任务入队时冻结的快照，重跑历史任务不受后续参数修订影响
        sample=data["sample"]
        endmembers=data["endmembers"]
        try: result=self.solve_mixture(sample,endmembers,data["max_iterations"],data["tolerance"])
        except Exception as exc:
            with transaction(immediate=True) as connection: connection.execute("UPDATE hydro_inversions SET status='failed',error=?,updated_at=? WHERE id=?",(str(exc),_now(),task_id))
            raise
        with transaction(immediate=True) as connection:
            connection.execute("UPDATE hydro_inversions SET status='done',result_json=?,error='',updated_at=? WHERE id=?",(json.dumps(result,ensure_ascii=False),_now(),task_id))
            return dict(connection.execute("SELECT * FROM hydro_inversions WHERE id=?",(task_id,)).fetchone())

    def get_inversion(self, task_id: int) -> dict[str, Any] | None:
        row=self.connection.execute("SELECT * FROM hydro_inversions WHERE id=?",(task_id,)).fetchone()
        return dict(row) if row else None

    def get_transport_run(self, task_id: int) -> dict[str, Any] | None:
        row=self.connection.execute("SELECT * FROM hydro_transport_runs WHERE id=?",(task_id,)).fetchone()
        return dict(row) if row else None

    def run_transport(self, well_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        if self.connection.execute("SELECT id FROM hydro_wells WHERE id=?",(well_id,)).fetchone() is None: raise KeyError("well_not_found")
        snapshot=param_set_snapshot(self.connection, payload["parameter_set_id"])
        effective=build_transport_input(payload, snapshot)
        with transaction(immediate=True) as connection:
            task, _ = insert_transport_run(connection, well_id, effective)
            return task
