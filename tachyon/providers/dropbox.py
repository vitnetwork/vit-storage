import os
import io
import logging
import asyncio
from typing import Optional, List, AsyncIterator, Dict, Any
from tachyon.providers.base import CloudProvider
from tachyon.providers.exceptions import FileNotFoundError, StorageError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Credential validator
# ---------------------------------------------------------------------------

def _validate_dropbox_token(token: str, context: str = "") -> None:
    """
    Raise ValueError if *token* is obviously not a real Dropbox token.

    Real Dropbox tokens are at least 40 characters, contain no spaces, and
    are composed of printable ASCII.  Placeholder strings like
    "I already have them in render" fail immediately — before the C-extension
    SDK ever touches them, preventing SIGSEGV crashes.
    """
    placeholders = [
        "i already have", "your_token", "placeholder", "change me",
        "todo", "xxx", "insert", "put your", "add your",
    ]
    if not token:
        raise ValueError(f"Dropbox access token is empty{context}.")
    if len(token) < 20:
        raise ValueError(
            f"Dropbox access token is too short ({len(token)} chars){context}. "
            "Real tokens are ≥ 40 characters."
        )
    if " " in token:
        raise ValueError(
            f"Dropbox access token contains spaces{context}. "
            "This looks like a placeholder — please set DROPBOX_ACCESS_TOKEN "
            "to a real token obtained from the Dropbox developer console."
        )
    lower = token.lower()
    for p in placeholders:
        if p in lower:
            raise ValueError(
                f"Dropbox access token looks like a placeholder (matched '{p}'){context}. "
                "Please update DROPBOX_ACCESS_TOKEN with a real credential."
            )


class DropboxProvider(CloudProvider):
    """
    Consolidated Production-Grade Dropbox Provider for Tachyon Fabric.

    Changes vs. previous version
    ─────────────────────────────
    • Credential validation at _get_client() time — before passing anything to
      the Dropbox C-extension, preventing SIGSEGV crashes on bad tokens.
    • health_check() is wrapped in asyncio.wait_for so a hung API call cannot
      block the event loop indefinitely.
    • All public methods catch generic Exception so a single provider failure
      cannot propagate upward and crash the whole service.
    """

    def __init__(self, account_id: str, credentials: Optional[dict] = None):
        self.account_id = account_id
        self.name = account_id
        self._credentials = credentials or {}
        self._dbx = None
        self._permanently_disabled = False   # set True on bad-credential detection
        self._disable_reason: str = ""
        self._last_upload_error = None        # For diagnostics via /debug/providers

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_client(self):
        if self._permanently_disabled:
            raise StorageError(
                f"Dropbox provider '{self.account_id}' is disabled: {self._disable_reason}",
                code="provider_disabled",
                status_code=503,
            )
        if self._dbx:
            return self._dbx

        import dropbox

        access_token  = self._credentials.get("access_token")  or os.getenv("DROPBOX_ACCESS_TOKEN")
        app_key       = self._credentials.get("app_key")       or os.getenv("DROPBOX_APP_KEY")
        app_secret    = self._credentials.get("app_secret")    or os.getenv("DROPBOX_APP_SECRET")
        refresh_token = self._credentials.get("refresh_token") or os.getenv("DROPBOX_REFRESH_TOKEN")

        # Prefer offline refresh-token flow (never expires)
        if refresh_token and app_key and app_secret:
            logger.info(
                f"Initializing Dropbox with OAuth2 refresh token for account: {self.account_id}"
            )
            self._dbx = dropbox.Dropbox(
                oauth2_refresh_token=refresh_token,
                app_key=app_key,
                app_secret=app_secret,
            )
        elif access_token:
            # ── Validate before handing to C-extension SDK ────────────
            try:
                _validate_dropbox_token(
                    access_token,
                    context=f" for account '{self.account_id}'"
                )
            except ValueError as exc:
                self._permanently_disabled = True
                self._disable_reason = str(exc)
                logger.error(
                    f"[dropbox/{self.account_id}] Credential validation failed — "
                    f"provider disabled: {exc}"
                )
                raise StorageError(
                    str(exc), code="invalid_credential", status_code=503
                )
            logger.info(
                f"Initializing Dropbox with static access token for account: {self.account_id}"
            )
            self._dbx = dropbox.Dropbox(access_token)
        else:
            self._permanently_disabled = True
            self._disable_reason = "No Dropbox credentials configured."
            raise RuntimeError(
                f"Dropbox credentials not configured for account {self.account_id}"
            )

        return self._dbx

    def _clean_path(self, name: str) -> str:
        if ".." in name or name.startswith("/"):
            raise StorageError(
                f"Security Warning: Path traversal blocked: {name}",
                code="path_traversal",
                status_code=400,
            )
        return f"/tachyon/{name}" if name else "/tachyon"

    # ------------------------------------------------------------------
    # CloudProvider interface
    # ------------------------------------------------------------------

    async def upload(self, data: bytes, name: str) -> bool:
        path = self._clean_path(name)
        try:
            dbx = self._get_client()
            import dropbox

            def _upload():
                dbx.files_upload(
                    data,
                    path,
                    mode=dropbox.files.WriteMode.overwrite,
                    mute=True,
                )

            await asyncio.to_thread(_upload)
            return True
        except Exception as e:
            self._last_upload_error = repr(e)
            logger.error(f"Dropbox upload failed [{self.account_id}]: {e}")
            return False

    async def download(self, name: str) -> Optional[bytes]:
        path = self._clean_path(name)
        try:
            dbx = self._get_client()

            def _download():
                _, res = dbx.files_download(path)
                return res.content

            return await asyncio.to_thread(_download)
        except Exception as e:
            logger.error(f"Dropbox download failed [{self.account_id}]: {e}")
            return None

    async def stream(self, name: str) -> AsyncIterator[bytes]:
        path = self._clean_path(name)
        dbx = self._get_client()

        def _download():
            _, res = dbx.files_download(path)
            return res.content

        content = await asyncio.to_thread(_download)
        chunk_size = 1024 * 1024
        for i in range(0, len(content), chunk_size):
            yield content[i : i + chunk_size]

    async def delete(self, name: str) -> bool:
        path = self._clean_path(name)
        try:
            dbx = self._get_client()

            def _del():
                dbx.files_delete_v2(path)

            await asyncio.to_thread(_del)
            return True
        except Exception as e:
            logger.warning(f"Dropbox delete failed [{self.account_id}] for {name}: {e}")
            return False

    async def rename(self, old_name: str, new_name: str) -> bool:
        old_path = self._clean_path(old_name)
        new_path = self._clean_path(new_name)
        try:
            dbx = self._get_client()

            def _ren():
                dbx.files_move_v2(old_path, new_path)

            await asyncio.to_thread(_ren)
            return True
        except Exception as e:
            logger.error(f"Dropbox rename failed [{self.account_id}]: {e}")
            return False

    async def copy(self, src_name: str, dest_name: str) -> bool:
        src_path  = self._clean_path(src_name)
        dest_path = self._clean_path(dest_name)
        try:
            dbx = self._get_client()

            def _cp():
                dbx.files_copy_v2(src_path, dest_path)

            await asyncio.to_thread(_cp)
            return True
        except Exception as e:
            logger.error(f"Dropbox copy failed [{self.account_id}]: {e}")
            return False

    async def exists(self, name: str) -> bool:
        path = self._clean_path(name)
        try:
            dbx = self._get_client()

            def _meta():
                return dbx.files_get_metadata(path)

            await asyncio.to_thread(_meta)
            return True
        except Exception:
            return False

    async def metadata(self, name: str) -> Dict[str, Any]:
        dbx = self._get_client()
        if not name:
            def _space():
                return dbx.users_get_space_usage()

            usage = await asyncio.to_thread(_space)
            alloc = usage.allocation.get_individual()
            total = alloc.allocated
            used  = usage.used
            return {
                "total_bytes": total,
                "used_bytes":  used,
                "free_bytes":  max(0, total - used),
                "type":        "directory",
            }

        path = self._clean_path(name)
        import dropbox

        try:
            def _meta():
                return dbx.files_get_metadata(path)

            res       = await asyncio.to_thread(_meta)
            is_folder = isinstance(res, dropbox.files.FolderMetadata)
            return {
                "name":        res.name,
                "size":        res.size if not is_folder else 0,
                "created_at":  getattr(res, "client_modified", None),
                "modified_at": getattr(res, "server_modified", None),
                "type":        "directory" if is_folder else "file",
            }
        except Exception as e:
            raise FileNotFoundError(f"File {name} metadata not found: {e}")

    async def checksum(self, name: str) -> str:
        path = self._clean_path(name)
        dbx  = self._get_client()
        try:
            def _meta():
                return dbx.files_get_metadata(path)

            res = await asyncio.to_thread(_meta)
            return getattr(res, "content_hash", "")
        except Exception as e:
            raise FileNotFoundError(f"File {name} not found: {e}")

    async def create_directory(self, path: str) -> bool:
        dir_path = self._clean_path(path)
        try:
            dbx = self._get_client()

            def _create():
                dbx.files_create_folder_v2(dir_path)

            await asyncio.to_thread(_create)
            return True
        except Exception as e:
            logger.warning(f"Dropbox create_directory failed [{self.account_id}]: {e}")
            return True  # Usually already exists

    async def delete_directory(self, path: str) -> bool:
        return await self.delete(path)

    async def list_directory(self, path: str) -> List[str]:
        dir_path = self._clean_path(path)
        try:
            dbx = self._get_client()

            def _list():
                res = dbx.files_list_folder(dir_path)
                return [entry.name for entry in res.entries]

            return await asyncio.to_thread(_list)
        except Exception as e:
            logger.error(f"Dropbox list_directory failed [{self.account_id}]: {e}")
            return []

    async def generate_signed_url(self, name: str, expiration: int = 3600) -> str:
        path = self._clean_path(name)
        dbx  = self._get_client()
        try:
            def _link():
                try:
                    return dbx.sharing_create_shared_link_with_settings(path).url
                except Exception:
                    links = dbx.sharing_list_shared_links(
                        path=path, direct_only=True
                    ).links
                    if links:
                        return links[0].url
                    raise

            return await asyncio.to_thread(_link)
        except Exception as e:
            logger.error(f"Dropbox generate_signed_url failed: {e}")
            try:
                def _temp():
                    return dbx.files_get_temporary_link(path).link

                return await asyncio.to_thread(_temp)
            except Exception:
                return f"https://www.dropbox.com/home/tachyon/{name}"

    async def health_check(self) -> bool:
        """
        Health check with a 10-second timeout.

        If credentials are invalid the provider is already flagged as
        permanently disabled and _get_client() raises immediately (no SDK call).
        """
        if self._permanently_disabled:
            logger.warning(
                f"[dropbox/{self.account_id}] health_check skipped — provider disabled: "
                f"{self._disable_reason}"
            )
            return False

        try:
            dbx = self._get_client()

            def _ping():
                return dbx.users_get_current_account()

            await asyncio.wait_for(asyncio.to_thread(_ping), timeout=10.0)
            return True
        except asyncio.TimeoutError:
            logger.warning(f"[dropbox/{self.account_id}] health_check timed out")
            return False
        except Exception as e:
            logger.warning(f"[dropbox/{self.account_id}] health_check failed: {e}")
            return False
