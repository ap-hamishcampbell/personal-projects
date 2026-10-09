from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event

LOG_BASENAME = "MQH-FOENIKS-NOTIFICATION"
LOG_NAME_PATTERN = re.compile(
    rf"^{re.escape(LOG_BASENAME)}(?:\.(\d+))?\.log$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EventRecord:
    timestamp: str
    payload: dict[str, object]
    source_file: str
    line_number: int


class LogRecordError(ValueError):
    pass


def discover_log_files(directory: Path, include_rotations: bool = True) -> list[Path]:
    files: list[tuple[int, Path]] = []
    for path in directory.iterdir():
        match = LOG_NAME_PATTERN.fullmatch(path.name)
        if not match or not path.is_file():
            continue
        rotation = match.group(1)
        if rotation is None:
            files.append((0, path))
        elif include_rotations:
            files.append((int(rotation), path))
    return [path for _, path in sorted(files, key=lambda item: item[0])]


def extract_payload(message_template: object) -> dict[str, object] | None:
    if not isinstance(message_template, str):
        return None
    prefix, separator, message = message_template.partition(": ")
    if not separator or prefix != "Got message":
        return None
    parts = message.split(" : ", 2)
    if len(parts) != 3 or not parts[2].lstrip().startswith("{"):
        return None
    try:
        payload = json.loads(parts[2])
    except json.JSONDecodeError as error:
        raise LogRecordError(f"Malformed embedded payload: {error.msg}") from error
    if not isinstance(payload, dict):
        raise LogRecordError("Embedded payload must be a JSON object")
    return payload


def iter_event_records(
    path: Path,
    stop_event: Event | None = None,
    on_line: Callable[[], None] | None = None,
) -> Iterator[EventRecord]:
    with path.open("r", encoding="utf-8") as log_file:
        for line_number, line in enumerate(log_file, start=1):
            if stop_event is not None and stop_event.is_set():
                return
            if on_line is not None:
                on_line()
            if not line.strip():
                continue
            try:
                outer_record = json.loads(line)
            except json.JSONDecodeError as error:
                raise LogRecordError(
                    f"{path.name}:{line_number}: malformed log JSON: {error.msg}"
                ) from error
            if not isinstance(outer_record, dict):
                raise LogRecordError(
                    f"{path.name}:{line_number}: log entry must be a JSON object"
                )
            payload = extract_payload(outer_record.get("MessageTemplate"))
            if payload is None:
                continue
            timestamp = outer_record.get("Timestamp")
            if not isinstance(timestamp, str):
                raise LogRecordError(
                    f"{path.name}:{line_number}: event has no string Timestamp"
                )
            try:
                datetime.fromisoformat(timestamp)
            except ValueError as error:
                raise LogRecordError(
                    f"{path.name}:{line_number}: invalid event Timestamp"
                ) from error
            yield EventRecord(
                timestamp=timestamp,
                payload=payload,
                source_file=path.name,
                line_number=line_number,
            )