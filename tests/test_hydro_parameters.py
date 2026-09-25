from __future__ import annotations

from tests.test_hydro import create_well, endmembers, publish_parameter_set


def make_draft(client, headers, site_code, *, velocity=2.0, dispersion=5.0, porosity=0.25, members=None,
               note="草稿"):
    response = client.post(
        f"/api/hydro/sites/{site_code}/parameter-sets",
        headers=headers,
        json={"porosity": porosity, "velocity_m_day": velocity, "dispersion_m2_day": dispersion,
              "endmembers": members or endmembers(), "change_note": note},
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_sample(client, headers, well, code="S-100"):
    response = client.post(
        f"/api/hydro/wells/{well['id']}/samples",
        headers=headers,
        json={"sample_code": code, "sampled_at": "2026-09-24T08:00:00+00:00",
              "isotope_d18o": -7.5, "isotope_d2h": -52.5, "solute_mg_l": 30},
    )
    assert response.status_code == 201, response.text
    return response.json()


def run_done_inversion(client, headers, sample_id, parameter_set_id):
    task = client.post(
        f"/api/hydro/samples/{sample_id}/inversions",
        headers=headers, json={"parameter_set_id": parameter_set_id},
    )
    assert task.status_code == 202, task.text
    done = client.post(f"/api/hydro/inversions/{task.json()['id']}/run?worker_id=w1", headers=headers)
    assert done.status_code == 200, done.text
    return done.json()


def test_draft_review_publish_revoke_lifecycle(client, admin):
    headers = admin["headers"]
    well = create_well(client, headers, "W-100")
    draft = make_draft(client, headers, well["code"])
    assert draft["status"] == "draft"
    assert draft["revision"] is None
    assert draft["is_current"] is False
    assert len(draft["endmembers"]) == 2

    # 提交复核
    submitted = client.post(f"/api/hydro/parameter-sets/{draft['id']}/submit", headers=headers)
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "in_review"
    assert submitted.json()["submitted_by"] == "系统管理员"

    # 复核中不允许直接改稿
    revise = client.patch(
        f"/api/hydro/parameter-sets/{draft['id']}", headers=headers,
        json={"porosity": 0.3, "base_hash": draft["content_hash"]},
    )
    assert revise.status_code == 409

    # 复核退回，填写意见后可继续修订
    rejected = client.post(
        f"/api/hydro/parameter-sets/{draft['id']}/reject", headers=headers,
        json={"reason": "流速依据不足"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "draft"
    assert rejected.json()["review_comment"] == "流速依据不足"

    # 修订后重新走流程
    revised = client.patch(
        f"/api/hydro/parameter-sets/{draft['id']}", headers=headers,
        json={"velocity_m_day": 2.5, "change_note": "按新抽水试验修订", "base_hash": draft["content_hash"]},
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["velocity_m_day"] == 2.5
    assert revised.json()["content_hash"] != draft["content_hash"]
    client.post(f"/api/hydro/parameter-sets/{draft['id']}/submit", headers=headers)
    published = client.post(f"/api/hydro/parameter-sets/{draft['id']}/publish", headers=headers, json={})
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["status"] == "published"
    assert body["revision"] == 1
    assert body["is_current"] is True

    # 发布后不可变：不能再改
    immutable = client.patch(
        f"/api/hydro/parameter-sets/{draft['id']}", headers=headers,
        json={"porosity": 0.4, "base_hash": body["content_hash"]},
    )
    assert immutable.status_code == 409

    # 撤销
    revoked = client.post(
        f"/api/hydro/parameter-sets/{draft['id']}/revoke", headers=headers,
        json={"reason": "端元测试方法作废"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["revoke_reason"] == "端元测试方法作废"

    # 撤销后不能再发布
    republish = client.post(f"/api/hydro/parameter-sets/{draft['id']}/publish", headers=headers, json={})
    assert republish.status_code == 409


def test_concurrent_revision_optimistic_lock(client, admin):
    headers = admin["headers"]
    well = create_well(client, headers, "W-101")
    draft = make_draft(client, headers, well["code"])
    # 分析师甲先改了孔隙率
    first = client.patch(
        f"/api/hydro/parameter-sets/{draft['id']}", headers=headers,
        json={"porosity": 0.3, "base_hash": draft["content_hash"]},
    )
    assert first.status_code == 200
    # 分析师乙拿着旧 hash 再改，必须检测到冲突
    stale = client.patch(
        f"/api/hydro/parameter-sets/{draft['id']}", headers=headers,
        json={"velocity_m_day": 3.0, "base_hash": draft["content_hash"]},
    )
    assert stale.status_code == 409
    assert "已被他人修改" in stale.json()["error"]["message"]


def test_concurrent_publish_conflict_and_force(client, admin):
    headers = admin["headers"]
    well = create_well(client, headers, "W-102")
    first = publish_parameter_set(client, headers, well["code"], velocity=2.0)

    # 两个并起草稿都基于 rev1
    draft_a = make_draft(client, headers, well["code"], velocity=2.6, note="修订A")
    draft_b = make_draft(client, headers, well["code"], velocity=2.8, note="修订B")
    for draft_id in (draft_a["id"], draft_b["id"]):
        client.post(f"/api/hydro/parameter-sets/{draft_id}/submit", headers=headers)

    # A 先发布成功
    published_a = client.post(f"/api/hydro/parameter-sets/{draft_a['id']}/publish", headers=headers, json={})
    assert published_a.status_code == 200
    assert published_a.json()["revision"] == 2

    # B 再发布：基准版本冲突
    conflict = client.post(f"/api/hydro/parameter-sets/{draft_b['id']}/publish", headers=headers, json={})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["context"]["current_published_id"] == draft_a["id"]

    # 确认后允许强制发布
    forced = client.post(
        f"/api/hydro/parameter-sets/{draft_b['id']}/publish?force=true", headers=headers, json={},
    )
    assert forced.status_code == 200, forced.text
    assert forced.json()["revision"] == 3
    # A 被 B 取代
    a_view = client.get(f"/api/hydro/parameter-sets/{draft_a['id']}", headers=headers).json()
    assert a_view["is_current"] is False
    assert a_view["superseded_by_id"] == draft_b["id"]


def test_publish_marks_affected_without_rewriting_history(client, admin):
    headers = admin["headers"]
    well = create_well(client, headers, "W-103")
    v1 = publish_parameter_set(client, headers, well["code"], velocity=2.0)
    sample = make_sample(client, headers, well, "S-200")
    old_inversion = run_done_inversion(client, headers, sample["id"], v1["id"])
    old_transport = client.post(
        f"/api/hydro/wells/{well['id']}/transport", headers=headers,
        json={"parameter_set_id": v1["id"], "source_concentration": 100, "distance_m": 100,
              "decay_per_day": 0.01, "duration_days": 50, "step_days": 5},
    ).json()
    assert old_inversion["impact_status"] == "current"

    # 发布新版本：旧任务只被标记 affected，结果行不被修改
    v2 = publish_parameter_set(client, headers, well["code"], velocity=3.2, note="新调查资料")
    assert v2["affected"] == {"inversions": 1, "transports": 1}

    old_inversion_view = client.get(f"/api/hydro/inversions/{old_inversion['id']}", headers=headers).json()
    old_transport_view = client.get(f"/api/hydro/transports/{old_transport['id']}", headers=headers).json()
    assert old_inversion_view["impact_status"] == "affected"
    assert old_transport_view["impact_status"] == "affected"
    # 历史结果原文未变
    assert old_inversion_view["result_json"] == old_inversion["result_json"]
    assert old_transport_view["result_json"] == old_transport["result_json"]
    # 仍引用不可变的旧版本号与哈希
    assert old_inversion_view["parameter_revision"] == 1
    assert old_inversion_view["parameter_hash"] == v1["content_hash"]

    affected = client.get(f"/api/hydro/affected-results?site_code={well['code']}", headers=headers)
    assert affected.status_code == 200
    assert {item["id"] for item in affected.json()["inversions"]} == {old_inversion["id"]}
    assert {item["id"] for item in affected.json()["transports"]} == {old_transport["id"]}


def test_batch_recalculate_creates_new_runs_and_diffs(client, admin):
    headers = admin["headers"]
    well = create_well(client, headers, "W-104")
    v1 = publish_parameter_set(client, headers, well["code"], velocity=2.0, dispersion=5.0)
    sample = make_sample(client, headers, well, "S-300")
    old_inversion = run_done_inversion(client, headers, sample["id"], v1["id"])
    old_transport = client.post(
        f"/api/hydro/wells/{well['id']}/transport", headers=headers,
        json={"parameter_set_id": v1["id"], "source_concentration": 100, "distance_m": 100,
              "decay_per_day": 0.01, "duration_days": 50, "step_days": 5},
    ).json()
    publish_parameter_set(client, headers, well["code"], velocity=4.0, dispersion=8.0, note="修订")

    # 只重算迁移任务
    recalc = client.post(
        f"/api/hydro/parameter-sets/{2}/recalculate", headers=headers, json={"kinds": ["transport"]},
    )
    assert recalc.status_code == 200, recalc.text
    body = recalc.json()
    assert len(body["recalculated"]) == 1
    item = body["recalculated"][0]
    assert item["kind"] == "transport"
    assert item["previous_task_id"] == old_transport["id"]
    assert item["summary"]["arrival_time_days_old"] == 50.0
    assert item["summary"]["arrival_time_days_new"] == 25.0
    assert item["summary"]["arrival_time_days_delta"] == -25.0

    # 旧任务转为 recalculated 并指向新任务；新任务是 current，引用 rev2
    old_view = client.get(f"/api/hydro/transports/{old_transport['id']}", headers=headers).json()
    assert old_view["impact_status"] == "recalculated"
    new_id = old_view["superseded_by_run_id"]
    new_view = client.get(f"/api/hydro/transports/{new_id}", headers=headers).json()
    assert new_view["impact_status"] == "current"
    assert new_view["parameter_revision"] == 2
    assert new_view["recalculation_of"] == old_transport["id"]

    # 反演任务仍为 affected（本次未选择）
    inversion_view = client.get(f"/api/hydro/inversions/{old_inversion['id']}", headers=headers).json()
    assert inversion_view["impact_status"] == "affected"

    # 差异可查
    diffs = client.get("/api/hydro/parameter-sets/2/diffs", headers=headers)
    assert diffs.status_code == 200
    assert len(diffs.json()) == 1
    assert diffs.json()[0]["kind"] == "transport"

    # 幂等：再次重算不会重复生成
    again = client.post(
        f"/api/hydro/parameter-sets/2/recalculate", headers=headers, json={"kinds": ["transport"]},
    )
    assert again.status_code == 200
    assert again.json()["recalculated"] == []
    assert again.json()["skipped"][0]["reason"] == "already_recalculated"


def test_every_state_change_is_audited(client, admin):
    headers = admin["headers"]
    well = create_well(client, headers, "W-105")
    draft = make_draft(client, headers, well["code"])
    client.post(f"/api/hydro/parameter-sets/{draft['id']}/submit", headers=headers)
    client.post(f"/api/hydro/parameter-sets/{draft['id']}/reject", headers=headers,
                json={"reason": "退回"})
    client.post(f"/api/hydro/parameter-sets/{draft['id']}/submit", headers=headers)
    client.post(f"/api/hydro/parameter-sets/{draft['id']}/publish", headers=headers, json={"note": "发布"})
    client.post(f"/api/hydro/parameter-sets/{draft['id']}/revoke", headers=headers,
                json={"reason": "撤销"})

    for action in (
        "hydro.parameter_set.draft",
        "hydro.parameter_set.submit",
        "hydro.parameter_set.reject",
        "hydro.parameter_set.publish",
        "hydro.parameter_set.revoke",
    ):
        events = client.get(f"/api/audit?action={action}", headers=headers)
        assert events.status_code == 200
        assert events.json()["total"] >= 1, action
        event = events.json()["data"][0]
        assert event["actor_name"] == "系统管理员"
        assert event["outcome"] == "success"


def test_permissions_are_enforced(client, admin):
    # 创建一个没有任何 hydro 权限的只读审计角色用户
    role = client.post(
        "/api/roles", headers=admin["headers"],
        json={"code": "hydro.viewer", "name": "水文观察员",
              "permission_codes": ["hydro.parameters.read", "hydro.results.read"]},
    )
    assert role.status_code == 201, role.text
    user = client.post(
        "/api/users", headers=admin["headers"],
        json={"username": "hydro.user", "password": "Hydro!23456", "display_name": "观察员",
              "role_codes": ["hydro.viewer"]},
    )
    assert user.status_code == 201, user.text
    login = client.post("/api/auth/login", json={"username": "hydro.user",
                                                 "password": "Hydro!23456", "client_label": "tests"})
    viewer_headers = {"Authorization": f"Bearer {login.json()['token']}"}

    well = create_well(client, admin["headers"], "W-106")
    # 无起草权限 → 403
    denied = client.post(
        f"/api/hydro/sites/{well['code']}/parameter-sets", headers=viewer_headers,
        json={"porosity": 0.25, "velocity_m_day": 2.0, "dispersion_m2_day": 5.0,
              "endmembers": endmembers()},
    )
    assert denied.status_code == 403
    # 有只读权限
    listing = client.get("/api/hydro/parameter-sets", headers=viewer_headers)
    assert listing.status_code == 200

    # 无认证 → 401
    assert client.post(
        f"/api/hydro/sites/{well['code']}/parameter-sets",
        json={"porosity": 0.25, "velocity_m_day": 2.0, "dispersion_m2_day": 5.0,
              "endmembers": endmembers()},
    ).status_code == 401
