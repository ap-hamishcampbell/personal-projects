from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from foniks_event_query.reader import (
    EventRecord,
    LogRecordError,
    discover_log_files,
    iter_event_records,
)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: Literal["eventId", "commandId", "secondaryId"]
    value: str = Field(min_length=1, max_length=512)
    include_rotations: bool = True

    def normalized_value(self) -> str:
        value = self.value.strip()
        if not value:
            raise ValueError("Enter an identifier to search for")
        return value


@dataclass
class SearchJob:
    job_id: str
    request: SearchRequest
    database_path: Path
    status: str = "queued"
    files_total: int = 0
    files_scanned: int = 0
    current_file: str | None = None
    lines_scanned: int = 0
    matches: int = 0
    warnings: list[str] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    worker: threading.Thread | None = None


class SearchAlreadyRunning(RuntimeError):
    pass


class SearchNotFound(KeyError):
    pass


class SearchService:
    def __init__(self, log_directory: Path, expiry_minutes: int = 30) -> None:
        self.log_directory = log_directory
        self.expiry = timedelta(minutes=expiry_minutes)
        self._jobs: dict[str, SearchJob] = {}
        self._lock = threading.Lock()
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="foniks-event-query-"
        )
        self._closed = False

    def start(self, request: SearchRequest) -> SearchJob:
        value = request.normalized_value()
        request = request.model_copy(update={"value": value})
        with self._lock:
            if self._closed:
                raise RuntimeError("Search service is closed")
            self._expire_jobs()
            if any(job.status in {"queued", "running"} for job in self._jobs.values()):
                raise SearchAlreadyRunning("A search is already running")
            job_id = uuid.uuid4().hex
            database_path = Path(self._temporary_directory.name) / f"{job_id}.sqlite"
            self._initialize_database(database_path)
            job = SearchJob(job_id, request, database_path)
            self._jobs[job_id] = job
            thread = threading.Thread(
                target=self._run_search,
                args=(job,),
                name=f"event-query-{job_id[:8]}",
                daemon=True,
            )
            job.worker = thread
            thread.start()
            return job

    def get(self, job_id: str) -> SearchJob:
        with self._lock:
            self._expire_jobs()
            job = self._jobs.get(job_id)
            if job is None:
                raise SearchNotFound(job_id)
            return job

    def cancel(self, job_id: str) -> SearchJob:
        job = self.get(job_id)
        with job.lock:
            if job.status in {"queued", "running"}:
                job.stop_event.set()
        return job

    def page(self, job_id: str, offset: int, limit: int) -> list[dict[str, object]]:
        job = self.get(job_id)
        with job.lock:
            if job.status in {"queued", "running", "failed"}:
                return []
            connection = sqlite3.connect(job.database_path, timeout=2)
            try:
                rows = connection.execute(
                    "SELECT timestamp, source_file, line_number, payload_json "
                    "FROM results ORDER BY result_number LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            finally:
                connection.close()
        return [
            {
                "timestamp": timestamp,
                "source_file": source_file,
                "line_number": line_number,
                "payload_json": payload_json,
            }
            for timestamp, source_file, line_number, payload_json in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._closed = True
            jobs = list(self._jobs.values())
        for job in jobs:
            job.stop_event.set()
        for job in jobs:
            if job.worker is not None and job.worker is not threading.current_thread():
                job.worker.join(timeout=5)
        self._temporary_directory.cleanup()

    @staticmethod
    def _initialize_database(path: Path) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "CREATE TABLE results ("
                "result_number INTEGER PRIMARY KEY AUTOINCREMENT, "
                "timestamp TEXT NOT NULL, "
                "source_file TEXT NOT NULL, "
                "line_number INTEGER NOT NULL, "
                "payload_json TEXT NOT NULL)"
            )
            connection.commit()
        finally:
            connection.close()

    def _expire_jobs(self) -> None:
        expired_before = datetime.now(UTC) - self.expiry
        for job_id, job in list(self._jobs.items()):
            if job.finished_at is not None and job.finished_at < expired_before:
                job.database_path.unlink(missing_ok=True)
                del self._jobs[job_id]

    def _run_search(self, job: SearchJob) -> None:
        with job.lock:
            job.status = "running"
            job.started_at = datetime.now(UTC)
        try:
            files = discover_log_files(
                self.log_directory, job.request.include_rotations
            )
            job.files_total = len(files)
            for path in files:
                if job.stop_event.is_set():
                    break
                with job.lock:
                    job.current_file = path.name
                try:
                    for record in iter_event_records(
                        path,
                        stop_event=job.stop_event,
                        on_line=lambda: self._increment_lines(job),
                    ):
                        if record.payload.get(job.request.field) == job.request.value:
                            self._store_match(job, record)
                except (OSError, UnicodeError, LogRecordError) as error:
                    self._add_warning(job, self._warning_for(path, error))
                finally:
                    with job.lock:
                        job.files_scanned += 1
            with job.lock:
                job.current_file = None
                job.status = "cancelled" if job.stop_event.is_set() else "completed"
        except OSError:
            with job.lock:
                job.warnings.append("The configured log directory could not be read.")
                job.status = "failed"
        except Exception:
            with job.lock:
                job.warnings.append("The search could not be completed.")
                job.status = "failed"
        finally:
            with job.lock:
                job.current_file = None
                job.finished_at = datetime.now(UTC)

    @staticmethod
    def _increment_lines(job: SearchJob) -> None:
        with job.lock:
            job.lines_scanned += 1

    @staticmethod
    def _add_warning(job: SearchJob, warning: str) -> None:
        with job.lock:
            job.warnings.append(warning)

    @staticmethod
    def _warning_for(path: Path, error: Exception) -> str:
        if isinstance(error, LogRecordError):
            return f"{path.name}: a malformed record stopped scanning this file."
        return f"{path.name}: the file could not be read completely."

    @staticmethod
    def _store_match(job: SearchJob, record: EventRecord) -> None:
        payload_json = json.dumps(record.payload, ensure_ascii=False, indent=2)
        with job.lock:
            connection = sqlite3.connect(job.database_path, timeout=2)
            try:
                connection.execute(
                    "INSERT INTO results "
                    "(timestamp, source_file, line_number, payload_json) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        record.timestamp,
                        record.source_file,
                        record.line_number,
                        payload_json,
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            job.matches += 1