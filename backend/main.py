"""
MA Trader - FastAPI Main Entry Point
"""
import os
import sys
from pathlib import Path

# Ensure backend and project root are on sys.path
_BACKEND_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _BACKEND_DIR.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from database import create_tables
from config import ConfigManager
from auth import PASSWORD_CHANGE_ALLOWED_PATHS, validate_token, get_token_from_request
from binance_ws import BinanceWSStreamer
from routers import auth as auth_router
from routers import settings as settings_router
from routers import trading as trading_router
from routers import backtest as backtest_router
from routers import stats as stats_router

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


class ConnectionManager:
    """Manages WebSocket connections with optional per-client env ('live' | 'paper') filtering."""

    def __init__(self):
        self.active_connections: list[WebSocket] = []
        self._client_envs: dict[WebSocket, str] = {}

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"WebSocket client connected. Total: {len(self.active_connections)}")

    def register_accepted(self, websocket: WebSocket, env: str = "all"):
        if websocket not in self.active_connections:
            self.active_connections.append(websocket)
        norm_env = env if env in ("live", "paper") else "all"
        self._client_envs[websocket] = norm_env
        logger.info(f"WebSocket client authenticated (env={norm_env}). Total: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        self._client_envs.pop(websocket, None)
        logger.info(f"WebSocket client disconnected. Total: {len(self.active_connections)}")

    def _matches_env(self, websocket: WebSocket, msg_env: Optional[str]) -> bool:
        if not msg_env:
            return True
        client_env = self._client_envs.get(websocket, "all")
        return client_env == "all" or client_env == msg_env

    async def broadcast(self, message: dict):
        """Broadcast a JSON message to matching connected clients (market data goes to all)."""
        msg_type = message.get("type") if isinstance(message, dict) else None
        msg_env: Optional[str] = None
        if isinstance(message, dict) and msg_type not in {"ticker", "ma_update", "bot_status", "pong"}:
            if message.get("mode") in ("live", "paper"):
                msg_env = message["mode"]
            elif "is_paper" in message and message["is_paper"] is not None:
                msg_env = "paper" if message["is_paper"] else "live"
            elif msg_type in {"trade_opened", "trade_closed"} and isinstance(message.get("trade"), dict):
                tr = message["trade"]
                if "is_paper" in tr and tr["is_paper"] is not None:
                    msg_env = "paper" if tr["is_paper"] else "live"

        text = json.dumps(message, ensure_ascii=False, default=str)
        dead = []
        for connection in list(self.active_connections):
            if not self._matches_env(connection, msg_env):
                continue
            try:
                await connection.send_text(text)
            except Exception:
                dead.append(connection)
        for conn in dead:
            self.disconnect(conn)

    async def send_personal(self, websocket: WebSocket, message: dict):
        text = json.dumps(message, ensure_ascii=False, default=str)
        try:
            await websocket.send_text(text)
        except Exception:
            self.disconnect(websocket)


ws_manager = ConnectionManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    logger.info("Starting MA Trader...")
    create_tables()

    app.state.ws_manager = ws_manager
    app.state.config = ConfigManager()
    shared_ws = BinanceWSStreamer(proxy=app.state.config.get("http_proxy"))
    shared_ws.register_callback(lambda msg: asyncio.create_task(ws_manager.broadcast(msg)))
    app.state.ws_streamer = shared_ws
    app.state.bots = {
        "paper:balanced": None,
        "paper:breakout": None,
        "paper:classic": None,
        "paper:ribbon": None,
        "live": None,
    }
    app.state.shared_paper_feed = None
    app.state.bot_lock = asyncio.Lock()

    logger.info("MA Trader ready.")
    yield
    bots = getattr(app.state, "bots", {}) or {}
    for bot in bots.values():
        if bot is not None and getattr(bot, "running", False):
            try:
                await bot.stop()
            except Exception as exc:
                logger.warning(f"Error stopping bot on shutdown: {exc}")
    try:
        await shared_ws.stop()
    except Exception as exc:
        logger.warning(f"Error stopping shared ws_streamer on shutdown: {exc}")
    logger.info("MA Trader shut down.")


_ENABLE_DOCS = os.environ.get("ENABLE_DOCS", "").strip().lower() in {"1", "true", "yes"}
app = FastAPI(
    title="MA Trader",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _ENABLE_DOCS else None,
    redoc_url="/redoc" if _ENABLE_DOCS else None,
    openapi_url="/openapi.json" if _ENABLE_DOCS else None,
)

# Restrict CORS to explicitly configured origins (same-origin requests don't need CORS).
_raw_origins = os.environ.get("CORS_ORIGINS", "").strip()
_cors_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()] if _raw_origins else []
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


@app.middleware("http")
async def add_security_and_cache_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "font-src 'self' data: https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "img-src 'self' data: blob:; "
        "connect-src 'self' ws: wss: https://*.binance.com https://*.binance.vision https://cdn.jsdelivr.net;",
    )
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css")) or "/js/" in path or "/css/" in path:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


PUBLIC_API_PATHS = {"/api/auth/login", "/health"}


@app.middleware("http")
async def api_auth_middleware(request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in PUBLIC_API_PATHS:
        token = get_token_from_request(request, allow_query=False)
        user = validate_token(token)
        if not user:
            return JSONResponse(
                status_code=401,
                content={"detail": "未授权或登录已过期，请重新登录", "code": "UNAUTHORIZED"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        if getattr(user, "must_change_password", False) and path not in PASSWORD_CHANGE_ALLOWED_PATHS:
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "首次登录或仍在使用默认密码，请先修改管理员密码",
                    "code": "PASSWORD_CHANGE_REQUIRED",
                },
            )
    return await call_next(request)


# Static files (frontend)
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
    if (FRONTEND_DIR / "css").exists():
        app.mount("/css", StaticFiles(directory=str(FRONTEND_DIR / "css")), name="css")
    if (FRONTEND_DIR / "js").exists():
        app.mount("/js", StaticFiles(directory=str(FRONTEND_DIR / "js")), name="js")

# Routers
app.include_router(auth_router.router, prefix="/api")
app.include_router(settings_router.router, prefix="/api")
app.include_router(trading_router.router, prefix="/api")
app.include_router(backtest_router.router, prefix="/api")
app.include_router(stats_router.router, prefix="/api")


@app.get("/", include_in_schema=False)
@app.get("/demo", include_in_schema=False)
async def root():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"status": "MA Trader API running. Frontend not found."}


@app.get("/favicon.svg", include_in_schema=False)
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    icon = FRONTEND_DIR / "favicon.svg"
    if icon.exists():
        return FileResponse(str(icon), media_type="image/svg+xml")
    return JSONResponse(status_code=404, content={"detail": "Not found"})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint.
    Prefers first-frame authentication ({"type": "auth", "token": "..."}) so tokens
    never appear in URLs / server logs; also falls back to Authorization header.
    """
    token = None
    auth_header = websocket.headers.get("authorization") or ""
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):].strip()

    await websocket.accept()

    client_env = "all"
    if not token:
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
            msg = json.loads(raw)
            if isinstance(msg, dict) and msg.get("type") == "auth":
                token = str(msg.get("token") or "").strip()
                raw_env = str(msg.get("env") or "").strip().lower()
                if raw_env in ("live", "paper"):
                    client_env = raw_env
        except Exception:
            await websocket.close(code=4401)
            return

    user = validate_token(token)
    if not user or getattr(user, "must_change_password", False):
        await websocket.close(code=4401)
        return

    ws_manager.register_accepted(websocket, env=client_env)

    bots = getattr(websocket.app.state, "bots", {}) or {}
    await ws_manager.send_personal(websocket, trading_router._bot_status_payload(bots))

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await ws_manager.send_personal(websocket, {"type": "pong"})
            except Exception:
                pass
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as exc:
        logger.warning(f"WebSocket error: {exc}")
        ws_manager.disconnect(websocket)
