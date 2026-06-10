import os
import io
import json
import asyncio
import logging
from typing import Optional
from tachyon.providers.base import CloudProvider

logger = logging.getLogger(__name__)

class GoogleDriveProvider(CloudProvider):
    """Google Drive provider using service account credentials."""

    def __init__(self, account_id: str, folder_id: Optional[str] = None):
        self.account_id = account_id
        self.folder_id = folder_id or os.getenv(f"GDRIVE_{account_id.upper()}_FOLDER_ID")
        self._service = None

    def _get_service(self):
        if self._service:
            return self._service
        sa_json = os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON")
        if not sa_json:
            raise RuntimeError("GDRIVE_SERVICE_ACCOUNT_JSON env var not set")
        import google.auth
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds_info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(
            creds_info,
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._service

    async def upload_fragment(self, data: bytes, name: str) -> bool:
        try:
            svc = self._get_service()
            from googleapiclient.http import MediaIoBaseUpload
            loop = asyncio.get_event_loop()
            def _upload():
                media = MediaIoBaseUpload(io.BytesIO(data), mimetype="application/octet-stream")
                meta = {"name": name}
                if self.folder_id:
                    meta["parents"] = [self.folder_id]
                svc.files().create(body=meta, media_body=media, fields="id").execute()
            await loop.run_in_executor(None, _upload)
            return True
        except Exception as e:
            logger.error(f"GDrive upload failed [{self.account_id}]: {e}")
            return False

    async def download_fragment(self, name: str) -> Optional[bytes]:
        try:
            svc = self._get_service()
            loop = asyncio.get_event_loop()
            def _download():
                from googleapiclient.http import MediaIoBaseDownload
                results = svc.files().list(q=f"name='{name}'", fields="files(id)").execute()
                files = results.get("files", [])
                if not files:
                    return None
                fid = files[0]["id"]
                buf = io.BytesIO()
                req = svc.files().get_media(fileId=fid)
                dl = MediaIoBaseDownload(buf, req)
                done = False
                while not done:
                    _, done = dl.next_chunk()
                return buf.getvalue()
            return await loop.run_in_executor(None, _download)
        except Exception as e:
            logger.error(f"GDrive download failed [{self.account_id}]: {e}")
            return None

    async def get_quota(self) -> dict:
        try:
            svc = self._get_service()
            loop = asyncio.get_event_loop()
            about = await loop.run_in_executor(None, lambda: svc.about().get(fields="storageQuota").execute())
            q = about.get("storageQuota", {})
            return {"total": int(q.get("limit", 0)), "used": int(q.get("usage", 0))}
        except Exception:
            return {"total": 0, "used": 0}

    async def get_latency(self) -> float:
        import time
        t = time.monotonic()
        try:
            svc = self._get_service()
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: svc.about().get(fields="user").execute())
        except Exception:
            pass
        return (time.monotonic() - t) * 1000
