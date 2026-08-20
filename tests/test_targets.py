"""Targets CRUD against a real Postgres.

Validation gets its own tests on purpose: a malformed URL must come back as a 4xx and be
distinguishable in the HTTP metrics from a 5xx. "It returns an error" is not the same
claim as "it returns the right kind of error".
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.db


def make_target(**overrides):
    payload = {"name": "example", "url": "https://example.com", "interval_seconds": 60}
    payload.update(overrides)
    return payload


def test_ready_when_the_database_is_up(client):
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok", "migrations": "ok"},
    }


def test_create_and_read_back(client):
    created = client.post("/api/targets", json=make_target())
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "example"
    assert body["enabled"] is True

    fetched = client.get(f"/api/targets/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == body


def test_list_and_delete(client):
    first = client.post("/api/targets", json=make_target(name="one")).json()
    client.post("/api/targets", json=make_target(name="two"))

    assert len(client.get("/api/targets").json()) == 2

    assert client.delete(f"/api/targets/{first['id']}").status_code == 204
    assert client.get(f"/api/targets/{first['id']}").status_code == 404
    assert len(client.get("/api/targets").json()) == 1


def test_patch_toggles_enabled_without_losing_the_row(client):
    target = client.post("/api/targets", json=make_target()).json()

    patched = client.patch(f"/api/targets/{target['id']}", json={"enabled": False})
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False
    assert client.get(f"/api/targets/{target['id']}").status_code == 200


def test_status_counts_disabled_separately(client):
    enabled = client.post("/api/targets", json=make_target(name="on")).json()
    disabled = client.post("/api/targets", json=make_target(name="off")).json()
    client.patch(f"/api/targets/{disabled['id']}", json={"enabled": False})

    # Everything is unknown until PR 2 produces check results.
    assert client.get("/api/status").json() == {
        "up": 0,
        "down": 0,
        "unknown": 1,
        "disabled": 1,
    }
    assert enabled["enabled"] is True


@pytest.mark.parametrize(
    "url",
    ["ftp://example.com", "example.com", "https://", "javascript:alert(1)"],
)
def test_malformed_urls_are_rejected_as_4xx(client, url):
    response = client.post("/api/targets", json=make_target(url=url))
    assert 400 <= response.status_code < 500


def test_interval_below_the_minimum_is_rejected(client):
    response = client.post("/api/targets", json=make_target(interval_seconds=1))
    assert response.status_code == 400
    assert "at least" in response.json()["detail"]


def test_duplicate_name_is_a_conflict(client):
    client.post("/api/targets", json=make_target())
    response = client.post("/api/targets", json=make_target(url="https://other.example"))
    assert response.status_code == 409


def test_target_limit_is_enforced(clean_database, settings_env):
    settings_env(MAX_TARGETS=2)
    with TestClient(app) as client:
        assert client.post("/api/targets", json=make_target(name="a")).status_code == 201
        assert client.post("/api/targets", json=make_target(name="b")).status_code == 201

        response = client.post("/api/targets", json=make_target(name="c"))
        assert response.status_code == 409
        assert "limit" in response.json()["detail"]


def test_targets_gauge_tracks_the_table(client):
    def gauge_value():
        for line in client.get("/metrics").text.splitlines():
            if line.startswith("devops_lab_targets_total "):
                return float(line.split()[1])
        raise AssertionError("devops_lab_targets_total is not exposed")

    baseline = gauge_value()
    target = client.post("/api/targets", json=make_target()).json()
    assert gauge_value() == baseline + 1

    client.delete(f"/api/targets/{target['id']}")
    assert gauge_value() == baseline
