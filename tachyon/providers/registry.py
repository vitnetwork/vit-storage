import os
import json
import time
import asyncio
import logging
from typing import List, Dict, Any, Tuple, Optional
from tachyon.providers.base import CloudProvider
from tachyon.providers.disk import DiskProvider
from tachyon.providers.gdrive import GoogleDriveProvider
from tachyon.providers.dropbox import DropboxProvider
from tachyon.providers.onedrive import OneDriveProvider
from tachyon.providers.s3 import S3Provider
from tachyon.providers.object_storage import ObjectStorageProvider

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """
    Centralized Provider Registry and Failover Manager.

    Changes vs. previous version
    ─────────────────────────────
    • _bootstrap_providers() catches *all* exceptions per provider (including
      ValueError from credential validation) so one bad provider never prevents
      the others from loading.
    • health_check() runs provider checks concurrently with a per-provider
      timeout, and reports disabled-provider status explicitly.
    • startup_diagnostics() produces a clear, human-readable boot report.
    • Invalid/disabled providers are tracked in self.disabled_providers for
      introspection via the /health endpoint.
    """

    QUOTA_GUARD_PCT       = 0.90
    DEGRADED_TIMEOUT_SECONDS = 120
    HEALTH_CHECK_TIMEOUT  = 12.0   # seconds per provider

    def __init__(self):
        self.providers: Dict[str, CloudProvider] = {}
        self.disabled_providers: Dict[str, str]  = {}   # id -> reason
        self.degraded_until: Dict[str, float]    = {}
        self.usage_cache: Dict[str, Dict[str, Any]] = {}
        self._current_index = 0
        self._upload_failures: Dict[str, int] = {}  # consecutive failures per provider
        self._bootstrap_providers()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, provider_id: str, provider: CloudProvider):
        """Manually register a provider instance."""
        self.providers[provider_id] = provider
        logger.info(f"Registered provider: {provider_id} ({type(provider).__name__})")

    def _safe_register(self, provider_id: str, factory, label: str):
        """
        Call *factory()* to create a provider and register it.
        Catches all exceptions so a bad provider never blocks the registry.
        """
        try:
            provider = factory()
            self.register(provider_id, provider)
        except Exception as exc:
            reason = str(exc)
            self.disabled_providers[provider_id] = reason
            logger.error(
                f"Provider '{provider_id}' ({label}) disabled at startup: {reason}"
            )

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def _bootstrap_providers(self):
        """Auto-discover and configure storage providers from environment variables."""
        logger.info("Bootstrapping storage providers from environment...")

        # 1. Local Disk (always available as fallback)
        disk_path = os.getenv("TACHYON_STORAGE_PATH", "/tmp/tachyon_storage")
        self._safe_register(
            "local_disk",
            lambda: DiskProvider("local_disk", storage_path=disk_path),
            "DiskProvider",
        )

        # 2. Google Drive — multi-account from JSON array
        gdrive_keys_raw = os.getenv("GDRIVE_SERVICE_ACCOUNT_KEYS", "[]")
        try:
            gdrive_keys = json.loads(gdrive_keys_raw)
            for i, key in enumerate(gdrive_keys):
                pid = f"gdrive_sa_{i}"
                self._safe_register(
                    pid,
                    lambda k=key: GoogleDriveProvider(f"gdrive_sa_{i}", credentials=k),
                    "GoogleDriveProvider",
                )
        except Exception as e:
            logger.error(f"Failed to parse GDRIVE_SERVICE_ACCOUNT_KEYS: {e}")

        # Google Drive — single account
        if os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON"):
            self._safe_register(
                "gdrive_sa_main",
                lambda: GoogleDriveProvider("gdrive_sa_main"),
                "GoogleDriveProvider",
            )

        # 3. OneDrive — multi-account from JSON array
        onedrive_accounts_raw = os.getenv("ONEDRIVE_ACCOUNTS", "[]")
        try:
            onedrive_accounts = json.loads(onedrive_accounts_raw)
            for i, acc in enumerate(onedrive_accounts):
                pid = f"onedrive_{i}"
                self._safe_register(
                    pid,
                    lambda a=acc, p=pid: OneDriveProvider(p, credentials=a),
                    "OneDriveProvider",
                )
        except Exception as e:
            logger.error(f"Failed to parse ONEDRIVE_ACCOUNTS: {e}")

        # OneDrive — single account from discrete env vars
        if os.getenv("ONEDRIVE_CLIENT_ID") and os.getenv("ONEDRIVE_CLIENT_SECRET"):
            self._safe_register(
                "onedrive_main",
                lambda: OneDriveProvider("onedrive_main"),
                "OneDriveProvider",
            )

        # 4. Dropbox — multi-account from JSON array
        dropbox_tokens_raw = os.getenv("DROPBOX_TOKENS", "[]")
        try:
            dropbox_tokens = json.loads(dropbox_tokens_raw)
            for i, token in enumerate(dropbox_tokens):
                pid = f"dropbox_{i}"
                self._safe_register(
                    pid,
                    lambda t=token, p=pid: DropboxProvider(p, credentials={"access_token": t}),
                    "DropboxProvider",
                )
        except Exception as e:
            logger.error(f"Failed to parse DROPBOX_TOKENS: {e}")

        # Dropbox — single account from discrete env vars
        if os.getenv("DROPBOX_ACCESS_TOKEN") or os.getenv("DROPBOX_REFRESH_TOKEN"):
            self._safe_register(
                "dropbox_main",
                lambda: DropboxProvider("dropbox_main"),
                "DropboxProvider",
            )

        # 5. S3-compatible
        if os.getenv("S3_ACCESS_KEY_ID") and os.getenv("S3_SECRET_ACCESS_KEY"):
            self._safe_register(
                "s3_main",
                lambda: S3Provider("s3_main"),
                "S3Provider",
            )

        # 6. Generic Object Storage (R2 / MinIO / B2)
        if os.getenv("OBJECT_STORAGE_ACCESS_KEY") and os.getenv("OBJECT_STORAGE_SECRET_KEY"):
            creds = {
                "access_key":   os.getenv("OBJECT_STORAGE_ACCESS_KEY"),
                "secret_key":   os.getenv("OBJECT_STORAGE_SECRET_KEY"),
                "endpoint_url": os.getenv("OBJECT_STORAGE_ENDPOINT"),
                "bucket_name":  os.getenv("OBJECT_STORAGE_BUCKET"),
            }
            self._safe_register(
                "object_storage_main",
                lambda c=creds: ObjectStorageProvider("object_storage_main", credentials=c),
                "ObjectStorageProvider",
            )

        active   = len(self.providers)
        disabled = len(self.disabled_providers)
        logger.info(
            f"Provider bootstrap complete: {active} active, {disabled} disabled."
        )

        if self.disabled_providers:
            for pid, reason in self.disabled_providers.items():
                logger.warning(f"  ✗ DISABLED [{pid}]: {reason}")

    # ------------------------------------------------------------------
    # Startup diagnostics
    # ------------------------------------------------------------------

    async def startup_diagnostics(self) -> Dict[str, Any]:
        """
        Run a quick health check on every registered provider at startup and
        return a structured report suitable for logging.
        """
        report: Dict[str, Any] = {
            "active_providers":   list(self.providers.keys()),
            "disabled_providers": dict(self.disabled_providers),
            "health":             {},
        }

        async def _check_one(pid: str, provider: CloudProvider):
            try:
                healthy = await asyncio.wait_for(
                    provider.health_check(), timeout=self.HEALTH_CHECK_TIMEOUT
                )
                usage_pct = 0.0
                try:
                    usage = await asyncio.wait_for(
                        provider.get_usage(), timeout=self.HEALTH_CHECK_TIMEOUT
                    )
                    if usage.get("quota_bytes", 0) > 0:
                        usage_pct = usage["used_bytes"] / usage["quota_bytes"]
                except Exception:
                    pass
                return pid, {"healthy": healthy, "usage_pct": round(usage_pct, 4), "error": None}
            except asyncio.TimeoutError:
                return pid, {"healthy": False, "usage_pct": 0.0, "error": "timeout"}
            except Exception as exc:
                return pid, {"healthy": False, "usage_pct": 0.0, "error": str(exc)}

        tasks    = [_check_one(pid, p) for pid, p in self.providers.items()]
        results  = await asyncio.gather(*tasks, return_exceptions=True)

        all_ok = True
        for item in results:
            if isinstance(item, Exception):
                continue
            pid, status = item
            report["health"][pid] = status
            if not status["healthy"]:
                all_ok = False
                logger.warning(
                    f"  ✗ UNHEALTHY [{pid}]: {status['error'] or 'check returned False'}"
                )
            else:
                logger.info(
                    f"  ✓ OK        [{pid}]  usage={status['usage_pct']*100:.1f}%"
                )

        for pid, reason in self.disabled_providers.items():
            report["health"][pid] = {
                "healthy":   False,
                "usage_pct": 0.0,
                "error":     f"disabled at startup: {reason}",
            }

        report["all_healthy"] = all_ok
        return report

    # ------------------------------------------------------------------
    # Provider access
    # ------------------------------------------------------------------

    def get_provider(self, provider_id: str) -> Optional[CloudProvider]:
        return self.providers.get(provider_id)

    def list_providers(self, active_only: bool = False) -> List[CloudProvider]:
        if not active_only:
            return list(self.providers.values())
        now = time.time()
        return [
            p for pid, p in self.providers.items()
            if self.degraded_until.get(pid, 0) < now
        ]

    def discover_capabilities(self) -> Dict[str, List[str]]:
        caps = {}
        for pid, p in self.providers.items():
            plist = ["upload", "download", "delete", "exists", "metadata", "health_check"]
            if isinstance(p, (DiskProvider, S3Provider, ObjectStorageProvider)):
                plist += ["signed_urls", "directories"]
            if isinstance(p, (DiskProvider, S3Provider, ObjectStorageProvider, OneDriveProvider)):
                plist.append("streaming")
            caps[pid] = plist
        return caps

    def is_degraded(self, provider_id: str) -> bool:
        return time.time() < self.degraded_until.get(provider_id, 0)

    async def is_full(self, provider_id: str) -> bool:
        provider = self.get_provider(provider_id)
        if not provider:
            return True
        now = time.time()
        if (
            provider_id in self.usage_cache
            and (now - self.usage_cache[provider_id]["timestamp"]) < 300
        ):
            usage = self.usage_cache[provider_id]
        else:
            try:
                quota = await asyncio.wait_for(
                    provider.get_usage(), timeout=self.HEALTH_CHECK_TIMEOUT
                )
                usage = {
                    "used_bytes":  quota.get("used_bytes", 0),
                    "quota_bytes": quota.get("quota_bytes", 10 * 1024**3),
                    "timestamp":   now,
                }
                self.usage_cache[provider_id] = usage
            except Exception:
                usage = {"used_bytes": 0, "quota_bytes": 10 * 1024**3, "timestamp": now}

        if usage["quota_bytes"] > 0:
            return usage["used_bytes"] / usage["quota_bytes"] > self.QUOTA_GUARD_PCT
        return False

    # ------------------------------------------------------------------
    # Shard I/O
    # ------------------------------------------------------------------

    async def upload_shard(self, shard_id: str, data: bytes) -> Tuple[str, str]:
        plist = list(self.providers.values())
        if not plist:
            raise RuntimeError("No storage providers registered in Tachyon Registry!")

        total = len(plist)
        for _ in range(total):
            provider = plist[self._current_index % total]
            self._current_index += 1
            pid = provider.account_id

            if self.is_degraded(pid) or await self.is_full(pid):
                continue

            try:
                file_id = await provider.upload_shard(shard_id, data)
                self._upload_failures.pop(pid, None)  # reset strike counter on success
                return pid, file_id
            except Exception as e:
                # Check for permanent provider failure (e.g. Drive service account quota)
                if getattr(provider, "_permanently_disabled", False):
                    reason = getattr(provider, "_disable_reason", str(e))
                    logger.error(f"Provider {pid} permanently disabled — removing from pool: {reason}")
                    self.disabled_providers[pid] = reason
                    # Remove from active providers so it is never tried again
                    self.providers.pop(pid, None)
                    break  # restart loop with updated provider list

                self._upload_failures[pid] = self._upload_failures.get(pid, 0) + 1
                strikes = self._upload_failures[pid]
                # Give providers a grace period: quarantine only after 2 consecutive failures.
                # First failure gets a short 15s cooldown so a transient error recovers fast.
                cooldown = 15 if strikes == 1 else self.DEGRADED_TIMEOUT_SECONDS
                logger.error(
                    f"Provider {pid} upload failure #{strikes}. "
                    f"Cooldown: {cooldown}s. Error: {e}"
                )
                self.degraded_until[pid] = time.time() + cooldown

        # Last-resort fallback to local disk
        try:
            fallback = self.get_provider("local_disk")
            if fallback:
                logger.warning("All primary providers quarantined! Failing over to local_disk.")
                file_id = await fallback.upload_shard(shard_id, data)
                return "local_disk", file_id
        except Exception as e:
            logger.error(f"Fallback to local disk failed: {e}")

        raise RuntimeError("No available storage providers and local disk fallback failed!")

    async def download_shard(self, provider_id: str, file_id: str) -> bytes:
        provider = self.get_provider(provider_id)
        if not provider:
            raise RuntimeError(f"Provider {provider_id} not registered")
        try:
            return await provider.download_shard(file_id)
        except Exception as e:
            logger.error(f"Download failed from provider {provider_id}: {e}")
            self.degraded_until[provider_id] = time.time() + self.DEGRADED_TIMEOUT_SECONDS
            raise

    async def delete_shard(self, provider_id: str, file_id: str) -> bool:
        provider = self.get_provider(provider_id)
        if not provider:
            return False
        try:
            return await provider.delete_shard(file_id)
        except Exception as e:
            logger.warning(f"Delete failed on provider {provider_id}: {e}")
            return False

    # ------------------------------------------------------------------
    # Health / diagnostics
    # ------------------------------------------------------------------

    async def health_check(self) -> Dict[str, Dict[str, Any]]:
        """Concurrent health check across all registered providers."""

        async def _one(pid: str, provider: CloudProvider):
            try:
                healthy = await asyncio.wait_for(
                    provider.health_check(), timeout=self.HEALTH_CHECK_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning(f"Health check timed out for provider {pid}")
                healthy = False
            except Exception as e:
                logger.warning(f"Health check failed for provider {pid}: {e}")
                healthy = False

            usage_pct = 0.0
            try:
                usage = await asyncio.wait_for(
                    provider.get_usage(), timeout=self.HEALTH_CHECK_TIMEOUT
                )
                if usage.get("quota_bytes", 0) > 0:
                    usage_pct = usage["used_bytes"] / usage["quota_bytes"]
            except Exception as e:
                logger.warning(f"Usage check failed for provider {pid}: {e}")

            return pid, {
                "healthy":     healthy,
                "quarantined": self.is_degraded(pid),
                "usage_pct":   round(usage_pct, 4),
            }

        tasks   = [_one(pid, p) for pid, p in self.providers.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        status: Dict[str, Dict[str, Any]] = {}
        for item in results:
            if isinstance(item, Exception):
                continue
            pid, info = item
            status[pid] = info

        # Surface disabled providers in the health report too
        for pid, reason in self.disabled_providers.items():
            status[pid] = {
                "healthy":     False,
                "quarantined": False,
                "usage_pct":   0.0,
                "disabled":    True,
                "reason":      reason,
            }

        return status

    def available_provider_count(self) -> int:
        now = time.time()
        return sum(
            1 for pid in self.providers if self.degraded_until.get(pid, 0) < now
        )


# Compatibility alias
ProviderPool = ProviderRegistry
