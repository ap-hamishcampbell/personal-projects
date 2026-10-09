import json
from pathlib import Path

import pytest
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
    app = create_app()

    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
    ) as client:
        health = client.get("/api/health")

    assert health.json() == {"configured": True, "available": True}