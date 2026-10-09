from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from foniks_event_query.service import (
    SearchAlreadyRunning,
    SearchJob,
    SearchNotFound,
    SearchRequest,
    SearchService,
)

PACKAGE_DIRECTORY = Path(__file__).parent
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def create_app(
    log_directory: Path | None = None,
    service: SearchService | None = None,
) -> FastAPI:
    owns_service = service is None
    if service is None:
        configured_directory = log_directory or os.environ.get("LOG_DIRECTORY")
        if configured_directory:
            service = SearchService(Path(configured_directory))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_service and service is not None:
            service.close()

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
                detail="Set LOG_DIRECTORY in the local .env file before searching.",
            )
        return configured_service

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(PACKAGE_DIRECTORY / "templates" / "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, bool]:
        configured_service = app.state.search_service
        directory_available = (
            configured_service is not None
            and configured_service.log_directory.is_dir()
        )
        return {
            "configured": configured_service is not None,
            "available": directory_available,
        }

    @app.post("/api/search")
    async def start_search(search_request: SearchRequest) -> dict[str, object]:
        try:
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