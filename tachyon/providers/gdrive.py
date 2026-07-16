import os
import io
import json
import base64
import logging
import asyncio
from typing import Optional, List, AsyncIterator, Dict, Any
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as OAuthCredentials

from tachyon.providers.base import CloudProvider
from tachyon.providers.exceptions import FileNotFoundError, StorageError

logger = logging.getLogger(__name__)

# Sentinel: folder lookup was attempted but failed; use Drive root instead
_GDRIVE_ROOT_SENTINEL = "__root__"

class GoogleDriveProvider(CloudProvider):
    """
    Consolidated Production-Grade Google Drive Provider for Tachyon Fabric.
    Combines legacy directory memoization cache and service discovery suppression
    with modern resumable chunk uploads and path-traversal guards.
    """

    def __init__(self, account_id: str, credentials: Optional[dict] = None, folder_id: Optional[str] = None):
        self.account_id = account_id
        self.name = account_id
        self._credentials_dict = credentials
        self._folder_id = folder_id or os.getenv(f"GDRIVE_{account_id.upper()}_FOLDER_ID")
        self._service = None
        self._name_to_id = {} # Memoization lookup cache for files

    def _get_service(self):
        if self._service:
            return self._service

        sa_json = None
        if self._credentials_dict:
            sa_json = self._credentials_dict
        else:
            env_val = os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON")
            if env_val:
                env_val = env_val.strip()
                if env_val.startswith("{"):
                    sa_json = json.loads(env_val)
                else:
                    try:
                        sa_json = json.loads(base64.b64decode(env_val).decode("utf-8"))
                    except Exception:
                        if os.path.exists(env_val):
                            with open(env_val, "r") as f:
                                sa_json = json.load(f)
                        else:
                            raise RuntimeError(f"GDRIVE_SERVICE_ACCOUNT_JSON is not a valid file, raw JSON, or Base64 string")

        if not sa_json:
            raise RuntimeError("Google Drive credentials not set")

        if "type" in sa_json and sa_json["type"] == "service_account":
            creds = service_account.Credentials.from_service_account_info(
                sa_json,
                scopes=["https://www.googleapis.com/auth/drive"]
            )
        else:
            creds = OAuthCredentials.from_authorized_user_info(
                sa_json,
                scopes=["https://www.googleapis.com/auth/drive"]
            )

        # cache_discovery=False avoids calls to standard web GAPI discovery endpoint (saves >150ms)
        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._service

    async def _get_folder_id(self) -> str:
        """
        Returns the Drive folder ID for VIT shard storage.
        Falls back gracefully to None (Drive root) if the folder cannot be found
        or created — avoiding a RuntimeError that would quarantine this provider.
        Uses a sentinel so repeated calls don't re-attempt after a failure.
        """
        if self._folder_id == _GDRIVE_ROOT_SENTINEL:
            return None   # previous attempt failed; use Drive root
        if self._folder_id:
            return self._folder_id

        service = self._get_service()
        query = ("name = 'tachyon_fragments' and "
                 "mimeType = 'application/vnd.google-apps.folder' and trashed = false")
        try:
            def _list():
                return service.files().list(q=query, fields="files(id)").execute()

            results = await asyncio.wait_for(asyncio.to_thread(_list), timeout=10.0)
            files = results.get("files", [])

            if files:
                self._folder_id = files[0]["id"]
            else:
                file_metadata = {
                    "name": "tachyon_fragments",
                    "mimeType": "application/vnd.google-apps.folder",
                }

                def _create():
                    return service.files().create(body=file_metadata, fields="id").execute()

                folder = await asyncio.wait_for(asyncio.to_thread(_create), timeout=10.0)
                self._folder_id = folder.get("id") or _GDRIVE_ROOT_SENTINEL

        except Exception as exc:
            logger.warning(
                "[%s] Google Drive folder setup failed: %s — "
                "shards will be written to Drive root (no parent folder).",
                self.account_id, exc,
            )
            self._folder_id = _GDRIVE_ROOT_SENTINEL

        return None if self._folder_id == _GDRIVE_ROOT_SENTINEL else self._folder_id

    def _check_name(self, name: str) -> str:
        if ".." in name or name.startswith("/"):
            raise StorageError(f"Security Warning: Path traversal blocked: {name}", code="path_traversal", status_code=400)
        return name

    async def _resolve_file_id(self, name: str) -> str:
        self._check_name(name)
        if name in self._name_to_id:
            return self._name_to_id[name]

        service = self._get_service()
        fid = await self._get_folder_id()
        if fid:
            query = f"name = '{name}' and '{fid}' in parents and trashed = false"
        else:
            query = f"name = '{name}' and trashed = false"

        def _search():
            return service.files().list(q=query, fields="files(id)").execute()

        res = await asyncio.to_thread(_search)
        files = res.get("files", [])
        if not files:
            raise FileNotFoundError(f"File {name} not found in Google Drive")

        file_id = files[0]["id"]
        self._name_to_id[name] = file_id
        return file_id

    async def upload(self, data: bytes, name: str) -> bool:
        self._check_name(name)
        try:
            service = self._get_service()
            folder_id = await self._get_folder_id()

            # Check if file already exists to overwrite
            try:
                existing_id = await self._resolve_file_id(name)
                # If exists, delete old one first
                def _del():
                    service.files().delete(fileId=existing_id).execute()
                await asyncio.to_thread(_del)
                self._name_to_id.pop(name, None)
            except FileNotFoundError:
                pass

            file_metadata = {"name": name}
            if folder_id:
                file_metadata["parents"] = [folder_id]

            media = MediaIoBaseUpload(
                io.BytesIO(data),
                mimetype="application/octet-stream",
                resumable=len(data) > 5 * 1024 * 1024 # Resumable for files > 5MB
            )

            def _upload():
                return service.files().create(
                    body=file_metadata,
                    media_body=media,
                    fields="id"
                ).execute()

            file_res = await asyncio.to_thread(_upload)
            self._name_to_id[name] = file_res.get("id")
            return True
        except Exception as e:
            logger.error(f"Google Drive upload failed [{self.account_id}]: {e}")
            return False

    async def download(self, name: str) -> Optional[bytes]:
        try:
            file_id = await self._resolve_file_id(name)
            service = self._get_service()

            def _dl():
                request = service.files().get_media(fileId=file_id)
                buf = io.BytesIO()
                downloader = MediaIoBaseDownload(buf, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                return buf.getvalue()

            return await asyncio.to_thread(_dl)
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.error(f"Google Drive download failed [{self.account_id}]: {e}")
            return None

    async def stream(self, name: str) -> AsyncIterator[bytes]:
        file_id = await self._resolve_file_id(name)
        service = self._get_service()

        # Simple chunk stream over GDrive HTTP GET request
        # Google API client does not provide full native async stream, so we simulate using threadpool chunk reads.
        request = service.files().get_media(fileId=file_id)

        # Download in 1MB chunks
        chunk_size = 1024 * 1024
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request, chunksize=chunk_size)

        done = False
        last_offset = 0
        while not done:
            def _next():
                return downloader.next_chunk()
            status, done = await asyncio.to_thread(_next)
            buf.seek(last_offset)
            chunk = buf.read()
            last_offset = buf.tell()
            if chunk:
                yield chunk

    async def delete(self, name: str) -> bool:
        try:
            file_id = await self._resolve_file_id(name)
            service = self._get_service()

            def _del():
                service.files().delete(fileId=file_id).execute()

            await asyncio.to_thread(_del)
            self._name_to_id.pop(name, None)
            return True
        except FileNotFoundError:
            return True # idempotency
        except Exception as e:
            logger.error(f"Google Drive delete failed [{self.account_id}]: {e}")
            return False

    async def rename(self, old_name: str, new_name: str) -> bool:
        self._check_name(new_name)
        try:
            file_id = await self._resolve_file_id(old_name)
            service = self._get_service()

            def _ren():
                return service.files().update(
                    fileId=file_id,
                    body={"name": new_name},
                    fields="id"
                ).execute()

            await asyncio.to_thread(_ren)
            self._name_to_id.pop(old_name, None)
            self._name_to_id[new_name] = file_id
            return True
        except Exception as e:
            logger.error(f"Google Drive rename failed [{self.account_id}]: {e}")
            return False

    async def copy(self, src_name: str, dest_name: str) -> bool:
        self._check_name(dest_name)
        try:
            src_id = await self._resolve_file_id(src_name)
            service = self._get_service()
            folder_id = await self._get_folder_id()

            def _cp():
                body = {
                    "name": dest_name,
                    "parents": [folder_id]
                }
                return service.files().copy(fileId=src_id, body=body, fields="id").execute()

            res = await asyncio.to_thread(_cp)
            self._name_to_id[dest_name] = res.get("id")
            return True
        except Exception as e:
            logger.error(f"Google Drive copy failed [{self.account_id}]: {e}")
            return False

    async def exists(self, name: str) -> bool:
        try:
            await self._resolve_file_id(name)
            return True
        except Exception:
            return False

    async def metadata(self, name: str) -> Dict[str, Any]:
        service = self._get_service()
        if not name:
            # Fetch storage quota
            def _quota():
                return service.about().get(fields="storageQuota").execute()
            res = await asyncio.to_thread(_quota)
            q = res.get("storageQuota", {})
            limit = int(q.get("limit", 0))
            usage = int(q.get("usage", 0))
            return {
                "total_bytes": limit or 15 * 1024**3,
                "used_bytes": usage,
                "free_bytes": max(0, (limit or 15 * 1024**3) - usage),
                "type": "directory"
            }

        file_id = await self._resolve_file_id(name)
        def _get():
            return service.files().get(fileId=file_id, fields="id, name, size, createdTime, modifiedTime, mimeType").execute()

        res = await asyncio.to_thread(_get)
        is_folder = res.get("mimeType") == "application/vnd.google-apps.folder"
        return {
            "name": res.get("name"),
            "size": int(res.get("size", 0)) if not is_folder else 0,
            "created_at": res.get("createdTime"),
            "modified_at": res.get("modifiedTime"),
            "type": "directory" if is_folder else "file"
        }

    async def checksum(self, name: str) -> str:
        file_id = await self._resolve_file_id(name)
        service = self._get_service()
        def _get():
            return service.files().get(fileId=file_id, fields="md5Checksum").execute()
        res = await asyncio.to_thread(_get)
        return res.get("md5Checksum", "")

    async def create_directory(self, path: str) -> bool:
        self._check_name(path)
        try:
            service = self._get_service()
            parent_id = await self._get_folder_id()

            # Check if directory already exists
            query = f"name = '{path}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            def _find():
                return service.files().list(q=query, fields="files(id)").execute()
            res = await asyncio.to_thread(_find)
            if res.get("files"):
                return True

            file_metadata = {
                "name": path,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id]
            }
            def _create():
                return service.files().create(body=file_metadata, fields="id").execute()
            await asyncio.to_thread(_create)
            return True
        except Exception as e:
            logger.error(f"Google Drive create_directory failed [{self.account_id}]: {e}")
            return False

    async def delete_directory(self, path: str) -> bool:
        # Same as deleting file or directory since SAPI files().delete deletes folders and kids recursive
        return await self.delete(path)

    async def list_directory(self, path: str) -> List[str]:
        try:
            service = self._get_service()
            if path:
                # Find the directory ID first
                dir_id = await self._resolve_file_id(path)
            else:
                dir_id = await self._get_folder_id()

            query = f"'{dir_id}' in parents and trashed = false"
            def _list():
                return service.files().list(q=query, fields="files(name)").execute()
            res = await asyncio.to_thread(_list)
            return [f["name"] for f in res.get("files", [])]
        except Exception as e:
            logger.error(f"Google Drive list_directory failed [{self.account_id}]: {e}")
            return []

    async def generate_signed_url(self, name: str, expiration: int = 3600) -> str:
        # Google Drive doesn't have standard presigned URLs like S3, so we use public webContentLink / webViewLink
        file_id = await self._resolve_file_id(name)
        service = self._get_service()
        def _get():
            return service.files().get(fileId=file_id, fields="webContentLink").execute()
        res = await asyncio.to_thread(_get)
        return res.get("webContentLink", f"https://drive.google.com/uc?id={file_id}&export=download")

    async def health_check(self) -> bool:
        try:
            service = self._get_service()
            def _ping():
                return service.files().list(pageSize=1, fields="files(id)").execute()
            await asyncio.to_thread(_ping)
            return True
        except Exception:
            return False
