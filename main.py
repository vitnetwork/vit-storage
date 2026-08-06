import asyncio
import os
import logging
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse, JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from tachyon.core.config import settings
from tachyon.core.database import init_db, AsyncSessionLocal
from tachyon.core.worker import TachyonVerificationWorker
from app.services.cache import _get_redis

VERSION = "2.0.1"

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("tachyon")


class RequestIDFilter(logging.Filter):
    def filter(self, record):
        record.request_id = getattr(record, "request_id", "startup")
        return True


logger.addFilter(RequestIDFilter())

# ---------------------------------------------------------------------------
# Singleton registry — shared across the whole process lifetime.
# Instantiated once during lifespan startup so the /health endpoint never
# creates a new ProviderRegistry (which would re-bootstrap and re-run
# expensive credential validation on every request).
# ---------------------------------------------------------------------------
_registry = None


def get_registry():
    """Return the process-wide ProviderRegistry singleton."""
    return _registry


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _registry

    logger.info(f"VIT Storage Service v{VERSION} starting up...")
    os.makedirs("frontend/static", exist_ok=True)

    # ── 1. Database schema ────────────────────────────────────────────────
    try:
        await init_db()
    except Exception as e:
        logger.critical(f"Startup database initialization failed: {e}")

    # ── 2. Database connectivity ──────────────────────────────────────────
    db_healthy = False
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
            db_healthy = True
            logger.info("Database connectivity check: OK")
    except Exception as e:
        logger.error(f"Database connectivity check: FAILED ({e})")

    # ── 3. Redis connectivity ─────────────────────────────────────────────
    redis_healthy = False
    r = None
    try:
        r = _get_redis()
        if r:
            await r.ping()
            redis_healthy = True
            logger.info("Redis connectivity check: OK")
        else:
            logger.info("Redis not configured — operating with in-memory cache fallback.")
    except Exception as e:
        logger.warning(f"Redis connectivity check: FAILED — memory fallback active. Error: {e}")

    # ── 4. Provider registry + startup diagnostics ────────────────────────
    from tachyon.providers.registry import ProviderRegistry

    _registry = ProviderRegistry()
    app.state.registry = _registry

    logger.info("=== Storage Provider Startup Diagnostics ===")
    try:
        diag = await _registry.startup_diagnostics()
        logger.info(
            f"Provider summary: "
            f"{len(diag['active_providers'])} active, "
            f"{len(diag['disabled_providers'])} disabled, "
            f"all_healthy={diag['all_healthy']}"
        )
    except Exception as e:
        logger.warning(f"Provider startup diagnostics failed: {e}")
    logger.info("=== End Provider Diagnostics ===")

    # ── 5. Background integrity worker ────────────────────────────────────
    worker = TachyonVerificationWorker(interval_seconds=3600)
    task   = asyncio.create_task(worker.start())

    # ── 6. VIT Chain proof reporter ───────────────────────────────────────
    from tachyon.proof_reporter import ProofReporter
    proof_reporter = ProofReporter()
    await proof_reporter.start()

    app.state.db_healthy     = db_healthy
    app.state.redis_healthy  = redis_healthy
    app.state.worker         = worker
    app.state.proof_reporter = proof_reporter

    logger.info(f"VIT Storage Service v{VERSION} ready.")
    yield

    # ── Graceful shutdown ─────────────────────────────────────────────────
    logger.info("Service shutting down...")
    await proof_reporter.stop()
    await worker.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    try:
        if r:
            await r.aclose()
            logger.info("Redis connection closed cleanly.")
    except Exception as e:
        logger.debug(f"Redis connection close exception: {e}")

    logger.info("Service shutdown completed cleanly.")


app = FastAPI(
    title="VIT Storage Service",
    description=(
        "Decentralised swarm storage coordination — "
        "EEC erasure coding, multi-cloud burst transfer"
    ),
    version=VERSION,
    lifespan=lifespan,
)

# ── Middleware ────────────────────────────────────────────────────────────────
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"]       = request_id
    response.headers["X-Tachyon-Version"]  = VERSION
    return response


# ── Exception handlers ────────────────────────────────────────────────────────
from app.core.errors import AppError, error_response


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return error_response(
        request=request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
    )


# ── Routers ──────────────────────────────────────────────────────────────────
from tachyon.api.router import router as api_router
from tachyon.api.extended_router import extended_router

app.include_router(api_router,      prefix="/api/v1", tags=["Storage Core"])
app.include_router(extended_router, prefix="/api/v1", tags=["Extended API"])

# VIT Connect
from vit_connect.router import router as connect_router
app.include_router(connect_router, prefix="/api/v1/connect", tags=["VIT Connect"])

# ── Static files ──────────────────────────────────────────────────────────────
os.makedirs("frontend/static", exist_ok=True)
app.mount("/static", StaticFiles(directory="frontend/static"), name="static")


def _serve_spa() -> HTMLResponse:
    index_path = os.path.join("frontend", "static", "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            content = f.read()
        return HTMLResponse(content=content)
    return HTMLResponse("<h1>VIT Storage — UI not found</h1>", status_code=503)


# ── SPA routes ────────────────────────────────────────────────────────────────
@app.get("/",               include_in_schema=False)
async def root():            return _serve_spa()

@app.get("/dashboard",      response_class=HTMLResponse, include_in_schema=False)
async def dashboard():       return _serve_spa()

@app.get("/my-files",       response_class=HTMLResponse, include_in_schema=False)
async def my_files():        return _serve_spa()

@app.get("/shared-links",   response_class=HTMLResponse, include_in_schema=False)
async def shared_links_page(): return _serve_spa()

@app.get("/api-playground", response_class=HTMLResponse, include_in_schema=False)
async def api_playground_page(): return _serve_spa()

@app.get("/administration", response_class=HTMLResponse, include_in_schema=False)
async def administration_page(): return _serve_spa()

@app.get("/wallet",         response_class=HTMLResponse, include_in_schema=False)
async def wallet_page():     return _serve_spa()

@app.get("/documentation",  response_class=HTMLResponse, include_in_schema=False)
async def documentation_page(): return _serve_spa()

@app.get("/connect",        response_class=HTMLResponse, include_in_schema=False)
async def connect_page():    return _serve_spa()


# ── Core endpoints ────────────────────────────────────────────────────────────
@app.get("/ping", summary="Service Liveness Ping")
async def ping():
    return {
        "ping":      "pong",
        "status":    "ok",
        "version":   VERSION,
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/health", summary="Active Subsystem Diagnostics Check")
async def health(request: Request):
    # ── Database ──────────────────────────────────────────────────────────
    db_ok = False
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
            db_ok = True
    except Exception:
        db_ok = False

    # ── Redis ─────────────────────────────────────────────────────────────
    redis_ok = getattr(app.state, "redis_healthy", False)

    # ── Provider summary — count only, no external API calls ─────────────
    # Calling cloud provider SDKs (Dropbox, OneDrive) in a health-check
    # endpoint runs synchronous C-extension code in threads and can crash
    # the process with SIGSEGV on bad/expired tokens.  Detailed provider
    # health is available at /api/v1/providers/health (authenticated).
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        provider_summary = {"active": 0, "disabled": 0}
    else:
        provider_summary = {
            "active":   len(registry.providers),
            "disabled": len(registry.disabled_providers),
            "available": registry.available_provider_count(),
        }

    # Detect if the service is still warming up (lifespan hasn't finished)
    startup_complete = hasattr(app.state, "db_healthy")
    if not startup_complete:
        return JSONResponse(
            status_code=503,
            content={
                "status":      "warming",
                "retry_after": 15,
                "version":     VERSION,
            },
        )

    status_str = "healthy" if db_ok else "degraded"
    return {
        "status":     status_str,
        "version":    VERSION,
        "plane":      "coordination",
        "timestamp":  datetime.utcnow().isoformat(),
        "database":   "connected"      if db_ok    else "disconnected",
        "redis":      "connected"      if redis_ok else "not_configured_or_disconnected",
        "providers":  provider_summary,
    }


@app.get("/metrics", summary="Prometheus Metrics Endpoint")
async def metrics(request: Request):
    db_ok       = 1
    total_files = 0
    total_bytes = 0

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
            from sqlalchemy import select, func
            from tachyon.core.models import TachyonManifest
            count_res   = await session.execute(select(func.count(TachyonManifest.file_id)))
            total_files = count_res.scalar() or 0
            bytes_res   = await session.execute(select(func.sum(TachyonManifest.size_bytes)))
            total_bytes = bytes_res.scalar() or 0
    except Exception:
        db_ok = 0

    redis_ok = 1 if getattr(app.state, "redis_healthy", False) else 0

    registry     = getattr(request.app.state, "registry", None)
    active_nodes = registry.available_provider_count() if registry else 1

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
        "# HELP tachyon_total_files Total number of files stored\n"
        "# TYPE tachyon_total_files gauge\n"
        f"tachyon_total_files {total_files}\n"
        "# HELP tachyon_total_bytes_stored Total bytes of original data stored\n"
        "# TYPE tachyon_total_bytes_stored gauge\n"
        f"tachyon_total_bytes_stored {total_bytes}\n"
        "# HELP tachyon_active_nodes Number of active, non-quarantined storage nodes\n"
        "# TYPE tachyon_active_nodes gauge\n"
        f"tachyon_active_nodes {active_nodes}\n"
    )
    return Response(content=metric_output, media_type="text/plain")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
