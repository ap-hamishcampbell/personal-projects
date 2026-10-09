# Føniks Event Query

A local browser tool for finding notification log events by `eventId`,
`commandId`, or `secondaryId`. It searches the configured current log and, when
selected, its numbered rotations. Matching payloads are displayed as formatted
JSON; timestamps and source locations remain outside the JSON and Copy JSON
copies only the payload.

## Requirements

- Python 3.12 or newer
- Windows access to the configured log directory

## Setup

From this directory in PowerShell:

```powershell
py -3.14 -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -e ".[dev]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Edit `.env` and set `LOG_DIRECTORY` to a folder the current Windows account can
read. The local `.env` file is ignored by Git. No credentials are stored by the
application.

## Run

```powershell
& ".\.venv\Scripts\python.exe" -m uvicorn foniks_event_query.app:app `
  --host 127.0.0.1 --port 8000 --env-file .env
```

Open <http://127.0.0.1:8000>. The server binds only to loopback. If port 8000 is
occupied, use another local port such as 8001.

To launch VS Code with a separate settings store and extension directory, run
`.\.vscode\open-isolated.ps1` from PowerShell. It installs Microsoft's Python,
Python Environments, Pylance, and Python Debugger extensions in that isolated
directory only. The project interpreter, tasks, and debug configuration are
already set to use this project's `.venv`.

## Search behavior

- Select one identifier field and enter its exact value. Matching is case
  sensitive and exact; every matching event occurrence is retained.
- Current log: `MQH-FOENIKS-NOTIFICATION.log`.
- Rotations: `MQH-FOENIKS-NOTIFICATION.<number>.log`, scanned by numeric suffix.
- Each input line is parsed as an outer JSON record. The payload JSON is decoded
  from the `MessageTemplate` value.
- Results are paginated, with each source timestamp and line location separate
  from the standalone payload JSON. Copy JSON excludes that metadata.
- Search results live in a temporary local SQLite database for the running
  session and are removed when the server exits or the job expires.
- A malformed record or unreadable file is reported as a warning; that file may
  have been only partially scanned. Results follow file/line order, not global
  timestamp order.

## Checks

```powershell
& ".\.venv\Scripts\python.exe" -m pytest
& ".\.venv\Scripts\python.exe" -m ruff check .
```