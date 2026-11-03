from __future__ import annotations

import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from dotenv import dotenv_values, set_key
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from foniks_event_query.service import (
    SearchAlreadyRunning,
    SearchJob,
    SearchNotFound,
    SearchRequest,
    SearchService,
)

PACKAGE_DIRECTORY = Path(__file__).parent
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class DirectoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    directory: str = Field(min_length=1, max_length=32767)


def choose_directory() -> str | None:
    import tkinter
    from tkinter import filedialog

    try:
        root = tkinter.Tk()
    except tkinter.TclError as error:
        raise RuntimeError("Folder picker unavailable") from error
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        root.update_idletasks()
        root.lift()
        return filedialog.askdirectory(
            parent=root, title="Select notification log directory", mustexist=True
        ) or None
    except tkinter.TclError as error:
        raise RuntimeError("Folder picker unavailable") from error
    finally:
        root.destroy()


def create_app(
    log_directory: Path | None = None,
    service: SearchService | None = None,
    config_path: Path | None = None,
) -> FastAPI:
    settings_path = (config_path or Path.cwd() / ".env").resolve()
    configuration_lock = threading.Lock()
    picker_lock = threading.Lock()
    owned_services: list[SearchService] = []
    if service is None:
        configured_directory = (
            log_directory
            or dotenv_values(settings_path).get("LOG_DIRECTORY")
            or os.environ.get("LOG_DIRECTORY")
        )
        if configured_directory:
            service = SearchService(Path(configured_directory))
            owned_services.append(service)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        for owned_service in owned_services:
            owned_service.close()

    app = FastAPI(title="Føniks Event Query", lifespan=lifespan)
    app.state.search_service = service
    app.mount(
        "/static",
        StaticFiles(directory=PACKAGE_DIRECTORY / "static"),
        name="static",
    )

    @app.middleware("http")
    async def local_request_guard(request: Request, call_next):
        host = request.url.hostname
        client_host = request.client.host if request.client else None
        if host not in LOCAL_HOSTS or client_host not in LOCAL_HOSTS:
            return JSONResponse({"detail": "Local access only"}, status_code=403)
        origin = request.headers.get("origin")
        if origin is not None:
            parsed_origin = urlsplit(origin)
            if (
                parsed_origin.scheme != "http"
                or parsed_origin.hostname not in LOCAL_HOSTS
                or parsed_origin.netloc != request.headers.get("host")
            ):
                return JSONResponse(
                    {"detail": "Same-origin requests only"}, status_code=403
                )
        elif request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            return JSONResponse({"detail": "Origin header required"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    def get_service() -> SearchService:
        configured_service = app.state.search_service
        if configured_service is None:
            raise HTTPException(
                status_code=503,
                detail="Choose and save a log directory before searching.",
            )
        return configured_service

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(PACKAGE_DIRECTORY / "templates" / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, bool]:
        configured_service = app.state.search_service
        directory_available = (
            configured_service is not None
            and configured_service.log_directory.is_dir()
        )
        return {
            "configured": configured_service is not None,
            "available": directory_available,
        }

    @app.get("/api/settings")
    def settings() -> dict[str, str | None]:
        configured_service = app.state.search_service
        return {
            "directory": (
                str(configured_service.log_directory)
                if configured_service is not None else None
            )
        }

    @app.post("/api/settings/browse")
    def browse_directory() -> dict[str, str | None]:
        if not picker_lock.acquire(blocking=False):
            raise HTTPException(
                status_code=409, detail="A folder picker is already open."
            )
        try:
            try:
                return {"directory": choose_directory()}
            except (ImportError, RuntimeError) as error:
                raise HTTPException(
                    status_code=503,
                    detail="Folder picker unavailable. Paste a directory path instead.",
                ) from error
        finally:
            picker_lock.release()

    @app.put("/api/settings")
    def save_directory(request: DirectoryRequest) -> dict[str, str]:
        value = request.directory.strip()
        if not value or "\0" in value:
            raise HTTPException(status_code=422, detail="Enter a directory path.")
        directory = Path(value)
        if not directory.is_absolute():
            raise HTTPException(
                status_code=422, detail="Enter an absolute directory path."
            )
        with configuration_lock:
            configured_service = app.state.search_service
            if configured_service is not None and configured_service.is_running():
                raise HTTPException(
                    status_code=409,
                    detail="Finish or cancel the search before changing directory.",
                )
            try:
                with os.scandir(directory):
                    pass
            except OSError as error:
                raise HTTPException(
                    status_code=422,
                    detail=f"The directory could not be read: {error.strerror}",
                ) from error
            try:
                set_key(str(settings_path), "LOG_DIRECTORY", str(directory))
            except OSError as error:
                raise HTTPException(
                    status_code=500,
                    detail="The directory could not be saved to the local .env file.",
                ) from error
            if configured_service is None:
                configured_service = SearchService(directory)
                owned_services.append(configured_service)
                app.state.search_service = configured_service
            else:
                configured_service.log_directory = directory
            return {"directory": str(directory)}

    @app.post("/api/search")
    def start_search(search_request: SearchRequest) -> dict[str, object]:
        try:
            with configuration_lock:
                job = get_service().start(search_request)
        except SearchAlreadyRunning as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return job_summary(job)

    @app.get("/api/search/{job_id}")
    async def search_status(
        job_id: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=25, ge=1, le=50),
    ) -> dict[str, object]:
        configured_service = get_service()
        try:
            job = configured_service.get(job_id)
        except SearchNotFound as error:
            raise HTTPException(status_code=404, detail="Search not found") from error
        summary = job_summary(job)
        summary["results"] = configured_service.page(job_id, offset, limit)
        summary["offset"] = offset
        summary["limit"] = limit
        return summary

    @app.post("/api/search/{job_id}/cancel")
    async def cancel_search(job_id: str) -> dict[str, object]:
        try:
            job = get_service().cancel(job_id)
        except SearchNotFound as error:
            raise HTTPException(status_code=404, detail="Search not found") from error
        return job_summary(job)

    return app


def job_summary(job: SearchJob) -> dict[str, object]:
    with job.lock:
        return {
            "id": job.job_id,
            "status": job.status,
            "field": job.request.field,
            "files_total": job.files_total,
            "files_scanned": job.files_scanned,
            "current_file": job.current_file,
            "lines_scanned": job.lines_scanned,
            "matches": job.matches,
            "warnings": list(job.warnings),
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        }


def run() -> None:
    uvicorn.run(create_app(), host="127.0.0.1", port=8000)


app = create_app()