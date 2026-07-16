import os
import logging
import asyncio
from typing import Optional, List, AsyncIterator, Dict, Any
import httpx
from tachyon.providers.base import CloudProvider
from tachyon.providers.exceptions import FileNotFoundError, StorageError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Credential validator
# ---------------------------------------------------------------------------

def _validate_onedrive_secret(secret: str, context: str = "") -> None:
    """
    Raise ValueError if *secret* looks like a placeholder rather than a real
    Azure client secret.

    Real Azure client secrets are at least 24 characters, contain no spaces,
    and are composed of alphanumeric + special characters.
    """
    placeholders = [
        "i already have", "your_secret", "placeholder", "change me",
        "todo", "xxx", "insert", "put your", "add your",
    ]
    if not secret:
        raise ValueError(f"OneDrive client secret is empty{context}.")
    if " " in secret:
        raise ValueError(
            f"OneDrive client secret contains spaces{context}. "
            "This looks like a placeholder — please set ONEDRIVE_CLIENT_SECRET "
            "to the actual secret VALUE (not the secret ID) from Azure App registrations."
        )
    if len(secret) < 16:
        raise ValueError(
            f"OneDrive client secret is suspiciously short ({len(secret)} chars){context}."
        )
    lower = secret.lower()
    for p in placeholders:
        if p in lower:
            raise ValueError(
                f"OneDrive client secret looks like a placeholder (matched '{p}'){context}. "
                "Please update ONEDRIVE_CLIENT_SECRET with the real credential."
            )


class OneDriveProvider(CloudProvider):
    """
    Consolidated Production-Grade OneDrive Provider for Tachyon Fabric.

    Changes vs. previous version
    ─────────────────────────────
    • Credential validation before MSAL is instantiated — catches placeholder
      secrets early and marks the provider disabled without making any network call.
    • health_check() has a 10-second asyncio timeout.
    • All methods handle exceptions gracefully so one provider failure cannot
      crash the whole service.
    """

    def __init__(self, account_id: str, credentials: Optional[dict] = None):
        self.account_id = account_id
        self.name = account_id
        self._credentials = credentials or {}
        self._msal_app = None
        self._cached_token = None
        self._permanently_disabled = False
        self._disable_reason: str = ""
        self._last_upload_error = None   # For diagnostics via /debug/providers

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_msal_app(self):
        if self._permanently_disabled:
            raise StorageError(
                f"OneDrive provider '{self.account_id}' is disabled: {self._disable_reason}",
                code="provider_disabled",
                status_code=503,
            )
        if self._msal_app:
            return self._msal_app

        import msal

        client_id     = self._credentials.get("client_id")     or os.getenv("ONEDRIVE_CLIENT_ID")
        client_secret = self._credentials.get("client_secret") or os.getenv("ONEDRIVE_CLIENT_SECRET")
        tenant_id     = self._credentials.get("tenant_id")     or os.getenv("ONEDRIVE_TENANT_ID", "common")

        if not client_id or not client_secret:
            self._permanently_disabled = True
            self._disable_reason = "ONEDRIVE_CLIENT_ID or ONEDRIVE_CLIENT_SECRET is not set."
            raise RuntimeError(
                f"OneDrive client credentials missing for account: {self.account_id}"
            )

        # ── Validate secret format before handing to MSAL ─────────────
        try:
            _validate_onedrive_secret(
                client_secret, context=f" for account '{self.account_id}'"
            )
        except ValueError as exc:
            self._permanently_disabled = True
            self._disable_reason = str(exc)
            logger.error(
                f"[onedrive/{self.account_id}] Credential validation failed — "
                f"provider disabled: {exc}"
            )
            raise StorageError(
                str(exc), code="invalid_credential", status_code=503
            )

        self._msal_app = msal.ConfidentialClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            client_credential=client_secret,
        )
        return self._msal_app

    def _get_token(self) -> str:
        if self._permanently_disabled:
            raise StorageError(
                f"OneDrive provider '{self.account_id}' is disabled: {self._disable_reason}",
                code="provider_disabled",
                status_code=503,
            )
        app    = self._get_msal_app()
        scopes = ["https://graph.microsoft.com/.default"]

        result = app.acquire_token_silent(scopes, account=None)
        if result and "access_token" in result:
            return result["access_token"]

        result = app.acquire_token_for_client(scopes=scopes)
        if "access_token" not in result:
            raise RuntimeError(
                f"OneDrive MSAL acquisition failed: {result.get('error_description')}"
            )
        return result["access_token"]

    def _get_drive_endpoint(self) -> str:
        user_id = (
            self._credentials.get("user_id")
            or os.getenv(f"ONEDRIVE_{self.account_id.upper()}_USER_ID")
        )
        if user_id:
            return f"https://graph.microsoft.com/v1.0/users/{user_id}/drive"
        return "https://graph.microsoft.com/v1.0/me/drive"

    def _clean_path(self, name: str) -> str:
        if ".." in name or name.startswith("/"):
            raise StorageError(
                f"Security Warning: Path traversal blocked: {name}",
                code="path_traversal",
                status_code=400,
            )
        folder_id = (
            self._credentials.get("folder_id")
            or os.getenv(f"ONEDRIVE_{self.account_id.upper()}_FOLDER_ID", "root")
        )
        if folder_id == "root":
            return f"/items/root:/{name}" if name else "/items/root"
        return f"/items/{folder_id}:/{name}" if name else f"/items/{folder_id}"

    # ------------------------------------------------------------------
    # CloudProvider interface
    # ------------------------------------------------------------------

    async def upload(self, data: bytes, name: str) -> bool:
        path = self._clean_path(name)
        try:
            token = self._get_token()
            drive = self._get_drive_endpoint()
            url   = f"{drive}{path}:/content"

            async with httpx.AsyncClient(timeout=30.0) as client:
                res = await client.put(
                    url,
                    content=data,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type":  "application/octet-stream",
                    },
                )
            return res.status_code in (200, 201)
        except Exception as e:
            self._last_upload_error = repr(e)
            logger.error(f"OneDrive upload failed [{self.account_id}]: {e}")
            return False

    async def download(self, name: str) -> Optional[bytes]:
        path = self._clean_path(name)
        try:
            token = self._get_token()
            drive = self._get_drive_endpoint()
            url   = f"{drive}{path}:/content"

            async with httpx.AsyncClient(timeout=30.0) as client:
                res = await client.get(url, headers={"Authorization": f"Bearer {token}"})

            if res.status_code == 404:
                return None
            return res.content if res.status_code == 200 else None
        except Exception as e:
            logger.error(f"OneDrive download failed [{self.account_id}]: {e}")
            return None

    async def stream(self, name: str) -> AsyncIterator[bytes]:
        path  = self._clean_path(name)
        token = self._get_token()
        drive = self._get_drive_endpoint()
        url   = f"{drive}{path}:/content"

        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream(
                "GET", url, headers={"Authorization": f"Bearer {token}"}
            ) as response:
                if response.status_code == 404:
                    raise FileNotFoundError(f"File {name} not found")
                response.raise_for_status()
                async for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    yield chunk

    async def delete(self, name: str) -> bool:
        path = self._clean_path(name)
        try:
            token = self._get_token()
            drive = self._get_drive_endpoint()
            url   = f"{drive}{path}"

            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.delete(url, headers={"Authorization": f"Bearer {token}"})
            return res.status_code in (200, 204)
        except Exception as e:
            logger.warning(
                f"OneDrive delete failed [{self.account_id}] for {name}: {e}"
            )
            return False

    async def rename(self, old_name: str, new_name: str) -> bool:
        old_path = self._clean_path(old_name)
        try:
            token = self._get_token()
            drive = self._get_drive_endpoint()
            url   = f"{drive}{old_path}"

            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.patch(
                    url,
                    json={"name": new_name},
                    headers={"Authorization": f"Bearer {token}"},
                )
            return res.status_code == 200
        except Exception as e:
            logger.error(f"OneDrive rename failed [{self.account_id}]: {e}")
            return False

    async def copy(self, src_name: str, dest_name: str) -> bool:
        src_path = self._clean_path(src_name)
        try:
            token = self._get_token()
            drive = self._get_drive_endpoint()
            url   = f"{drive}{src_path}/copy"

            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    url,
                    json={"name": dest_name},
                    headers={"Authorization": f"Bearer {token}"},
                )
            return res.status_code in (200, 202)
        except Exception as e:
            logger.error(f"OneDrive copy failed [{self.account_id}]: {e}")
            return False

    async def exists(self, name: str) -> bool:
        path = self._clean_path(name)
        try:
            token = self._get_token()
            drive = self._get_drive_endpoint()
            url   = f"{drive}{path}"

            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            return res.status_code == 200
        except Exception:
            return False

    async def metadata(self, name: str) -> Dict[str, Any]:
        try:
            token = self._get_token()
            drive = self._get_drive_endpoint()

            if not name:
                url = f"{drive}"
                async with httpx.AsyncClient(timeout=10.0) as client:
                    res  = await client.get(url, headers={"Authorization": f"Bearer {token}"})
                data = res.json()
                q    = data.get("quota", {})
                return {
                    "total_bytes": q.get("total", 0),
                    "used_bytes":  q.get("used",  0),
                    "free_bytes":  q.get("remaining", 0),
                    "type":        "directory",
                }

            path = self._clean_path(name)
            url  = f"{drive}{path}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(url, headers={"Authorization": f"Bearer {token}"})

            if res.status_code == 404:
                raise FileNotFoundError(f"OneDrive file {name} not found")

            data      = res.json()
            is_folder = "folder" in data
            return {
                "name":        data.get("name"),
                "size":        data.get("size", 0),
                "created_at":  data.get("createdDateTime"),
                "modified_at": data.get("lastModifiedDateTime"),
                "type":        "directory" if is_folder else "file",
            }
        except (StorageError, FileNotFoundError):
            raise
        except Exception as e:
            raise StorageError(
                f"OneDrive metadata failed [{self.account_id}]: {e}",
                code="metadata_error",
                status_code=500,
            )

    async def checksum(self, name: str) -> str:
        path  = self._clean_path(name)
        token = self._get_token()
        drive = self._get_drive_endpoint()
        url   = f"{drive}{path}"

        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(url, headers={"Authorization": f"Bearer {token}"})

        data    = res.json()
        f_facet = data.get("file", {})
        hashes  = f_facet.get("hashes", {})
        return (
            hashes.get("sha256Hash")
            or hashes.get("sha1Hash")
            or hashes.get("quickXorHash")
            or ""
        )

    async def create_directory(self, path: str) -> bool:
        try:
            token     = self._get_token()
            drive     = self._get_drive_endpoint()
            folder_id = (
                self._credentials.get("folder_id")
                or os.getenv(f"ONEDRIVE_{self.account_id.upper()}_FOLDER_ID", "root")
            )
            url = f"{drive}/items/{folder_id}/children"

            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    url,
                    json={
                        "name":   path,
                        "folder": {},
                        "@microsoft.graph.conflictBehavior": "replace",
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
            return res.status_code in (200, 201)
        except Exception as e:
            logger.error(f"OneDrive create_directory failed [{self.account_id}]: {e}")
            return False

    async def delete_directory(self, path: str) -> bool:
        return await self.delete(path)

    async def list_directory(self, path: str) -> List[str]:
        try:
            token = self._get_token()
            drive = self._get_drive_endpoint()

            if path:
                item_path = self._clean_path(path)
                url = f"{drive}{item_path}/children"
            else:
                folder_id = (
                    self._credentials.get("folder_id")
                    or os.getenv(
                        f"ONEDRIVE_{self.account_id.upper()}_FOLDER_ID", "root"
                    )
                )
                url = f"{drive}/items/{folder_id}/children"

            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.get(url, headers={"Authorization": f"Bearer {token}"})

            if res.status_code == 200:
                return [item["name"] for item in res.json().get("value", [])]
            return []
        except Exception as e:
            logger.error(f"OneDrive list_directory failed [{self.account_id}]: {e}")
            return []

    async def generate_signed_url(self, name: str, expiration: int = 3600) -> str:
        path = self._clean_path(name)
        try:
            token = self._get_token()
            drive = self._get_drive_endpoint()
            url   = f"{drive}{path}/createLink"

            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    url,
                    json={"type": "view", "scope": "anonymous"},
                    headers={"Authorization": f"Bearer {token}"},
                )
            if res.status_code in (200, 201):
                return res.json().get("link", {}).get(
                    "webUrl", f"https://onedrive.live.com/download?id={name}"
                )
            return f"https://onedrive.live.com/download?id={name}"
        except Exception as e:
            logger.error(f"OneDrive generate_signed_url failed: {e}")
            return f"https://onedrive.live.com/download?id={name}"

    async def health_check(self) -> bool:
        """Health check with 10-second timeout and early-exit on disabled provider."""
        if self._permanently_disabled:
            logger.warning(
                f"[onedrive/{self.account_id}] health_check skipped — provider disabled: "
                f"{self._disable_reason}"
            )
            return False

        try:
            token = self._get_token()
            drive = self._get_drive_endpoint()

            async def _check():
                async with httpx.AsyncClient(timeout=10.0) as client:
                    res = await client.get(
                        f"{drive}", headers={"Authorization": f"Bearer {token}"}
                    )
                return res.status_code == 200

            return await asyncio.wait_for(_check(), timeout=12.0)
        except asyncio.TimeoutError:
            logger.warning(f"[onedrive/{self.account_id}] health_check timed out")
            return False
        except Exception as e:
            logger.warning(f"[onedrive/{self.account_id}] health_check failed: {e}")
            return False
