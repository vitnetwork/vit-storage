import os
import asyncio
import logging
from typing import Optional, List
from tachyon.providers.base import CloudProvider

logger = logging.getLogger(__name__)

class OneDriveProvider(CloudProvider):
    """OneDrive provider using Microsoft Graph API (app-only auth)."""

    def __init__(self, account_id: str):
        self.account_id = account_id
        self._token_cache = None

    def _get_token(self) -> str:
        import msal
        client_id = os.getenv("ONEDRIVE_CLIENT_ID")
        client_secret = os.getenv("ONEDRIVE_CLIENT_SECRET")
        tenant_id = os.getenv("ONEDRIVE_TENANT_ID", "common")
        if not all([client_id, client_secret]):
            raise RuntimeError("ONEDRIVE_CLIENT_ID and ONEDRIVE_CLIENT_SECRET must be set")
        app = msal.ConfidentialClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            client_credential=client_secret,
        )
        result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            raise RuntimeError(f"MSAL auth failed: {result.get('error_description')}")
        return result["access_token"]

    async def upload_fragment(self, data: bytes, name: str) -> bool:
        if ".." in name or name.startswith("/"):
             logger.warning(f"Potential path traversal attempt: {name}")
             return False
        try:
            import httpx
            token = self._get_token()
            folder_id = os.getenv(f"ONEDRIVE_{self.account_id.upper()}_FOLDER_ID", "root")
            url = f"https://graph.microsoft.com/v1.0/me/drive/items/{folder_id}:/{name}:/content"
            async with httpx.AsyncClient() as client:
                res = await client.put(url, content=data, headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/octet-stream"
                })
            return res.status_code in (200, 201)
        except Exception as e:
            logger.error(f"OneDrive upload failed [{self.account_id}]: {e}")
            return False

    async def download_fragment(self, name: str) -> Optional[bytes]:
        try:
            import httpx
            token = self._get_token()
            folder_id = os.getenv(f"ONEDRIVE_{self.account_id.upper()}_FOLDER_ID", "root")
            url = f"https://graph.microsoft.com/v1.0/me/drive/items/{folder_id}:/{name}:/content"
            async with httpx.AsyncClient() as client:
                res = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            return res.content if res.status_code == 200 else None
        except Exception as e:
            logger.error(f"OneDrive download failed [{self.account_id}]: {e}")
            return None

    async def delete_fragment(self, name: str) -> bool:
        try:
            import httpx
            token = self._get_token()
            folder_id = os.getenv(f"ONEDRIVE_{self.account_id.upper()}_FOLDER_ID", "root")
            url = f"https://graph.microsoft.com/v1.0/me/drive/items/{folder_id}:/{name}"
            async with httpx.AsyncClient() as client:
                res = await client.delete(url, headers={"Authorization": f"Bearer {token}"})
            return res.status_code == 204
        except Exception as e:
            logger.error(f"OneDrive delete failed [{self.account_id}]: {e}")
            return False

    async def list_fragments(self) -> List[str]:
        try:
            import httpx
            token = self._get_token()
            folder_id = os.getenv(f"ONEDRIVE_{self.account_id.upper()}_FOLDER_ID", "root")
            url = f"https://graph.microsoft.com/v1.0/me/drive/items/{folder_id}/children"
            async with httpx.AsyncClient() as client:
                res = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if res.status_code == 200:
                return [item["name"] for item in res.json().get("value", [])]
            return []
        except Exception as e:
            logger.error(f"OneDrive list failed [{self.account_id}]: {e}")
            return []

    async def get_quota(self) -> dict:
        try:
            import httpx
            token = self._get_token()
            async with httpx.AsyncClient() as client:
                res = await client.get("https://graph.microsoft.com/v1.0/me/drive", headers={"Authorization": f"Bearer {token}"})
            data = res.json()
            q = data.get("quota", {})
            return {"total": q.get("total", 0), "used": q.get("used", 0)}
        except Exception:
            return {"total": 0, "used": 0}

    async def get_latency(self) -> float:
        import time
        t = time.monotonic()
        try:
            import httpx
            token = self._get_token()
            async with httpx.AsyncClient() as client:
                await client.get("https://graph.microsoft.com/v1.0/me/drive", headers={"Authorization": f"Bearer {token}"})
        except Exception as e:
             logger.error(f"OneDrive latency check failed: {e}")
        return (time.monotonic() - t) * 1000
