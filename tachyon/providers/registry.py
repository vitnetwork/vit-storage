import os
import json
import time
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
    Manages provider bootup, automatic capabilities discovery, health status,
    quota threshold guards, and timeout-based quarantining.
    """

    QUOTA_GUARD_PCT = 0.90
    DEGRADED_TIMEOUT_SECONDS = 600

    def __init__(self):
        self.providers: Dict[str, CloudProvider] = {}
        self.degraded_until: Dict[str, float] = {} # provider_id -> timestamp
        self.usage_cache: Dict[str, Dict[str, Any]] = {} # provider_id -> cache_dict
        self._current_index = 0
        self._bootstrap_providers()

    def register(self, provider_id: str, provider: CloudProvider):
        """Manually register a provider instance."""
        self.providers[provider_id] = provider
        logger.info(f"Registered provider: {provider_id} ({type(provider).__name__})")

    def _bootstrap_providers(self):
        """Automatically discover and configure storage providers based on env vars."""
        logger.info("Bootstrapping storage providers from environment...")

        # 1. Local Disk Provider (Always registered as fallback/test backend)
        try:
            disk_path = os.getenv("TACHYON_STORAGE_PATH", "/tmp/tachyon_storage")
            self.register("local_disk", DiskProvider("local_disk", storage_path=disk_path))
        except Exception as e:
            logger.error(f"Failed to bootstrap local disk provider: {e}")

        # 2. Google Drive Providers
        # Multiple accounts from GDRIVE_SERVICE_ACCOUNT_KEYS
        gdrive_keys_raw = os.getenv("GDRIVE_SERVICE_ACCOUNT_KEYS", "[]")
        try:
            gdrive_keys = json.loads(gdrive_keys_raw)
            for i, key in enumerate(gdrive_keys):
                pid = f"gdrive_sa_{i}"
                self.register(pid, GoogleDriveProvider(pid, credentials=key))
        except Exception as e:
            logger.error(f"Failed to load GDRIVE_SERVICE_ACCOUNT_KEYS: {e}")

        # Single account from GDRIVE_SERVICE_ACCOUNT_JSON
        gdrive_sa_single = os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON")
        if gdrive_sa_single:
            try:
                self.register("gdrive_sa_main", GoogleDriveProvider("gdrive_sa_main"))
            except Exception as e:
                logger.error(f"Failed to load main Google Drive SA provider: {e}")

        # 3. OneDrive Providers
        onedrive_accounts_raw = os.getenv("ONEDRIVE_ACCOUNTS", "[]")
        try:
            onedrive_accounts = json.loads(onedrive_accounts_raw)
            for i, acc in enumerate(onedrive_accounts):
                pid = f"onedrive_{i}"
                self.register(pid, OneDriveProvider(pid, credentials=acc))
        except Exception as e:
            logger.error(f"Failed to load ONEDRIVE_ACCOUNTS: {e}")

        # Single account OneDrive from discrete env variables
        od_client_id = os.getenv("ONEDRIVE_CLIENT_ID")
        od_secret = os.getenv("ONEDRIVE_CLIENT_SECRET")
        if od_client_id and od_secret:
            try:
                self.register("onedrive_main", OneDriveProvider("onedrive_main"))
            except Exception as e:
                logger.error(f"Failed to load main OneDrive provider: {e}")

        # 4. Dropbox Providers
        dropbox_tokens_raw = os.getenv("DROPBOX_TOKENS", "[]")
        try:
            dropbox_tokens = json.loads(dropbox_tokens_raw)
            for i, token in enumerate(dropbox_tokens):
                pid = f"dropbox_{i}"
                self.register(pid, DropboxProvider(pid, credentials={"access_token": token}))
        except Exception as e:
            logger.error(f"Failed to load DROPBOX_TOKENS: {e}")

        # Single Dropbox account from discrete env variables
        db_token = os.getenv("DROPBOX_ACCESS_TOKEN")
        db_refresh = os.getenv("DROPBOX_REFRESH_TOKEN")
        if db_token or db_refresh:
            try:
                self.register("dropbox_main", DropboxProvider("dropbox_main"))
            except Exception as e:
                logger.error(f"Failed to load main Dropbox provider: {e}")

        # 5. S3 Compatible Providers
        s3_key = os.getenv("S3_ACCESS_KEY_ID")
        s3_secret = os.getenv("S3_SECRET_ACCESS_KEY")
        if s3_key and s3_secret:
            try:
                self.register("s3_main", S3Provider("s3_main"))
            except Exception as e:
                logger.error(f"Failed to load main S3 provider: {e}")

        # 6. Object Storage (R2/MinIO/B2)
        obj_key = os.getenv("OBJECT_STORAGE_ACCESS_KEY")
        obj_secret = os.getenv("OBJECT_STORAGE_SECRET_KEY")
        if obj_key and obj_secret:
            try:
                creds = {
                    "access_key": obj_key,
                    "secret_key": obj_secret,
                    "endpoint_url": os.getenv("OBJECT_STORAGE_ENDPOINT"),
                    "bucket_name": os.getenv("OBJECT_STORAGE_BUCKET")
                }
                self.register("object_storage_main", ObjectStorageProvider("object_storage_main", credentials=creds))
            except Exception as e:
                logger.error(f"Failed to load main Object Storage provider: {e}")

        logger.info(f"Loaded total of {len(self.providers)} providers into Registry.")

    def get_provider(self, provider_id: str) -> Optional[CloudProvider]:
        """Fetch provider by registration ID."""
        return self.providers.get(provider_id)

    def list_providers(self, active_only: bool = False) -> List[CloudProvider]:
        """List active/all providers."""
        if not active_only:
            return list(self.providers.values())

        now = time.time()
        active = []
        for pid, provider in self.providers.items():
            if self.degraded_until.get(pid, 0) < now:
                active.append(provider)
        return active

    def discover_capabilities(self) -> Dict[str, List[str]]:
        """Map of provider ID to list of its custom capabilities (e.g. streaming, presigned URLs)."""
        caps = {}
        for pid, p in self.providers.items():
            plist = ["upload", "download", "delete", "exists", "metadata", "health_check"]

            # Capability tagging based on class
            if isinstance(p, (DiskProvider, S3Provider, ObjectStorageProvider)):
                plist.append("signed_urls")
                plist.append("directories")
            if isinstance(p, (DiskProvider, S3Provider, ObjectStorageProvider, OneDriveProvider)):
                plist.append("streaming")

            caps[pid] = plist
        return caps

    def is_degraded(self, provider_id: str) -> bool:
        """Check if provider is currently quarantined."""
        until = self.degraded_until.get(provider_id, 0)
        return time.time() < until

    async def is_full(self, provider_id: str) -> bool:
        """Check if provider storage exceeds the Quota Guard threshold."""
        provider = self.get_provider(provider_id)
        if not provider:
            return True

        now = time.time()
        # Cache usage details for 5 minutes
        if provider_id in self.usage_cache and (now - self.usage_cache[provider_id]["timestamp"]) < 300:
            usage = self.usage_cache[provider_id]
        else:
            try:
                # Compatibility wrap
                quota = await provider.get_usage()
                usage = {
                    "used_bytes": quota.get("used_bytes", 0),
                    "quota_bytes": quota.get("quota_bytes", 10 * 1024**3),
                    "timestamp": now
                }
                self.usage_cache[provider_id] = usage
            except Exception:
                usage = {"used_bytes": 0, "quota_bytes": 10 * 1024**3, "timestamp": now}

        if usage["quota_bytes"] > 0:
            pct = usage["used_bytes"] / usage["quota_bytes"]
            return pct > self.QUOTA_GUARD_PCT
        return False

    async def upload_shard(self, shard_id: str, data: bytes) -> Tuple[str, str]:
        """
        Uploads a shard to the first available non-degraded provider in round-robin sequence.
        Quarantines providers on upload failure.
        """
        plist = list(self.providers.values())
        if not plist:
            raise RuntimeError("No storage providers registered in Tachyon Registry!")

        total_providers = len(plist)
        for _ in range(total_providers):
            # Select next in round-robin
            provider = plist[self._current_index % total_providers]
            self._current_index += 1

            pid = provider.account_id

            # Skip if quarantined or full
            if self.is_degraded(pid) or await self.is_full(pid):
                continue

            try:
                # Call standardized upload_shard
                file_id = await provider.upload_shard(shard_id, data)
                return pid, file_id
            except Exception as e:
                logger.error(f"Provider {pid} failed upload. Quarantining for 10 minutes. Error: {e}")
                self.degraded_until[pid] = time.time() + self.DEGRADED_TIMEOUT_SECONDS
                continue

        # Last resort fallback: local disk even if degraded/full
        try:
            fallback = self.get_provider("local_disk")
            if fallback:
                logger.warning("All primary cloud providers quarantined! Failing over to local_disk.")
                file_id = await fallback.upload_shard(shard_id, data)
                return "local_disk", file_id
        except Exception as e:
            logger.error(f"Fallback to local disk failed: {e}")

        raise RuntimeError("No available storage providers and local disk fallback failed!")

    async def download_shard(self, provider_id: str, file_id: str) -> bytes:
        """Download a shard directly from the specific provider."""
        provider = self.get_provider(provider_id)
        if not provider:
            raise RuntimeError(f"Provider {provider_id} not registered")
        try:
            return await provider.download_shard(file_id)
        except Exception as e:
            logger.error(f"Download failed from provider {provider_id}: {e}")
            # Quarantine on failure
            self.degraded_until[provider_id] = time.time() + self.DEGRADED_TIMEOUT_SECONDS
            raise

    async def delete_shard(self, provider_id: str, file_id: str) -> bool:
        """Delete a shard from the specific provider."""
        provider = self.get_provider(provider_id)
        if not provider:
            return False
        try:
            return await provider.delete_shard(file_id)
        except Exception as e:
            logger.warning(f"Delete failed on provider {provider_id}: {e}")
            return False

    async def health_check(self) -> Dict[str, Dict[str, Any]]:
        """Active diagnostic check over all registered endpoints."""
        status = {}
        for pid, provider in self.providers.items():
            try:
                healthy = await provider.health_check()
            except Exception as e:
                logger.warning(f"Health check failed for provider {pid}: {e}")
                healthy = False

            usage_pct = 0.0
            try:
                usage = await provider.get_usage()
                if usage.get("quota_bytes", 0) > 0:
                    usage_pct = usage["used_bytes"] / usage["quota_bytes"]
            except Exception as e:
                logger.warning(f"Usage check failed for provider {pid}: {e}")

            status[pid] = {
                "healthy": healthy,
                "quarantined": self.is_degraded(pid),
                "usage_pct": round(usage_pct, 4)
            }
        return status

    def available_provider_count(self) -> int:
        """Returns the number of active, non-degraded providers."""
        count = 0
        now = time.time()
        for pid in self.providers:
            if self.is_degraded(pid):
                continue
            # Non quarantined count
            count += 1
        return count

# Compatibility Alias - Allows seamless drop-in replace for pool.py imports
ProviderPool = ProviderRegistry
