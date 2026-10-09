import json
import threading
import time
from pathlib import Path

import pytest

from foniks_event_query.reader import LOG_BASENAME
from foniks_event_query.service import (
    SearchAlreadyRunning,
    SearchRequest,
    SearchService,
)


def make_log_line(timestamp: str, payload: dict[str, str]) -> str:
    message = f"Got message: message-id : worker-job : {json.dumps(payload)}"
    return json.dumps({"Timestamp": timestamp, "MessageTemplate": message})


def wait_for_completion(service: SearchService, job_id: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if service.get(job_id).status not in {"queued", "running"}:
            return
        time.sleep(0.01)
    raise AssertionError("Search did not finish in time")


def test_search_matches_exact_field_and_paginates_json_results(
    tmp_path: Path,
) -> None:
    current = tmp_path / f"{LOG_BASENAME}.log"
    rotated = tmp_path / f"{LOG_BASENAME}.2.log"
    unrelated = tmp_path / "other.log"
    matching_payload = {"eventId": "event-123", "description": "quoted \"text\""}
    current.write_text(
        make_log_line("2026-10-02T13:22:58+02:00", matching_payload) + "\n",
        encoding="utf-8",
    )
    rotated.write_text(
        make_log_line("2026-10-01T13:22:58+02:00", {"eventId": "event-123"})
        + "\n"
        + make_log_line("2026-10-01T13:22:59+02:00", {"eventId": "event-1234"})
        + "\n",
        encoding="utf-8",
    )
    unrelated.write_text(
        make_log_line("2026-10-02T13:22:58+02:00", {"eventId": "event-123"})
        + "\n",
        encoding="utf-8",
    )
    service = SearchService(tmp_path)
    try:
        job = service.start(SearchRequest(field="eventId", value="event-123"))
        wait_for_completion(service, job.job_id)

        status = service.get(job.job_id)
        first_page = service.page(job.job_id, 0, 1)
        second_page = service.page(job.job_id, 1, 1)

        assert status.status == "completed"
        assert status.matches == 2
        assert len(first_page) == len(second_page) == 1
        assert first_page[0]["timestamp"] == "2026-10-02T13:22:58+02:00"
        assert first_page[0]["source_file"] == current.name
        assert json.loads(first_page[0]["payload_json"]) == matching_payload
        assert json.loads(second_page[0]["payload_json"]) == {
            "eventId": "event-123"
        }
    finally:
        service.close()


def test_only_one_search_runs_at_a_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def wait_for_search_files(directory: Path, include_rotations: bool) -> list[Path]:
        entered.set()
        release.wait(timeout=2)
        return []

    monkeypatch.setattr(
        "foniks_event_query.service.discover_log_files", wait_for_search_files
    )
    service = SearchService(tmp_path)
    try:
        job = service.start(SearchRequest(field="eventId", value="event-123"))
        assert entered.wait(timeout=2)
        with pytest.raises(SearchAlreadyRunning):
            service.start(SearchRequest(field="commandId", value="command-456"))
        release.set()
        wait_for_completion(service, job.job_id)
    finally:
        release.set()
        service.close()