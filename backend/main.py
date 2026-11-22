"""
MA Trader - FastAPI Main Entry Point
"""
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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from database import create_tables, SessionLocal
from config import ConfigManager
from routers import settings as settings_router
from routers import trading as trading_router
from routers import backtest as backtest_router
from routers import stats as stats_router

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


class ConnectionManager:
    """Manages WebSocket connections."""

    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"WebSocket client connected. Total: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info(f"WebSocket client disconnected. Total: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        """Broadcast a JSON message to all connected clients."""
        text = json.dumps(message, ensure_ascii=False, default=str)
        dead = []
        for connection in self.active_connections:
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
    # Startup
    logger.info("Starting MA Trader...")
    create_tables()

    # Store shared objects on app.state
    app.state.ws_manager = ws_manager
    app.state.config = ConfigManager()
    app.state.bot = None  # TradingBot will be initialised lazily via /api/trading/start

    logger.info("MA Trader ready.")
    yield
    # Shutdown
    if app.state.bot is not None:
        try:
            await app.state.bot.stop()
        except Exception as exc:
            logger.warning(f"Error stopping bot on shutdown: {exc}")
    logger.info("MA Trader shut down.")


app = FastAPI(title="MA Trader", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_no_cache_headers(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css")) or "/js/" in path or "/css/" in path:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# Static files (frontend)
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
    if (FRONTEND_DIR / "css").exists():
        app.mount("/css", StaticFiles(directory=str(FRONTEND_DIR / "css")), name="css")
    if (FRONTEND_DIR / "js").exists():
        app.mount("/js", StaticFiles(directory=str(FRONTEND_DIR / "js")), name="js")

# Routers
app.include_router(settings_router.router, prefix="/api")
app.include_router(trading_router.router, prefix="/api")
app.include_router(backtest_router.router, prefix="/api")
app.include_router(stats_router.router, prefix="/api")


@app.get("/", include_in_schema=False)
async def root():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"status": "MA Trader API running. Frontend not found."}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)

    # Send current bot status immediately upon connection
    bot = websocket.app.state.bot
    if bot is not None:
        await ws_manager.send_personal(websocket, {"type": "bot_status", "running": bot.running})
    else:
        await ws_manager.send_personal(websocket, {"type": "bot_status", "running": False})

    try:
        while True:
            # Keep connection alive; bot broadcasts outbound, client can send pings
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
