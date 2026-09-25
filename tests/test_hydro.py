from __future__ import annotations


def create_well(client, headers, code="W-001"):
    response = client.post(
        "/api/hydro/wells",
        headers=headers,
        json={"code": code, "name": "北部监测井", "latitude": 35.1, "longitude": 116.2,
              "aquifer": "浅层孔隙含水层", "screen_depth_m": 42},
    )
    assert response.status_code == 201, response.text
    return response.json()


def endmembers(d18o_a=-10, d2h_a=-70, solute_a=10, d18o_b=-5, d2h_b=-35, solute_b=50):
    return [
        {"code": "RAIN", "name": "山区降水", "isotope_d18o": d18o_a, "isotope_d2h": d2h_a,
         "solute_mg_l": solute_a, "uncertainty": 0.1},
        {"code": "RIVER", "name": "河流渗漏", "isotope_d18o": d18o_b, "isotope_d2h": d2h_b,
         "solute_mg_l": solute_b, "uncertainty": 0.2},
    ]


def publish_parameter_set(client, headers, site_code, *, porosity=0.25, velocity=2.0, dispersion=5.0,
                          members=None, note="初次发布", force=False):
    """起草 -> 提交复核 -> 发布，返回发布版 JSON。"""
    draft = client.post(
        f"/api/hydro/sites/{site_code}/parameter-sets",
        headers=headers,
        json={"porosity": porosity, "velocity_m_day": velocity, "dispersion_m2_day": dispersion,
              "endmembers": members or endmembers(), "change_note": note},
    )
    assert draft.status_code == 201, draft.text
    draft_id = draft.json()["id"]
    submitted = client.post(f"/api/hydro/parameter-sets/{draft_id}/submit", headers=headers)
    assert submitted.status_code == 200, submitted.text
    published = client.post(
        f"/api/hydro/parameter-sets/{draft_id}/publish?force=true" if force
        else f"/api/hydro/parameter-sets/{draft_id}/publish",
        headers=headers, json={"note": note},
    )
    assert published.status_code == 200, published.text
    return published.json()


def test_mixture_inversion_and_transport(client, admin):
    headers = admin["headers"]
    well = create_well(client, headers)
    published = publish_parameter_set(client, headers, well["code"])
    sample = client.post(
        f"/api/hydro/wells/{well['id']}/samples",
        headers=headers,
        json={"sample_code": "S-001", "sampled_at": "2026-09-24T08:00:00+00:00",
              "isotope_d18o": -7.5, "isotope_d2h": -52.5, "solute_mg_l": 30,
              "detection_limit": 0.1, "measurement_error": 0.05},
    ).json()
    task = client.post(
        f"/api/hydro/samples/{sample['id']}/inversions",
        headers=headers,
        json={"parameter_set_id": published["id"], "max_iterations": 1000,
              "tolerance": 1e-10, "model_version": "mix-test"},
    )
    assert task.status_code == 202, task.text
    task_body = task.json()
    assert task_body["parameter_set_id"] == published["id"]
    assert task_body["parameter_revision"] == 1
    assert task_body["parameter_hash"] == published["content_hash"]
    done = client.post(f"/api/hydro/inversions/{task_body['id']}/run?worker_id=test", headers=headers)
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "done"

    transport = client.post(
        f"/api/hydro/wells/{well['id']}/transport",
        headers=headers,
        json={"parameter_set_id": published["id"], "source_concentration": 100, "distance_m": 100,
              "decay_per_day": 0.01, "duration_days": 100, "step_days": 5, "model_version": "ade-test"},
    )
    assert transport.status_code == 201, transport.text
    body = transport.json()
    assert body["result_json"]
    import json
    effective_input = json.loads(body["input_json"])
    # 流速与弥散度取自参数集而非请求
    assert effective_input["velocity_m_day"] == 2.0
    assert effective_input["dispersion_m2_day"] == 5.0
    assert body["parameter_hash"] == published["content_hash"]


def test_missing_measurement_is_classified(client, admin):
    well = create_well(client, admin["headers"], "W-002")
    sample = client.post(
        f"/api/hydro/wells/{well['id']}/samples",
        headers=admin["headers"],
        json={"sample_code": "S-002", "sampled_at": "2026-09-24T08:00:00+00:00",
              "isotope_d18o": -7.5, "detection_limit": 0.1, "measurement_error": 0.05},
    )
    assert sample.status_code == 201
    assert sample.json()["quality_status"] == "incomplete"


def test_task_must_reference_published_set(client, admin):
    headers = admin["headers"]
    well = create_well(client, headers, "W-003")
    sample = client.post(
        f"/api/hydro/wells/{well['id']}/samples",
        headers=headers,
        json={"sample_code": "S-003", "sampled_at": "2026-09-24T08:00:00+00:00",
              "isotope_d18o": -7.5, "isotope_d2h": -52.5, "solute_mg_l": 30},
    ).json()
    draft = client.post(
        f"/api/hydro/sites/{well['code']}/parameter-sets",
        headers=headers,
        json={"porosity": 0.25, "velocity_m_day": 2.0, "dispersion_m2_day": 5.0,
              "endmembers": endmembers()},
    ).json()
    # 草稿版本不能被任务引用
    rejected = client.post(
        f"/api/hydro/samples/{sample['id']}/inversions",
        headers=headers, json={"parameter_set_id": draft["id"]},
    )
    assert rejected.status_code == 409, rejected.text
