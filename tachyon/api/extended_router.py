"""
Extended API Router — supplies missing endpoints for the VIT Storage frontend:
  GET  /api/v1/nodes                     — cloud provider node list
  GET  /api/v1/storage/stats             — aggregated storage statistics
  GET  /api/v1/quota                     — quota info
  GET  /api/v1/shared-links              — list shared links
  POST /api/v1/shared-links              — create shared link
  DELETE /api/v1/shared-links/{id}       — revoke shared link
  GET  /api/v1/shared/{token}            — public access to shared file
  GET  /api/v1/admin/overview            — admin system overview
  GET  /api/v1/wallet                    — VIT wallet info
  GET  /api/v1/providers/capabilities    — per-provider capability map
  POST /api/v1/providers/register        — register a new provider at runtime
"""

import uuid
import secrets
import logging
import hashlib
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.db.database import get_db
from tachyon.core.models import TachyonManifest, SharedLink
from tachyon.core.orchestrator import TachyonOrchestrator
from tachyon.api.models import (
    NodeInfo, StorageStats, QuotaInfo, AdminOverview, WalletInfo,
    SharedLinkCreate, SharedLinkResponse,
    ProviderCapabilities, RegisterProviderRequest, RegisterProviderResponse
)

logger = logging.getLogger(__name__)
extended_router = APIRouter()
_orchestrator = TachyonOrchestrator()

VERSION = "2.0.0"


# ─────────────────────────────────────────────
# NODES
# ─────────────────────────────────────────────

@extended_router.get(
    "/nodes",
    response_model=List[NodeInfo],
    summary="List cloud storage nodes",
    description="Returns health and usage statistics for all registered cloud provider nodes.",
)
async def list_nodes():
    try:
        health = await _orchestrator.pool.health_check()
    except Exception as e:
        logger.warning(f"Pool health check failed: {e}")
        health = {}

    caps = _orchestrator.pool.discover_capabilities()

    nodes: List[NodeInfo] = []
    for provider_id, info in health.items():
        nodes.append(NodeInfo(
            id=provider_id,
            name=provider_id.replace("_", " ").title(),
            type="Multi-Cloud Object Node",
            healthy=info.get("healthy", False),
            quarantined=info.get("quarantined", False),
            usage_pct=round(info.get("usage_pct", 0.0) * 100, 2),
            ping_ms=info.get("ping_ms"),
            capabilities=caps.get(provider_id, []),
        ))

    if not nodes:
        nodes.append(NodeInfo(
            id="local_disk",
            name="Local Disk",
            type="Multi-Cloud Object Node",
            healthy=True,
            quarantined=False,
            usage_pct=0.0,
            ping_ms=1,
            capabilities=["upload", "download", "delete", "exists", "metadata", "health_check", "streaming", "directories"],
        ))
    return nodes


# ─────────────────────────────────────────────
# STORAGE STATS
# ─────────────────────────────────────────────

@extended_router.get(
    "/storage/stats",
    response_model=StorageStats,
    summary="Aggregated storage statistics",
)
async def storage_stats(db: AsyncSession = Depends(get_db)):
    try:
        count_result = await db.execute(select(func.count(TachyonManifest.file_id)))
        total_files = count_result.scalar() or 0

        bytes_result = await db.execute(select(func.sum(TachyonManifest.size_bytes)))
        total_bytes = bytes_result.scalar() or 0

        latest_result = await db.execute(
            select(TachyonManifest.filename)
            .order_by(TachyonManifest.created_at.desc())
            .limit(1)
        )
        last_manifest = latest_result.scalar_one_or_none()

        try:
            health = await _orchestrator.pool.health_check()
            active_nodes = len(health)
        except Exception:
            active_nodes = 1

    except Exception as e:
        logger.error(f"Storage stats query failed: {e}")
        total_files = 0
        total_bytes = 0
        last_manifest = None
        active_nodes = 1

    return StorageStats(
        total_files=total_files,
        total_bytes=total_bytes,
        active_nodes=active_nodes,
        erasure_ratio=1.5,
        recent_failed_uploads=0,
        last_verified_manifest=last_manifest,
    )


# ─────────────────────────────────────────────
# QUOTA
# ─────────────────────────────────────────────

@extended_router.get(
    "/quota",
    response_model=QuotaInfo,
    summary="User storage quota",
)
async def get_quota(db: AsyncSession = Depends(get_db)):
    try:
        bytes_result = await db.execute(select(func.sum(TachyonManifest.size_bytes)))
        used_bytes = bytes_result.scalar() or 0
    except Exception:
        used_bytes = 0

    total_bytes = 100 * 1024 * 1024 * 1024  # 100 GB free plan
    used_pct = round((used_bytes / total_bytes) * 100, 4) if total_bytes > 0 else 0.0

    return QuotaInfo(
        used_bytes=used_bytes,
        total_bytes=total_bytes,
        used_pct=used_pct,
        plan="Free Plan",
    )


# ─────────────────────────────────────────────
# SHARED LINKS
# ─────────────────────────────────────────────

@extended_router.get(
    "/shared-links",
    response_model=List[SharedLinkResponse],
    summary="List all shared links",
)
async def list_shared_links(db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(
            select(SharedLink).order_by(SharedLink.created_at.desc())
        )
        links = result.scalars().all()
        out = []
        for lnk in links:
            out.append(SharedLinkResponse(
                id=lnk.id,
                file_id=lnk.file_id,
                filename=lnk.filename,
                token=lnk.token,
                url=f"/api/v1/shared/{lnk.token}",
                link_type=lnk.link_type,
                expires_at=lnk.expires_at.isoformat() if lnk.expires_at else None,
                download_count=lnk.download_count or 0,
                download_limit=lnk.download_limit,
                created_at=lnk.created_at.isoformat() if lnk.created_at else "",
            ))
        return out
    except Exception as e:
        logger.error(f"List shared links failed: {e}")
        return []


@extended_router.post(
    "/shared-links",
    response_model=SharedLinkResponse,
    summary="Create a shared link",
    status_code=201,
)
async def create_shared_link(
    payload: SharedLinkCreate,
    db: AsyncSession = Depends(get_db),
):
    manifest_result = await db.execute(
        select(TachyonManifest).where(TachyonManifest.file_id == payload.file_id)
    )
    manifest = manifest_result.scalar_one_or_none()
    if not manifest:
        raise HTTPException(status_code=404, detail="File not found")

    link_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(32)

    password_hash = None
    if payload.link_type == "password" and payload.password:
        password_hash = hashlib.sha256(payload.password.encode()).hexdigest()

    expires_at = None
    if payload.expires_hours is not None:
        expires_at = datetime.utcnow() + timedelta(hours=payload.expires_hours)

    link = SharedLink(
        id=link_id,
        file_id=payload.file_id,
        filename=manifest.filename,
        token=token,
        link_type=payload.link_type,
        password_hash=password_hash,
        expires_at=expires_at,
        download_limit=payload.download_limit,
    )
    db.add(link)
    await db.commit()
    await db.refresh(link)

    return SharedLinkResponse(
        id=link.id,
        file_id=link.file_id,
        filename=link.filename,
        token=link.token,
        url=f"/api/v1/shared/{link.token}",
        link_type=link.link_type,
        expires_at=link.expires_at.isoformat() if link.expires_at else None,
        download_count=0,
        download_limit=link.download_limit,
        created_at=link.created_at.isoformat(),
    )


@extended_router.delete(
    "/shared-links/{link_id}",
    summary="Revoke a shared link",
    status_code=204,
)
async def revoke_shared_link(link_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(SharedLink).where(SharedLink.id == link_id)
    )
    link = result.scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404, detail="Shared link not found")
    await db.delete(link)
    await db.commit()
    return Response(status_code=204)


@extended_router.get(
    "/shared/{token}",
    summary="Download via shared link",
    description="Public access endpoint. Increments download counter and streams the file.",
)
async def download_via_shared_link(token: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(SharedLink).where(SharedLink.token == token)
    )
    link = result.scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404, detail="Shared link not found or revoked")

    if link.expires_at and datetime.utcnow() > link.expires_at:
        raise HTTPException(status_code=410, detail="Shared link has expired")

    if link.download_limit is not None and link.download_count >= link.download_limit:
        raise HTTPException(status_code=410, detail="Download limit reached")

    from app.core.errors import AppError
    try:
        data = await _orchestrator.retrieve(db, link.file_id)
    except AppError as ae:
        raise HTTPException(status_code=ae.status_code, detail=ae.message)
    except Exception as e:
        logger.exception(f"Shared link retrieve failed for file_id={link.file_id}")
        raise HTTPException(status_code=500, detail="File could not be recovered")

    link.download_count = (link.download_count or 0) + 1
    await db.commit()

    manifest_result = await db.execute(
        select(TachyonManifest).where(TachyonManifest.file_id == link.file_id)
    )
    manifest = manifest_result.scalar_one_or_none()
    ct = (manifest.content_type if manifest and manifest.content_type else None) or "application/octet-stream"

    return Response(
        content=data,
        media_type=ct,
        headers={
            "Content-Disposition": f'attachment; filename="{link.filename}"',
        },
    )


# ─────────────────────────────────────────────
# ADMIN
# ─────────────────────────────────────────────

@extended_router.get(
    "/admin/overview",
    response_model=AdminOverview,
    summary="Administration system overview",
)
async def admin_overview(db: AsyncSession = Depends(get_db)):
    try:
        count_result = await db.execute(select(func.count(TachyonManifest.file_id)))
        total_files = count_result.scalar() or 0

        bytes_result = await db.execute(select(func.sum(TachyonManifest.size_bytes)))
        total_bytes = bytes_result.scalar() or 0

        from sqlalchemy import text
        await db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        logger.error(f"Admin overview DB query failed: {e}")
        total_files = 0
        total_bytes = 0
        db_status = "error"

    try:
        health = await _orchestrator.pool.health_check()
        active_nodes = len(health)
    except Exception:
        active_nodes = 1

    try:
        from app.services.cache import _get_redis
        r = _get_redis()
        if r:
            await r.ping()
            redis_status = "connected"
        else:
            redis_status = "not_configured"
    except Exception:
        redis_status = "not_configured"

    return AdminOverview(
        total_files=total_files,
        total_bytes=total_bytes,
        active_nodes=active_nodes,
        db_status=db_status,
        redis_status=redis_status,
        version=VERSION,
        uptime_info="Service running normally",
    )


# ─────────────────────────────────────────────
# PROVIDERS
# ─────────────────────────────────────────────

@extended_router.get(
    "/providers/capabilities",
    response_model=List[ProviderCapabilities],
    summary="Provider capability map",
    description="Returns the capability set for each registered storage provider.",
)
async def list_provider_capabilities():
    try:
        health = await _orchestrator.pool.health_check()
    except Exception as e:
        logger.warning(f"Pool health check failed: {e}")
        health = {}

    caps = _orchestrator.pool.discover_capabilities()
    result = []

    for pid, provider in _orchestrator.pool.providers.items():
        info = health.get(pid, {})
        provider_type = type(provider).__name__.replace("Provider", "").lower()
        result.append(ProviderCapabilities(
            provider_id=pid,
            name=pid.replace("_", " ").title(),
            provider_type=provider_type,
            capabilities=caps.get(pid, []),
            healthy=info.get("healthy", False),
            quarantined=info.get("quarantined", False),
            usage_pct=round(info.get("usage_pct", 0.0) * 100, 2),
        ))

    return result


@extended_router.post(
    "/providers/register",
    response_model=RegisterProviderResponse,
    summary="Register a new storage provider",
    description="Dynamically registers a new cloud storage provider without restarting the service.",
    status_code=201,
)
async def register_provider(payload: RegisterProviderRequest):
    ptype = payload.provider_type.lower()
    pid = payload.provider_id or f"{ptype}_{uuid.uuid4().hex[:8]}"

    if pid in _orchestrator.pool.providers:
        raise HTTPException(status_code=409, detail=f"Provider '{pid}' is already registered")

    try:
        provider = None
        creds = payload.credentials

        if ptype == "s3":
            from tachyon.providers.s3 import S3Provider
            provider = S3Provider(pid, credentials=creds)
        elif ptype == "dropbox":
            from tachyon.providers.dropbox import DropboxProvider
            provider = DropboxProvider(pid, credentials=creds)
        elif ptype == "gdrive":
            from tachyon.providers.gdrive import GoogleDriveProvider
            provider = GoogleDriveProvider(pid, credentials=creds)
        elif ptype == "onedrive":
            from tachyon.providers.onedrive import OneDriveProvider
            provider = OneDriveProvider(pid, credentials=creds)
        elif ptype in ("disk", "local"):
            from tachyon.providers.disk import DiskProvider
            storage_path = creds.get("storage_path", f"/tmp/tachyon_{pid}")
            provider = DiskProvider(pid, storage_path=storage_path)
        elif ptype in ("object_storage", "r2", "b2", "minio"):
            from tachyon.providers.object_storage import ObjectStorageProvider
            provider = ObjectStorageProvider(pid, credentials=creds)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown provider type: {ptype}")

        _orchestrator.pool.register(pid, provider)
        healthy = await provider.health_check()

        return RegisterProviderResponse(
            provider_id=pid,
            provider_type=ptype,
            registered=True,
            healthy=healthy,
            message=f"Provider '{pid}' registered successfully",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Provider registration failed: {e}")
        raise HTTPException(status_code=500, detail=f"Provider registration failed: {e}")


# ─────────────────────────────────────────────
# WALLET / AI
# ─────────────────────────────────────────────

@extended_router.get(
    "/wallet",
    response_model=WalletInfo,
    summary="VIT Wallet and AI credits",
)
async def get_wallet():
    """Returns VIT wallet info. Reads from platform config if available, else returns defaults."""
    try:
        from app.db.database import AsyncSessionLocal
        from app.modules.wallet.models import PlatformConfig
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(PlatformConfig).where(PlatformConfig.key == "wallet_info")
            )
            config = result.scalar_one_or_none()
            if config and config.value:
                v = config.value
                return WalletInfo(
                    address=v.get("address", "vit1_not_configured"),
                    vit_balance=v.get("vit_balance", 0.0),
                    storage_credits=v.get("storage_credits", 0.0),
                    plan=v.get("plan", "Free Plan"),
                    staked_vit=v.get("staked_vit", 0.0),
                    ai_requests_today=v.get("ai_requests_today", 0),
                    ai_requests_limit=v.get("ai_requests_limit", 100),
                )
    except Exception as e:
        logger.debug(f"Wallet config read failed (expected on fresh deploy): {e}")

    return WalletInfo(
        address="vit1_swarm_coordinator_main",
        vit_balance=0.0,
        storage_credits=100.0,
        plan="Free Plan",
        staked_vit=0.0,
        ai_requests_today=0,
        ai_requests_limit=100,
    )


# ─────────────────────────────────────────────
# DEBUG: Provider Upload Test
# ─────────────────────────────────────────────

@extended_router.get(
    "/debug/providers",
    summary="Per-provider upload/download probe (admin diagnostic)",
    description="Runs a tiny test upload on each registered provider and reports pass/fail with the actual error message.",
)
async def debug_providers():
    import time, asyncio
    registry = _orchestrator.pool
    results = {}
    probe_data = b"vit-probe-" + str(int(time.time())).encode()

    async def _probe_one(pid: str, provider):
        t0 = time.monotonic()
        try:
            name = f"__probe_{pid}_{int(time.time())}__"
            ok = await asyncio.wait_for(provider.upload(probe_data, name), timeout=12.0)
            if ok:
                # Cleanup (best-effort)
                try:
                    await asyncio.wait_for(provider.delete(name), timeout=5.0)
                except Exception:
                    pass
                return {"ok": True, "latency_ms": round((time.monotonic()-t0)*1000, 1)}
            else:
                return {"ok": False, "error": "upload() returned False", "latency_ms": round((time.monotonic()-t0)*1000, 1)}
        except asyncio.TimeoutError:
            return {"ok": False, "error": "Timed out after 12s"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    tasks = [_probe_one(pid, p) for pid, p in registry.providers.items()]
    probe_results = await asyncio.gather(*tasks, return_exceptions=True)
    for (pid, _), result in zip(registry.providers.items(), probe_results):
        results[pid] = result if not isinstance(result, Exception) else {"ok": False, "error": str(result)}

    # Also report disabled providers
    for pid, reason in registry.disabled_providers.items():
        results[pid] = {"ok": False, "error": f"Disabled at startup: {reason}", "disabled": True}

    # Report quarantine state
    now = time.time()
    for pid, until in registry.degraded_until.items():
        if until > now and pid in results:
            results[pid]["quarantined_for_s"] = round(until - now)

    return results
