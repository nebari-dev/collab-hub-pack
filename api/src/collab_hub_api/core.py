from collections.abc import Callable
from contextlib import asynccontextmanager, nullcontext

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .config import (
    BaseConfig,
    build_active_frame_store,
    build_frames_store,
    build_group_store,
    build_history_store,
    build_task_store,
    build_usage_store,
    build_user_directory_client,
)
from .frames.auth import current_auth_context, get_auth_context
from .frames.mcp_server import create_mcp_server
from .frames.observability import RequestObservabilityMiddleware, configure_logging, metrics_response
from .frames.store import ConcurrentFrameUpdateError
from .routers import connectors, frame_groups, frames, tasks, usage, user_directory


def _api_path(path: str) -> bool:
    return path.startswith(
        (
            "/v1/frames",
            "/v1/active-frames",
            "/v1/frame-groups",
            "/v1/user-directory",
            "/v1/usage",
            "/frames",
            "/active-frames",
            "/frame-groups",
            "/usage",
            "/v1/tasks",
            "/v1/task-devices",
            "/v1/task-notifications",
            "/v1/task-runs",
        )
    )


class McpAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI, authenticate: Callable[[Request], object]):
        super().__init__(app)
        self.authenticate = authenticate

    async def dispatch(self, request: Request, call_next):
        token = None
        try:
            auth_context = self.authenticate(request)
            token = current_auth_context.set(auth_context)
        except HTTPException as exc:
            return Response(status_code=exc.status_code, content=str(exc.detail))
        try:
            return await call_next(request)
        finally:
            if token is not None:
                current_auth_context.reset(token)


def make_app(config: BaseConfig) -> FastAPI:
    configure_logging()
    frames_store = build_frames_store(config)
    active_frame_store = build_active_frame_store(config)
    history_store = build_history_store(config)
    group_store = build_group_store(config)
    user_directory_client = build_user_directory_client(config)
    usage_store = build_usage_store(config)
    task_store = build_task_store(config)
    mcp = create_mcp_server(frames_store, active_store=active_frame_store)
    mcp_app = mcp.streamable_http_app()
    # MCP traffic authenticates through the same get_auth_context, which
    # records seen users off the owning app's state — the mounted MCP app has
    # its own state object, so give it the store too.
    mcp_app.state.usage_store = usage_store

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        mcp_lifespan = mcp.session_manager.run() if config.frames.mcp_session_manager_enabled else nullcontext()
        async with mcp_lifespan:
            app.state.frames_store = frames_store
            app.state.active_frame_store = active_frame_store
            app.state.history_store = history_store
            app.state.group_store = group_store
            app.state.user_directory_client = user_directory_client
            app.state.usage_store = usage_store
            app.state.task_store = task_store
            app.state.mcp_server = mcp
            app.state.connectors_config = config.connectors
            try:
                yield
            finally:
                user_directory_client.close()

    app = FastAPI(
        root_path=config.server.root_path,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(RequestObservabilityMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.security.cors.allowed_origins,
        allow_headers=config.security.cors.allowed_headers,
        allow_credentials=config.security.cors.allow_credentials,
        allow_methods=["*"],
    )

    @app.get("/", include_in_schema=False)
    async def home() -> HTMLResponse:
        # The hub landing-page tile points at this origin, so the root serves
        # a small directory of the browser-facing pages instead of jumping
        # straight to the API docs.
        return HTMLResponse(
            """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Collab Hub Frames</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
         Helvetica, Arial, sans-serif; margin: 4rem auto; max-width: 40rem;
         padding: 0 1rem; color: #1a1a2e; }
  h1 { font-size: 1.5rem; }
  ul { line-height: 2; }
  a { color: #3452d9; }
</style>
</head>
<body>
<h1>Collab Hub Frames</h1>
<p>Hub-side intelligence services for Frames and chat.</p>
<ul>
  <li><a href="./usage">Hub usage dashboard</a> — activity across this workspace</li>
  <li><a href="./docs">API documentation</a></li>
</ul>
</body>
</html>"""
        )

    @app.get("/docs", include_in_schema=False)
    async def protected_docs(_auth=Depends(get_auth_context)) -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=f"{app.root_path}/openapi.json",
            title=f"{app.title} - Swagger UI",
        )

    @app.get("/redoc", include_in_schema=False)
    async def protected_redoc(_auth=Depends(get_auth_context)) -> HTMLResponse:
        return get_redoc_html(
            openapi_url=f"{app.root_path}/openapi.json",
            title=f"{app.title} - ReDoc",
        )

    @app.get("/openapi.json", include_in_schema=False)
    async def protected_openapi(_auth=Depends(get_auth_context)) -> JSONResponse:
        return JSONResponse(app.openapi())

    @app.get("/health")
    async def health() -> Response:
        return Response(b"", status_code=status.HTTP_200_OK)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return metrics_response()

    @app.exception_handler(HTTPException)
    async def frames_http_exception_handler(request: Request, exc: HTTPException):
        if not _api_path(request.url.path):
            return await http_exception_handler(request, exc)
        code = {
            status.HTTP_401_UNAUTHORIZED: "unauthorized",
            status.HTTP_403_FORBIDDEN: "forbidden",
            status.HTTP_404_NOT_FOUND: "not_found",
        }.get(exc.status_code, "http_error")
        return frames.error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def frames_validation_exception_handler(request: Request, exc: RequestValidationError):
        if not _api_path(request.url.path):
            return await request_validation_exception_handler(request, exc)
        return frames.error_response(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            "Request validation failed",
            details=jsonable_encoder(exc.errors()),
        )

    @app.exception_handler(ConcurrentFrameUpdateError)
    async def concurrent_frame_update_handler(_request: Request, exc: ConcurrentFrameUpdateError):
        return frames.error_response(
            status.HTTP_409_CONFLICT,
            "frame_update_conflict",
            str(exc),
        )

    frames.register_exception_handlers(app)
    frame_groups.register_exception_handlers(app)
    user_directory.register_exception_handlers(app)
    usage.register_exception_handlers(app)
    app.include_router(frames.router, prefix="/v1")
    app.include_router(user_directory.router, prefix="/v1")
    app.include_router(frames.router, include_in_schema=False)
    app.include_router(frame_groups.router, prefix="/v1")
    app.include_router(frame_groups.router, include_in_schema=False)
    app.include_router(connectors.router, prefix="/v1")
    app.include_router(usage.router, prefix="/v1")
    app.include_router(usage.router, include_in_schema=False)
    app.include_router(usage.page_router)
    app.include_router(tasks.router, prefix="/v1")
    app.include_router(tasks.devices_router, prefix="/v1")
    app.include_router(tasks.notifications_router, prefix="/v1")
    app.include_router(tasks.runs_router, prefix="/v1")

    mcp_app.add_middleware(McpAuthMiddleware, authenticate=get_auth_context)
    app.mount("/", mcp_app)

    return app
