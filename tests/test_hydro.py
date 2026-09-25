from __future__ import annotations


def create_well(client, headers, code="W-001"):
    response = client.post(
        "/api/hydro/wells",
        headers=headers,
        json={"code": code, "name": "北部监测井", "latitude": 35.1, "longitude": 116.2, "aquifer": "浅层孔隙含水层", "screen_depth_m": 42},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_mixture_inversion_and_transport(client, admin, hydro):
    well = create_well(client, admin["headers"])
    param_set = hydro.publish_param_set()
    sample = client.post(
        f"/api/hydro/wells/{well['id']}/samples",
        headers=admin["headers"],
        json={"sample_code": "S-001", "sampled_at": "2026-09-24T08:00:00+00:00", "isotope_d18o": -7.5, "isotope_d2h": -52.5, "solute_mg_l": 30, "detection_limit": 0.1, "measurement_error": 0.05},
    ).json()
    task = client.post(
        f"/api/hydro/samples/{sample['id']}/inversions",
        headers=admin["headers"],
        json={"parameter_set_id": param_set["id"], "max_iterations": 1000, "tolerance": 1e-10, "model_version": "mix-test"},
    )
    assert task.status_code == 202, task.text
    assert task.json()["parameter_set_version"] == 1
    done = client.post(f"/api/hydro/inversions/{task.json()['id']}/run?worker_id=test", headers=admin["headers"])
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "done"
    transport = client.post(
        f"/api/hydro/wells/{well['id']}/transport",
        headers=admin["headers"],
        json={"parameter_set_id": param_set["id"], "source_concentration": 100, "distance_m": 100, "duration_days": 100, "step_days": 5, "model_version": "ade-test"},
    )
    assert transport.status_code == 201, transport.text
    assert transport.json()["result_json"]
    assert transport.json()["parameter_set_id"] == param_set["id"]


def test_missing_measurement_is_classified(client, admin):
    well = create_well(client, admin["headers"], "W-002")
    sample = client.post(
        f"/api/hydro/wells/{well['id']}/samples",
        headers=admin["headers"],
        json={"sample_code": "S-002", "sampled_at": "2026-09-24T08:00:00+00:00", "isotope_d18o": -7.5, "detection_limit": 0.1, "measurement_error": 0.05},
    )
    assert sample.status_code == 201
    assert sample.json()["quality_status"] == "incomplete"


def test_hydro_endpoints_require_authentication(client):
    assert client.post("/api/hydro/wells", json={"code": "W-100", "name": "井", "latitude": 35, "longitude": 116, "aquifer": "含水层", "screen_depth_m": 40}).status_code == 401
    assert client.get("/api/hydro/param-sets").status_code == 401
