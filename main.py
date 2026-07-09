import asyncio
import os
import logging
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from tachyon.core.config import settings
from tachyon.core.database import init_db, AsyncSessionLocal
from tachyon.core.worker import TachyonVerificationWorker
from app.services.cache import _get_redis

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - [%(request_id)s] %(message)s'
)
logger = logging.getLogger("tachyon")

# Custom logging filter for request ID
class RequestIDFilter(logging.Filter):
    def filter(self, record):
        record.request_id = getattr(record, 'request_id', 'startup')
        return True

logger.addFilter(RequestIDFilter())

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Service starting up...")

    # Ensure frontend/static directory exists
    os.makedirs("frontend/static", exist_ok=True)

    # 1. Startup Schema Creation & Validation
    try:
        await init_db()
    except Exception as e:
        logger.critical(f"Startup Database Migrations failed: {e}")

    # 2. Database Connectivity Diagnostics
    db_healthy = False
    try:
        async with AsyncSessionLocal() as session:
            res = await session.execute(text("SELECT 1"))
            res.scalar()
            db_healthy = True
            logger.info("Database connectivity check: OK")
    except Exception as e:
        logger.error(f"Database connectivity check: FAILED ({e})")

    # 3. Redis Connectivity Diagnostics
    redis_healthy = False
    try:
        r = _get_redis()
        if r:
            await r.ping()
            redis_healthy = True
            logger.info("Redis connectivity check: OK")
        else:
            logger.info("Redis not configured. Operating with in-memory cache fallback.")
    except Exception as e:
        logger.warning(f"Redis connectivity check: FAILED (%s). Memory fallback active. Error: {e}")

    # 4. Background Shards Integrity Worker
    worker = TachyonVerificationWorker(interval_seconds=3600)
    task = asyncio.create_task(worker.start())

    # Set lifespan states for diagnostics
    app.state.db_healthy = db_healthy
    app.state.redis_healthy = redis_healthy
    app.state.worker = worker

    yield

    # Graceful Shutdown
    logger.info("Service shutting down...")
    worker.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Close Redis connection if active
    try:
        if r:
            await r.aclose()
            logger.info("Redis connection closed cleanly.")
    except Exception as e:
        logger.debug(f"Redis connection close exception: {e}")

    logger.info("Service shutdown completed cleanly.")

app = FastAPI(
    title="VIT Storage Service",
    description="Decentralised swarm storage coordination — EEC erasure coding, multi-cloud burst transfer",
    version="1.1.0",
    lifespan=lifespan,
)

@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    # Ingest request_id into state
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Exception handlers mapping
from app.core.errors import AppError, error_response

@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return error_response(
        request=request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details
    )

# Include Router
from tachyon.api.router import router as api_router
app.include_router(api_router, prefix="/api/v1")

# Mount static files directory if it exists or create on the fly
os.makedirs("frontend/static", exist_ok=True)
app.mount("/static", StaticFiles(directory="frontend/static"), name="static")

@app.get("/")
async def root():
    return RedirectResponse(url="/dashboard")

@app.get("/dashboard", response_class=HTMLResponse, summary="VIT Storage Dashboard")
async def dashboard():
    index_path = os.path.join("frontend", "static", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return HTMLResponse("""
        <!DOCTYPE html>
        <html>
        <head>
            <title>VIT Storage Portal</title>
            <script src="https://cdn.tailwindcss.com"></script>
        </head>
        <body class="bg-gray-900 text-white flex flex-col items-center justify-center min-h-screen">
            <h1 class="text-3xl font-bold mb-4">VIT Storage UI Initializing</h1>
            <p class="text-gray-400">The modern UI is being built. Please stand by...</p>
        </body>
        </html>
    """)

@app.get("/ping", summary="Service Liveness Ping")
async def ping():
    return {"ping": "pong", "status": "ok", "timestamp": datetime.utcnow().isoformat()}

@app.get("/health", summary="Active Subsystem Diagnostics Check")
async def health():
    db_ok = getattr(app.state, "db_healthy", False)
    redis_ok = getattr(app.state, "redis_healthy", False)

    # Fetch database connection health dynamically
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
            db_ok = True
    except Exception:
        db_ok = False

    # Fetch provider health dynamically from registry
    try:
        from tachyon.providers.registry import ProviderRegistry
        registry = ProviderRegistry()
        provider_stats = await registry.health_check()
    except Exception as e:
        provider_stats = {"error": f"Failed to check providers health: {e}"}

    status_str = "quantum_stable" if db_ok else "degraded"
    return {
        "status": status_str,
        "version": "1.1.0",
        "plane": "coordination",
        "timestamp": datetime.utcnow().isoformat(),
        "database": "connected" if db_ok else "disconnected",
        "redis": "connected" if redis_ok else "not_configured_or_disconnected",
        "subsystems": provider_stats
    }

@app.get("/metrics", summary="Prometheus Metrics Endpoint")
async def metrics():
    # Retrieve dynamic status indicators
    db_ok = 1 if getattr(app.state, "db_healthy", False) else 0
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
            db_ok = 1
    except Exception:
        db_ok = 0

    redis_ok = 1 if getattr(app.state, "redis_healthy", False) else 0

    metric_output = (
        "# HELP tachyon_up Service status indicator (1 = UP, 0 = DOWN)\n"
        "# TYPE tachyon_up gauge\n"
        "tachyon_up 1\n"
        "# HELP tachyon_database_connected DB connection state\n"
        "# TYPE tachyon_database_connected gauge\n"
        f"tachyon_database_connected {db_ok}\n"
        "# HELP tachyon_redis_connected Redis connection state\n"
        "# TYPE tachyon_redis_connected gauge\n"
        f"tachyon_redis_connected {redis_ok}\n"
    )
    return Response(content=metric_output, media_type="text/plain")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
