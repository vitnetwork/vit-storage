import asyncio
import os
import logging
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from tachyon.core.worker import TachyonVerificationWorker

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
    logger.info("Service starting up")
    worker = TachyonVerificationWorker(interval_seconds=3600)
    task = asyncio.create_task(worker.start())
    yield
    logger.info("Service shutting down")
    worker.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

app = FastAPI(
    title="VIT Storage Service",
    description="Decentralised swarm storage coordination — EEC erasure coding, multi-cloud burst transfer",
    version="1.1.0",
    lifespan=lifespan,
)

@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    # Set request_id in a context-local way if needed, for now just pass it
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

from tachyon.api.router import router as api_router
app.include_router(api_router, prefix="/api/v1")

@app.get("/")
async def root():
    return RedirectResponse(url="/health")

@app.get("/health")
async def health():
    return {
        "status": "quantum_stable",
        "version": "1.1.0",
        "plane": "coordination",
        "timestamp": datetime.utcnow().isoformat() if "datetime" in globals() else None
    }

@app.get("/metrics")
async def metrics():
    # Placeholder for Prometheus metrics
    return "# HELP tachyon_up Service status\n# TYPE tachyon_up gauge\ntachyon_up 1"

if __name__ == "__main__":
    import uvicorn
    from datetime import datetime
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
