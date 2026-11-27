"""Same-origin HTTP and static frontend."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from gpu_console import storage
from gpu_console.api import auth, automation_rules, catalog, dashboard, mcp_keys, resources, tasks
from gpu_console.application.tasks import TaskTrashConflict
from gpu_console.config import Config


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.from_env()
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        storage.initialize(config.db)
        yield

    app = FastAPI(
        lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, title="GPU 实验管理台"
    )
    app.state.config = config
    app.include_router(auth.router)
    app.include_router(resources.router)
    app.include_router(catalog.router)
    app.include_router(dashboard.router)
    app.include_router(tasks.router)
    app.include_router(automation_rules.router)
    app.include_router(mcp_keys.router)

    @app.exception_handler(TaskTrashConflict)
    async def task_conflict(_request: Request, exc: TaskTrashConflict):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid_value(_request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(KeyError)
    async def missing_resource(_request: Request, exc: KeyError):
        detail = exc.args[0] if exc.args else "记录不存在"
        return JSONResponse({"detail": detail}, status_code=404)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(_request: Request, exc: RequestValidationError):
        fields = [".".join(str(part) for part in error["loc"][1:]) for error in exc.errors()]
        return JSONResponse({"detail": f"请检查输入字段：{'、'.join(fields)}"}, status_code=422)

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api/") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    def health() -> dict:
        with storage.connect(config.db) as conn:
            conn.execute("SELECT 1 FROM settings LIMIT 1").fetchone()
        return {"status": "ok"}

    if (config.static_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=config.static_dir / "assets"), name="assets")

    @app.get("/")
    def index():
        path = config.static_dir / "index.html"
        if not path.is_file():
            raise HTTPException(503, "前端尚未构建")
        return FileResponse(path)

    return app
