from __future__ import annotations

import os
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path):
    os.environ["TOWNSHIP_DATABASE_PATH"] = str(tmp_path / "test.db")
    from app.database import close_connection
    close_connection()
    from app.main import app
    with TestClient(app) as test_client:
        yield test_client
    close_connection()


@pytest.fixture()
def admin(client: TestClient) -> dict:
    response = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "Admin!23456", "client_label": "tests"})
    assert response.status_code == 201, response.text
    login = client.post("/api/auth/login", json={"username": "admin", "password": "Admin!23456", "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"token": login.json()["token"], "headers": {"Authorization": f"Bearer {login.json()['token']}"}}


@pytest.fixture()
def hydro(client: TestClient, admin: dict):
    """地下水参数集测试辅助：按需创建受限账号，并提供一键走完发布流程的函数。"""
    users: dict[str, dict] = {}

    def make_user(username: str, permissions: list[str]) -> dict:
        if username in users:
            return users[username]
        role_code = f"hydro.{username}"
        role = client.post(
            "/api/roles",
            headers=admin["headers"],
            json={"code": role_code, "name": username, "permission_codes": permissions},
        )
        assert role.status_code == 201, role.text
        created = client.post(
            "/api/users",
            headers=admin["headers"],
            json={"username": username, "password": "Hydro!23456", "display_name": username, "role_codes": [role_code]},
        )
        assert created.status_code == 201, created.text
        login = client.post("/api/auth/login", json={"username": username, "password": "Hydro!23456", "client_label": "tests"})
        assert login.status_code == 200, login.text
        users[username] = {"Authorization": f"Bearer {login.json()['token']}"}
        return users[username]

    def author() -> dict:
        return make_user("hydro.author", ["hydro.read", "hydro.write", "hydro.params.write"])

    def reviewer() -> dict:
        return make_user("hydro.reviewer", ["hydro.read", "hydro.params.review"])

    def param_set_payload(**overrides) -> dict:
        payload = {
            "site_code": "SITE-NORTH",
            "code": "BASELINE",
            "porosity": 0.3,
            "velocity_m_day": 2.0,
            "dispersion_m2_day": 5.0,
            "decay_per_day": 0.01,
            "notes": "基线参数",
            "endmembers": [
                {"name": "山区降水", "isotope_d18o": -10, "isotope_d2h": -70, "solute_mg_l": 10, "uncertainty": 0.1},
                {"name": "河流渗漏", "isotope_d18o": -5, "isotope_d2h": -35, "solute_mg_l": 50, "uncertainty": 0.2},
            ],
        }
        payload.update(overrides)
        return payload

    def create_draft(**overrides) -> dict:
        created = client.post("/api/hydro/param-sets", headers=author(), json=param_set_payload(**overrides))
        assert created.status_code == 201, created.text
        return created.json()

    def publish_param_set(**overrides) -> dict:
        draft = create_draft(**overrides)
        param_set_id = draft["id"]
        submitted = client.post(f"/api/hydro/param-sets/{param_set_id}/submit", headers=author(), json={"comment": "提交复核"})
        assert submitted.status_code == 200, submitted.text
        approved = client.post(f"/api/hydro/param-sets/{param_set_id}/approve", headers=reviewer(), json={"comment": "复核通过"})
        assert approved.status_code == 200, approved.text
        published = client.post(
            f"/api/hydro/param-sets/{param_set_id}/publish",
            headers=admin["headers"],
            json={"expected_base_version_id": draft["base_version_id"]},
        )
        assert published.status_code == 200, published.text
        return published.json()

    return types.SimpleNamespace(
        make_user=make_user,
        author=author,
        reviewer=reviewer,
        param_set_payload=param_set_payload,
        create_draft=create_draft,
        publish_param_set=publish_param_set,
        admin_headers=admin["headers"],
    )
