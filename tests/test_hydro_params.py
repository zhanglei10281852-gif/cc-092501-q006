from __future__ import annotations

import json
import os
import sqlite3


def test_legacy_task_tables_are_migrated(tmp_path):
    # 上线前已存在的旧库：反演与迁移表没有参数集相关列，启动时必须就地补齐
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE hydro_inversions (
         id INTEGER PRIMARY KEY AUTOINCREMENT, sample_id INTEGER NOT NULL,
         task_key TEXT NOT NULL UNIQUE, model_version TEXT NOT NULL, method TEXT NOT NULL,
         input_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
         worker_id TEXT NOT NULL DEFAULT '', result_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
         created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE hydro_transport_runs (
         id INTEGER PRIMARY KEY AUTOINCREMENT, well_id INTEGER NOT NULL,
         task_key TEXT NOT NULL UNIQUE, model_version TEXT NOT NULL, input_json TEXT NOT NULL,
         status TEXT NOT NULL DEFAULT 'queued', result_json TEXT NOT NULL DEFAULT '{}',
         created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO hydro_inversions(sample_id,task_key,model_version,method,input_json,created_at,updated_at)
         VALUES(1,'legacy-key','mix-1','weighted-least-squares','{}','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00');
        """
    )
    connection.close()

    os.environ["TOWNSHIP_DATABASE_PATH"] = str(db_path)
    from app.database import close_connection, get_connection
    from app.hydro.service import ensure_schema

    close_connection()
    ensure_schema()
    inversion_columns = {row[1] for row in get_connection().execute("PRAGMA table_info(hydro_inversions)")}
    transport_columns = {row[1] for row in get_connection().execute("PRAGMA table_info(hydro_transport_runs)")}
    expected = {"parameter_set_id", "parameter_set_version", "stale", "stale_reason", "stale_since", "superseded_by_task_id", "recompute_of_task_id"}
    assert expected <= inversion_columns
    assert expected <= transport_columns
    # 旧数据保留且默认未标记
    row = get_connection().execute("SELECT * FROM hydro_inversions WHERE task_key='legacy-key'").fetchone()
    assert row["stale"] == 0
    assert row["parameter_set_id"] is None
    close_connection()


def create_well(client, headers, code="W-100"):
    response = client.post(
        "/api/hydro/wells",
        headers=headers,
        json={"code": code, "name": "参数集测试井", "latitude": 35.1, "longitude": 116.2, "aquifer": "浅层孔隙含水层", "screen_depth_m": 40},
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_sample(client, headers, well_id, code="S-100"):
    response = client.post(
        f"/api/hydro/wells/{well_id}/samples",
        headers=headers,
        json={"sample_code": code, "sampled_at": "2026-09-24T08:00:00+00:00", "isotope_d18o": -7.5, "isotope_d2h": -52.5, "solute_mg_l": 30, "detection_limit": 0.1, "measurement_error": 0.05},
    )
    assert response.status_code == 201, response.text
    return response.json()


def run_inversion(client, headers, sample_id, param_set_id):
    task = client.post(
        f"/api/hydro/samples/{sample_id}/inversions",
        headers=headers,
        json={"parameter_set_id": param_set_id, "max_iterations": 1000, "tolerance": 1e-10, "model_version": "mix-1"},
    )
    assert task.status_code == 202, task.text
    done = client.post(f"/api/hydro/inversions/{task.json()['id']}/run?worker_id=test", headers=headers)
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "done"
    return done.json()


def run_transport(client, headers, well_id, param_set_id):
    response = client.post(
        f"/api/hydro/wells/{well_id}/transport",
        headers=headers,
        json={"parameter_set_id": param_set_id, "source_concentration": 100, "distance_m": 100, "duration_days": 100, "step_days": 5, "model_version": "ade-1"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_param_set_lifecycle_and_audit(client, admin, hydro):
    draft = hydro.create_draft()
    assert draft["status"] == "draft"
    assert draft["review_state"] == "none"
    assert draft["version"] == 1
    assert draft["content_digest"]
    assert draft["base_version_id"] is None

    updated = client.patch(f"/api/hydro/param-sets/{draft['id']}", headers=hydro.author(), json={"porosity": 0.32, "notes": "修订孔隙率"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["porosity"] == 0.32
    assert updated.json()["content_digest"] != draft["content_digest"]

    submitted = client.post(f"/api/hydro/param-sets/{draft['id']}/submit", headers=hydro.author(), json={"comment": "请复核"})
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "in_review"
    assert submitted.json()["review_state"] == "pending"

    approved = client.post(f"/api/hydro/param-sets/{draft['id']}/approve", headers=hydro.reviewer(), json={"comment": "数据可信"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "in_review"
    assert approved.json()["review_state"] == "approved"

    published = client.post(f"/api/hydro/param-sets/{draft['id']}/publish", headers=admin["headers"], json={"expected_base_version_id": None})
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["status"] == "published"
    assert body["published_by"] == "admin"
    assert body["affected_results"] == {"inversion": 0, "transport": 0}

    history = [item["action"] for item in body["review_history"]]
    assert history == ["create", "update", "submit", "approve", "publish"]

    events = client.get("/api/audit?resource_type=hydro_param_set", headers=admin["headers"]).json()
    actions = [item["action"] for item in events["data"]]
    for expected in ("hydro.params.create", "hydro.params.update", "hydro.params.submit", "hydro.params.approve", "hydro.params.publish"):
        assert expected in actions


def test_draft_immutability_rules(client, admin, hydro):
    draft = hydro.create_draft()
    early_approve = client.post(f"/api/hydro/param-sets/{draft['id']}/approve", headers=hydro.reviewer(), json={"comment": ""})
    assert early_approve.status_code == 409
    early_publish = client.post(f"/api/hydro/param-sets/{draft['id']}/publish", headers=admin["headers"], json={"expected_base_version_id": None})
    assert early_publish.status_code == 409

    client.post(f"/api/hydro/param-sets/{draft['id']}/submit", headers=hydro.author(), json={"comment": ""})
    edit_in_review = client.patch(f"/api/hydro/param-sets/{draft['id']}", headers=hydro.author(), json={"porosity": 0.5})
    assert edit_in_review.status_code == 409

    client.post(f"/api/hydro/param-sets/{draft['id']}/approve", headers=hydro.reviewer(), json={"comment": ""})
    published = client.post(f"/api/hydro/param-sets/{draft['id']}/publish", headers=admin["headers"], json={"expected_base_version_id": None})
    assert published.status_code == 200
    edit_published = client.patch(f"/api/hydro/param-sets/{draft['id']}", headers=hydro.author(), json={"porosity": 0.5})
    assert edit_published.status_code == 409
    resubmit = client.post(f"/api/hydro/param-sets/{draft['id']}/submit", headers=hydro.author(), json={"comment": ""})
    assert resubmit.status_code == 409


def test_reject_returns_draft_for_rework(client, admin, hydro):
    draft = hydro.create_draft()
    client.post(f"/api/hydro/param-sets/{draft['id']}/submit", headers=hydro.author(), json={"comment": ""})
    no_comment = client.post(f"/api/hydro/param-sets/{draft['id']}/reject", headers=hydro.reviewer(), json={"comment": ""})
    assert no_comment.status_code == 422
    rejected = client.post(f"/api/hydro/param-sets/{draft['id']}/reject", headers=hydro.reviewer(), json={"comment": "端元不确定度需要补充来源"})
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "draft"
    assert rejected.json()["review_state"] == "rejected"

    rework = client.patch(f"/api/hydro/param-sets/{draft['id']}", headers=hydro.author(), json={"notes": "已补充端元来源"})
    assert rework.status_code == 200
    resubmitted = client.post(f"/api/hydro/param-sets/{draft['id']}/submit", headers=hydro.author(), json={"comment": "修改后重新提交"})
    assert resubmitted.status_code == 200
    assert resubmitted.json()["review_state"] == "pending"


def test_self_review_is_forbidden(client, admin, hydro):
    both = hydro.make_user("hydro.both", ["hydro.read", "hydro.params.write", "hydro.params.review"])
    draft = client.post("/api/hydro/param-sets", headers=both, json=hydro.param_set_payload())
    assert draft.status_code == 201, draft.text
    param_set_id = draft.json()["id"]
    assert client.post(f"/api/hydro/param-sets/{param_set_id}/submit", headers=both, json={"comment": ""}).status_code == 200
    self_review = client.post(f"/api/hydro/param-sets/{param_set_id}/approve", headers=both, json={"comment": "自审"})
    assert self_review.status_code == 422


def test_tasks_must_reference_published_version(client, admin, hydro):
    well = create_well(client, admin["headers"])
    sample = create_sample(client, admin["headers"], well["id"])
    draft = hydro.create_draft()

    inversion_draft = client.post(
        f"/api/hydro/samples/{sample['id']}/inversions",
        headers=admin["headers"],
        json={"parameter_set_id": draft["id"], "model_version": "mix-1"},
    )
    assert inversion_draft.status_code == 422
    inversion_missing = client.post(
        f"/api/hydro/samples/{sample['id']}/inversions",
        headers=admin["headers"],
        json={"parameter_set_id": 99999, "model_version": "mix-1"},
    )
    assert inversion_missing.status_code == 404
    transport_draft = client.post(
        f"/api/hydro/wells/{well['id']}/transport",
        headers=admin["headers"],
        json={"parameter_set_id": draft["id"], "source_concentration": 100, "distance_m": 100, "duration_days": 100, "step_days": 5, "model_version": "ade-1"},
    )
    assert transport_draft.status_code == 422


def test_publish_marks_affected_results_without_rewriting(client, admin, hydro):
    well = create_well(client, admin["headers"])
    sample = create_sample(client, admin["headers"], well["id"])
    v1 = hydro.publish_param_set()
    inversion = run_inversion(client, admin["headers"], sample["id"], v1["id"])
    transport = run_transport(client, admin["headers"], well["id"], v1["id"])

    v2 = hydro.publish_param_set(porosity=0.35, velocity_m_day=4.0)
    assert v2["version"] == 2
    assert v2["supersedes_version_id"] == v1["id"]
    assert v2["affected_results"] == {"inversion": 1, "transport": 1}

    inversion_after = client.get(f"/api/hydro/inversions/{inversion['id']}", headers=admin["headers"]).json()
    assert inversion_after["stale"] == 1
    assert inversion_after["stale_reason"] == f"superseded_by_param_set:{v2['id']}"
    assert inversion_after["stale_since"]
    # 历史结果只被标记，内容一个字节都不能变
    assert inversion_after["result_json"] == inversion["result_json"]
    transport_after = client.get(f"/api/hydro/transport/{transport['id']}", headers=admin["headers"]).json()
    assert transport_after["stale"] == 1
    assert transport_after["result_json"] == transport["result_json"]

    affected = client.get(f"/api/hydro/param-sets/{v2['id']}/affected", headers=admin["headers"]).json()
    assert [item["id"] for item in affected["inversions"]] == [inversion["id"]]
    assert [item["id"] for item in affected["transports"]] == [transport["id"]]


def test_concurrent_publish_conflict_and_rebase(client, admin, hydro):
    v1 = hydro.publish_param_set()

    draft_a = hydro.create_draft(porosity=0.31)
    client.post(f"/api/hydro/param-sets/{draft_a['id']}/submit", headers=hydro.author(), json={"comment": ""})
    client.post(f"/api/hydro/param-sets/{draft_a['id']}/approve", headers=hydro.reviewer(), json={"comment": ""})

    draft_b = hydro.create_draft(porosity=0.32)
    client.post(f"/api/hydro/param-sets/{draft_b['id']}/submit", headers=hydro.author(), json={"comment": ""})
    client.post(f"/api/hydro/param-sets/{draft_b['id']}/approve", headers=hydro.reviewer(), json={"comment": ""})

    published_a = client.post(f"/api/hydro/param-sets/{draft_a['id']}/publish", headers=admin["headers"], json={"expected_base_version_id": v1["id"]})
    assert published_a.status_code == 200, published_a.text
    assert published_a.json()["version"] == 2

    # B 仍基于 v1 发布，必须被旧版本冲突拦截
    conflict = client.post(f"/api/hydro/param-sets/{draft_b['id']}/publish", headers=admin["headers"], json={"expected_base_version_id": v1["id"]})
    assert conflict.status_code == 409

    rebased = client.post(f"/api/hydro/param-sets/{draft_b['id']}/rebase", headers=hydro.author())
    assert rebased.status_code == 200, rebased.text
    assert rebased.json()["base_version_id"] == published_a.json()["id"]
    assert rebased.json()["status"] == "draft"
    assert rebased.json()["review_state"] == "none"

    client.post(f"/api/hydro/param-sets/{draft_b['id']}/submit", headers=hydro.author(), json={"comment": "rebase 后重新提交"})
    client.post(f"/api/hydro/param-sets/{draft_b['id']}/approve", headers=hydro.reviewer(), json={"comment": ""})
    published_b = client.post(f"/api/hydro/param-sets/{draft_b['id']}/publish", headers=admin["headers"], json={"expected_base_version_id": published_a.json()["id"]})
    assert published_b.status_code == 200, published_b.text
    assert published_b.json()["version"] == 3
    assert published_b.json()["supersedes_version_id"] == published_a.json()["id"]


def test_retract_marks_results_and_blocks_new_tasks(client, admin, hydro):
    well = create_well(client, admin["headers"])
    v1 = hydro.publish_param_set()
    transport = run_transport(client, admin["headers"], well["id"], v1["id"])

    retracted = client.post(f"/api/hydro/param-sets/{v1['id']}/retract", headers=admin["headers"], json={"reason": "端元数据来源不可靠"})
    assert retracted.status_code == 200, retracted.text
    assert retracted.json()["status"] == "retracted"
    assert retracted.json()["retract_reason"] == "端元数据来源不可靠"
    assert retracted.json()["affected_results"]["transport"] == 1

    transport_after = client.get(f"/api/hydro/transport/{transport['id']}", headers=admin["headers"]).json()
    assert transport_after["stale"] == 1
    assert transport_after["stale_reason"] == "parameter_set_retracted"

    new_transport = client.post(
        f"/api/hydro/wells/{well['id']}/transport",
        headers=admin["headers"],
        json={"parameter_set_id": v1["id"], "source_concentration": 100, "distance_m": 100, "duration_days": 100, "step_days": 5, "model_version": "ade-1"},
    )
    assert new_transport.status_code == 422
    retract_again = client.post(f"/api/hydro/param-sets/{v1['id']}/retract", headers=admin["headers"], json={"reason": "重复撤销"})
    assert retract_again.status_code == 409

    fresh_draft = hydro.create_draft()
    assert fresh_draft["base_version_id"] is None


def test_batch_recompute_and_result_diff(client, admin, hydro):
    well = create_well(client, admin["headers"])
    sample = create_sample(client, admin["headers"], well["id"])
    v1 = hydro.publish_param_set()
    inversion_v1 = run_inversion(client, admin["headers"], sample["id"], v1["id"])
    transport_v1 = run_transport(client, admin["headers"], well["id"], v1["id"])

    v2 = hydro.publish_param_set(
        porosity=0.35,
        velocity_m_day=4.0,
        endmembers=[
            {"name": "山区降水", "isotope_d18o": -11, "isotope_d2h": -76, "solute_mg_l": 12, "uncertainty": 0.1},
            {"name": "河流渗漏", "isotope_d18o": -5, "isotope_d2h": -35, "solute_mg_l": 50, "uncertainty": 0.2},
        ],
    )
    recompute = client.post(
        f"/api/hydro/param-sets/{v2['id']}/recompute",
        headers=admin["headers"],
        json={"task_types": ["inversion", "transport"]},
    )
    assert recompute.status_code == 202, recompute.text
    created = recompute.json()["created"]
    assert len(created["inversions"]) == 1
    assert len(created["transports"]) == 1
    inversion_v2_id = created["inversions"][0]
    transport_v2_id = created["transports"][0]

    rerun = client.post(f"/api/hydro/inversions/{inversion_v2_id}/run?worker_id=rerun", headers=admin["headers"])
    assert rerun.status_code == 200, rerun.text
    assert rerun.json()["status"] == "done"
    assert rerun.json()["recompute_of_task_id"] == inversion_v1["id"]

    inversion_v1_after = client.get(f"/api/hydro/inversions/{inversion_v1['id']}", headers=admin["headers"]).json()
    assert inversion_v1_after["superseded_by_task_id"] == inversion_v2_id
    transport_v1_after = client.get(f"/api/hydro/transport/{transport_v1['id']}", headers=admin["headers"]).json()
    assert transport_v1_after["superseded_by_task_id"] == transport_v2_id

    transport_diff = client.get(
        f"/api/hydro/results/diff?task_type=transport&old={transport_v1['id']}&new={transport_v2_id}",
        headers=admin["headers"],
    ).json()
    assert transport_diff["metrics"]["available"] is True
    assert transport_diff["metrics"]["arrival_time_days"] == {"old": 50.0, "new": 25.0, "delta": -25.0}
    assert transport_diff["parameter_diff"]["old"]["version"] == 1
    assert transport_diff["parameter_diff"]["new"]["version"] == 2

    inversion_diff = client.get(
        f"/api/hydro/results/diff?task_type=inversion&old={inversion_v1['id']}&new={inversion_v2_id}",
        headers=admin["headers"],
    ).json()
    assert inversion_diff["metrics"]["available"] is True
    assert len(inversion_diff["metrics"]["fractions"]) == 2
    assert any(item["delta"] for item in inversion_diff["metrics"]["fractions"])

    # 批量重算是幂等的：旧任务都已挂上后继，再次执行不再创建新任务
    again = client.post(
        f"/api/hydro/param-sets/{v2['id']}/recompute",
        headers=admin["headers"],
        json={"task_types": ["inversion", "transport"]},
    )
    assert again.status_code == 202
    assert again.json()["created"] == {"inversions": [], "transports": []}


def test_param_set_diff_endpoint(client, admin, hydro):
    v1 = hydro.publish_param_set()
    v2 = hydro.publish_param_set(
        porosity=0.35,
        endmembers=[
            {"name": "山区降水", "isotope_d18o": -10, "isotope_d2h": -70, "solute_mg_l": 10, "uncertainty": 0.1},
            {"name": "河流渗漏", "isotope_d18o": -6, "isotope_d2h": -35, "solute_mg_l": 50, "uncertainty": 0.2},
            {"name": "侧向径流", "isotope_d18o": -8, "isotope_d2h": -55, "solute_mg_l": 25, "uncertainty": 0.3},
        ],
    )
    diff = client.get(f"/api/hydro/param-sets/diff?old={v1['id']}&new={v2['id']}", headers=admin["headers"])
    assert diff.status_code == 200, diff.text
    body = diff.json()
    changes = {item["field"]: item for item in body["parameter_changes"]}
    assert changes["porosity"] == {"field": "porosity", "old": 0.3, "new": 0.35}
    assert "velocity_m_day" not in changes
    endmember_changes = {item["name"]: item for item in body["endmember_changes"]}
    assert endmember_changes["河流渗漏"]["change"] == "modified"
    assert endmember_changes["河流渗漏"]["fields"]["isotope_d18o"] == {"old": -5, "new": -6}
    assert endmember_changes["侧向径流"]["change"] == "added"
    assert body["old"]["content_digest"] != body["new"]["content_digest"]


def test_permissions_are_enforced_and_denials_audited(client, admin, hydro):
    viewer = hydro.make_user("hydro.viewer", ["hydro.read"])
    draft = hydro.create_draft()

    create_denied = client.post("/api/hydro/param-sets", headers=viewer, json=hydro.param_set_payload())
    assert create_denied.status_code == 403
    publish_denied = client.post(f"/api/hydro/param-sets/{draft['id']}/publish", headers=viewer, json={"expected_base_version_id": None})
    assert publish_denied.status_code == 403
    recompute_denied = client.post(f"/api/hydro/param-sets/{draft['id']}/recompute", headers=viewer, json={})
    assert recompute_denied.status_code == 403

    # 有读权限可以正常查看
    detail = client.get(f"/api/hydro/param-sets/{draft['id']}", headers=viewer)
    assert detail.status_code == 200

    outsider = hydro.make_user("hydro.outsider", ["users.read"])
    read_denied = client.get(f"/api/hydro/param-sets/{draft['id']}", headers=outsider)
    assert read_denied.status_code == 403

    events = client.get("/api/audit?action=hydro.access.denied", headers=admin["headers"]).json()
    assert events["total"] == 4
    assert {item["outcome"] for item in events["data"]} == {"denied"}
    denied_permissions = {json.loads(item["metadata_json"])["required_permission"] for item in events["data"]}
    assert denied_permissions == {"hydro.params.write", "hydro.params.publish", "hydro.recompute", "hydro.read"}


def test_write_only_user_can_complete_own_flow(client, admin, hydro):
    # 只持有编辑权限（无 hydro.read）的用户也能完成建稿与提交，响应由内部详情构造
    writer = hydro.make_user("hydro.writer", ["hydro.params.write"])
    created = client.post("/api/hydro/param-sets", headers=writer, json=hydro.param_set_payload())
    assert created.status_code == 201, created.text
    param_set_id = created.json()["id"]
    assert created.json()["status"] == "draft"
    submitted = client.post(f"/api/hydro/param-sets/{param_set_id}/submit", headers=writer, json={"comment": ""})
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "in_review"
    # 但没有读权限，不能查看详情接口
    assert client.get(f"/api/hydro/param-sets/{param_set_id}", headers=writer).status_code == 403


def test_group_versions_and_listing(client, admin, hydro):
    v1 = hydro.publish_param_set()
    v2 = hydro.publish_param_set(porosity=0.31)
    versions = client.get("/api/hydro/param-sets/group-versions?site_code=site-north&code=baseline", headers=admin["headers"])
    assert versions.status_code == 200, versions.text
    body = versions.json()
    assert [item["version"] for item in body] == [1, 2]
    assert body[0]["status"] == "published"
    assert body[1]["supersedes_version_id"] == v1["id"]

    listed = client.get("/api/hydro/param-sets?status=published&site_code=SITE-NORTH", headers=admin["headers"])
    assert listed.status_code == 200
    assert listed.json()["total"] == 2
    assert {item["id"] for item in listed.json()["data"]} == {v1["id"], v2["id"]}

    missing = client.get("/api/hydro/param-sets/group-versions?site_code=NOPE&code=NONE", headers=admin["headers"])
    assert missing.status_code == 404
