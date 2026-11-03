import json
from pathlib import Path

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from foniks_event_query.app import create_app
from foniks_event_query.reader import LOG_BASENAME
from foniks_event_query.service import SearchService


def write_matching_log(path: Path) -> None:
    payload = {"eventId": "event-123", "description": "valid JSON payload"}
    message = f"Got message: id : job : {json.dumps(payload)}"
    path.write_text(
        json.dumps(
            {
                "Timestamp": "2026-10-02T13:22:58+02:00",
                "MessageTemplate": message,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_search_endpoint_returns_payload_separate_from_timestamp(
    tmp_path: Path,
) -> None:
    write_matching_log(tmp_path / f"{LOG_BASENAME}.log")
    service = SearchService(tmp_path)
    app = create_app(service=service)
    try:
        with TestClient(
            app,
            base_url="http://127.0.0.1",
            client=("127.0.0.1", 50000),
        ) as client:
            started = client.post(
                "/api/search",
                headers={"Origin": "http://127.0.0.1"},
                json={"field": "eventId", "value": "event-123"},
            )
            assert started.status_code == 200
            job_id = started.json()["id"]

            for _ in range(100):
                response = client.get(f"/api/search/{job_id}")
                if response.json()["status"] == "completed":
                    break
            result = response.json()["results"][0]

        assert result["timestamp"] == "2026-10-02T13:22:58+02:00"
        assert json.loads(result["payload_json"]) == {
            "eventId": "event-123",
            "description": "valid JSON payload",
        }
        assert "timestamp" not in json.loads(result["payload_json"])
    finally:
        service.close()


def test_search_rejects_cross_origin_and_arbitrary_log_paths(tmp_path: Path) -> None:
    service = SearchService(tmp_path)
    app = create_app(service=service)
    try:
        with TestClient(
            app,
            base_url="http://127.0.0.1",
            client=("127.0.0.1", 50000),
        ) as client:
            cross_origin = client.post(
                "/api/search",
                headers={"Origin": "http://attacker.example"},
                json={"field": "eventId", "value": "event-123"},
            )
            arbitrary_path = client.post(
                "/api/search",
                headers={"Origin": "http://127.0.0.1"},
                json={
                    "field": "eventId",
                    "value": "event-123",
                    "log_directory": "C:\\private",
                },
            )

        assert cross_origin.status_code == 403
        assert arbitrary_path.status_code == 422
    finally:
        service.close()


def test_app_uses_log_directory_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOG_DIRECTORY", str(tmp_path))
    app = create_app(config_path=tmp_path / ".env")

    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
    ) as client:
        health = client.get("/api/health")

    assert health.json() == {"configured": True, "available": True}


def local_client(app) -> TestClient:
    return TestClient(
        app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000),
        headers={"Origin": "http://127.0.0.1"},
    )


def test_directory_save_persists_and_searches_new_folder(tmp_path: Path) -> None:
    logs = tmp_path / "logs with spaces"
    logs.mkdir()
    write_matching_log(logs / f"{LOG_BASENAME}.log")
    config = tmp_path / ".env"
    config.write_text("OTHER_SETTING=retained\n", encoding="utf-8")
    with local_client(create_app(log_directory=tmp_path, config_path=config)) as client:
        saved = client.put("/api/settings", json={"directory": str(logs)})
        assert saved.status_code == 200
        assert client.get("/api/settings").json() == {"directory": str(logs)}
        job_id = client.post(
            "/api/search", json={"field": "eventId", "value": "event-123"}
        ).json()["id"]
        service = client.app.state.search_service
        service.get(job_id).worker.join(timeout=5)
        assert client.get(f"/api/search/{job_id}").json()["matches"] == 1
        client.put("/api/settings", json={"directory": str(tmp_path)})
        assert client.get(f"/api/search/{job_id}").json()["matches"] == 1
    assert dotenv_values(config)["OTHER_SETTING"] == "retained"
    assert dotenv_values(config)["LOG_DIRECTORY"] == str(tmp_path)
    with local_client(create_app(config_path=config)) as client:
        assert client.get("/api/settings").json() == {"directory": str(tmp_path)}


@pytest.mark.parametrize("value", ["", "   ", "relative", "missing", "file", "\0"])
def test_invalid_directory_does_not_change_settings(tmp_path: Path, value: str) -> None:
    config = tmp_path / ".env"
    config.write_text("OTHER_SETTING=retained\n", encoding="utf-8")
    if value == "missing":
        value = str(tmp_path / "missing")
    elif value == "file":
        value = str(config)
    with local_client(create_app(log_directory=tmp_path, config_path=config)) as client:
        assert client.put("/api/settings", json={"directory": value}).status_code == 422
        assert client.get("/api/settings").json() == {"directory": str(tmp_path)}
    assert config.read_text(encoding="utf-8") == "OTHER_SETTING=retained\n"


def test_settings_reject_cross_origin(tmp_path: Path) -> None:
    with local_client(
        create_app(log_directory=tmp_path, config_path=tmp_path / ".env")
    ) as client:
        for url in ["/api/settings", "/api/settings/browse"]:
            response = client.request(
                "PUT" if url == "/api/settings" else "POST", url,
                headers={"Origin": "http://attacker.example"},
                json={"directory": str(tmp_path)},
            )
            assert response.status_code == 403


def test_directory_change_blocked_during_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / ".env"
    app = create_app(log_directory=tmp_path, config_path=config)
    monkeypatch.setattr(app.state.search_service, "is_running", lambda: True)
    with local_client(app) as client:
        assert client.put(
            "/api/settings", json={"directory": str(tmp_path)}
        ).status_code == 409
    assert not config.exists()


def test_save_failure_keeps_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_save(*args):
        raise PermissionError("read-only configuration")

    monkeypatch.setattr("foniks_event_query.app.set_key", fail_save)
    logs = tmp_path / "new"
    logs.mkdir()
    with local_client(
        create_app(log_directory=tmp_path, config_path=tmp_path / ".env")
    ) as client:
        assert client.put(
            "/api/settings", json={"directory": str(logs)}
        ).status_code == 500
        assert client.get("/api/settings").json() == {"directory": str(tmp_path)}


@pytest.mark.parametrize("selection", [None, r"C:\logs"])
def test_browse_returns_selection_without_saving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str | None
) -> None:
    monkeypatch.setattr("foniks_event_query.app.choose_directory", lambda: selection)
    config = tmp_path / ".env"
    with local_client(create_app(log_directory=tmp_path, config_path=config)) as client:
        assert client.post("/api/settings/browse").json() == {"directory": selection}
        assert client.get("/api/settings").json() == {"directory": str(tmp_path)}
    assert not config.exists()


def test_browse_unavailable_reports_paste_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable():
        raise RuntimeError("No desktop")

    monkeypatch.setattr("foniks_event_query.app.choose_directory", unavailable)
    with local_client(
        create_app(log_directory=tmp_path, config_path=tmp_path / ".env")
    ) as client:
        response = client.post("/api/settings/browse")
        assert response.status_code == 503
        assert "Paste" in response.json()["detail"]


def test_save_initializes_unconfigured_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LOG_DIRECTORY", raising=False)
    config = tmp_path / ".env"
    with local_client(create_app(config_path=config)) as client:
        assert client.get("/api/settings").json() == {"directory": None}
        assert client.put(
            "/api/settings", json={"directory": str(tmp_path)}
        ).status_code == 200
        assert client.get("/api/health").json() == {
            "configured": True, "available": True
        }
    assert dotenv_values(config)["LOG_DIRECTORY"] == str(tmp_path)