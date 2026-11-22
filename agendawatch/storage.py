"""Where a hosted run keeps its state (the SQLite database) and publishes the site.

Three backends, chosen with AGENDAWATCH_STORAGE:

  local   a folder (default ./hosted): state/ and site/ subfolders. For testing the job locally.
  azure   Azure Blob Storage: a private container for state, the `$web` container for the site.
          Authenticates with managed identity (or any DefaultAzureCredential source), or with
          AZURE_STORAGE_CONNECTION_STRING when set.
  s3      Any S3-compatible store (AWS S3, Cloudflare R2, Backblaze B2, MinIO).

Every backend also provides a run lock, so two runs never work on the database at once. A lock
older than lock_ttl_hours is treated as left behind by a crashed run and taken over.

The Azure and S3 SDKs are imported only when that backend is used.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("agendawatch.storage")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".xml": "application/atom+xml; charset=utf-8",
    ".ics": "text/calendar; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}
SHORT_CACHE = "public, max-age=300"          # pages and feeds that change every run
LONG_CACHE = "public, max-age=86400"         # dated snapshots never change


class LockHeld(RuntimeError):
    """Another run holds the lock (and it isn't stale yet)."""


def content_type(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def cache_control(path: Path) -> str:
    return LONG_CACHE if path.name.startswith("agenda-report-") else SHORT_CACHE


def site_files(site_dir: Path) -> list[Path]:
    return sorted(p for p in Path(site_dir).rglob("*") if p.is_file())


def _now() -> float:
    return time.time()


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    name = "storage"

    def __init__(self, lock_ttl_hours: float = 4.0):
        self.lock_ttl = lock_ttl_hours * 3600

    # state ------------------------------------------------------------------
    def get_state(self, key: str, dest: Path) -> bool:  # pragma: no cover - interface
        """Download state `key` to dest. Returns False if it doesn't exist yet (first run)."""
        raise NotImplementedError

    def put_state(self, key: str, src: Path) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    # site -------------------------------------------------------------------
    def publish_site(self, site_dir: Path) -> int:  # pragma: no cover - interface
        """Upload every file under site_dir. Returns how many were uploaded."""
        raise NotImplementedError

    # lock -------------------------------------------------------------------
    @contextmanager
    def lock(self):
        token = self._acquire()
        try:
            yield
        finally:
            try:
                self._release(token)
            except Exception as exc:  # noqa: BLE001 - a stale lock is taken over next time anyway
                log.warning("couldn't release the run lock: %s", exc)

    def _acquire(self):  # pragma: no cover - interface
        raise NotImplementedError

    def _release(self, token) -> None:  # pragma: no cover - interface
        raise NotImplementedError


# ---------------------------------------------------------------- local folder
class LocalStorage(Storage):
    name = "local"

    def __init__(self, root: str | Path = "hosted", **kw):
        super().__init__(**kw)
        self.root = Path(root)
        self.state = self.root / "state"
        self.site = self.root / "site"

    def get_state(self, key, dest):
        src = self.state / key
        if not src.exists():
            return False
        dest.write_bytes(src.read_bytes())
        return True

    def put_state(self, key, src):
        self.state.mkdir(parents=True, exist_ok=True)
        tmp = self.state / f".{key}.uploading"
        tmp.write_bytes(Path(src).read_bytes())
        tmp.replace(self.state / key)

    def publish_site(self, site_dir):
        n = 0
        for f in site_files(site_dir):
            dest = self.site / f.relative_to(site_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(f.read_bytes())
            n += 1
        return n

    def _acquire(self):
        self.state.mkdir(parents=True, exist_ok=True)
        path = self.state / "run.lock"
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                age = _now() - path.stat().st_mtime
                if age < self.lock_ttl:
                    raise LockHeld(f"{path} is held (since {age / 60:.0f} min ago)") from None
                log.warning("taking over a stale run lock (%.1f h old)", age / 3600)
                path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w") as fh:
                fh.write(_stamp())
            return path
        raise LockHeld(f"couldn't take {path}")

    def _release(self, token):
        Path(token).unlink(missing_ok=True)


# ---------------------------------------------------------------- Azure Blob Storage
class AzureBlobStorage(Storage):
    name = "azure"
    LOCK_BLOB = "run.lock"

    def __init__(self, account_url: str | None = None, connection_string: str | None = None,
                 state_container: str = "state", site_container: str = "$web",
                 client_id: str | None = None, service=None, **kw):
        super().__init__(**kw)
        if service is None:
            from azure.storage.blob import BlobServiceClient

            if connection_string:
                service = BlobServiceClient.from_connection_string(connection_string)
            else:
                if not account_url:
                    raise ValueError("Set AGENDAWATCH_AZURE_ACCOUNT_URL or AZURE_STORAGE_CONNECTION_STRING")
                from azure.identity import DefaultAzureCredential

                cred = DefaultAzureCredential(managed_identity_client_id=client_id) if client_id else DefaultAzureCredential()
                service = BlobServiceClient(account_url, credential=cred)
        self.service = service
        self.state = service.get_container_client(state_container)
        self.site = service.get_container_client(site_container)

    def get_state(self, key, dest):
        from azure.core.exceptions import ResourceNotFoundError

        try:
            data = self.state.get_blob_client(key).download_blob(max_concurrency=4)
        except ResourceNotFoundError:
            return False
        with open(dest, "wb") as fh:
            data.readinto(fh)
        return True

    def put_state(self, key, src):
        with open(src, "rb") as fh:
            self.state.get_blob_client(key).upload_blob(fh, overwrite=True, max_concurrency=4)

    def publish_site(self, site_dir):
        from azure.storage.blob import ContentSettings

        n = 0
        for f in site_files(site_dir):
            name = f.relative_to(site_dir).as_posix()
            with open(f, "rb") as fh:
                self.site.get_blob_client(name).upload_blob(
                    fh, overwrite=True,
                    content_settings=ContentSettings(content_type=content_type(f), cache_control=cache_control(f)))
            n += 1
        return n

    def _acquire(self):
        from azure.core.exceptions import HttpResponseError, ResourceExistsError

        blob = self.state.get_blob_client(self.LOCK_BLOB)
        try:
            blob.upload_blob(b"", overwrite=False)
        except ResourceExistsError:
            pass  # the lock blob exists from an earlier run; the lease is what matters
        try:
            lease = blob.acquire_lease(lease_duration=-1)
        except HttpResponseError as exc:
            if getattr(exc, "status_code", None) != 409:  # 409 = already leased
                raise
            acquired = (blob.get_blob_properties().metadata or {}).get("acquired")
            age = _now() - datetime.fromisoformat(acquired).timestamp() if acquired else self.lock_ttl
            if age < self.lock_ttl:
                raise LockHeld(f"run lock is held (since {age / 60:.0f} min ago)") from None
            log.warning("taking over a stale run lock (%.1f h old)", age / 3600)
            blob.break_lease(lease_break_period=0)
            lease = blob.acquire_lease(lease_duration=-1)
        blob.set_blob_metadata({"acquired": _stamp()}, lease=lease)
        return lease

    def _release(self, lease):
        lease.release()


# ---------------------------------------------------------------- S3-compatible
class S3Storage(Storage):
    name = "s3"
    LOCK_KEY = "run.lock"

    def __init__(self, state_bucket: str, site_bucket: str, endpoint_url: str | None = None,
                 state_prefix: str = "", site_prefix: str = "", client=None, **kw):
        super().__init__(**kw)
        if client is None:
            import boto3

            client = boto3.client("s3", endpoint_url=endpoint_url or None)
        self.s3 = client
        self.state_bucket, self.site_bucket = state_bucket, site_bucket
        self.state_prefix = state_prefix.strip("/") + "/" if state_prefix.strip("/") else ""
        self.site_prefix = site_prefix.strip("/") + "/" if site_prefix.strip("/") else ""

    def _missing(self, exc) -> bool:
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
        return code in ("404", "NoSuchKey", "NotFound")

    def get_state(self, key, dest):
        try:
            self.s3.download_file(self.state_bucket, self.state_prefix + key, str(dest))
        except Exception as exc:  # noqa: BLE001
            if self._missing(exc):
                return False
            raise
        return True

    def put_state(self, key, src):
        self.s3.upload_file(str(src), self.state_bucket, self.state_prefix + key)

    def publish_site(self, site_dir):
        n = 0
        for f in site_files(site_dir):
            key = self.site_prefix + f.relative_to(site_dir).as_posix()
            self.s3.upload_file(str(f), self.site_bucket, key,
                                ExtraArgs={"ContentType": content_type(f), "CacheControl": cache_control(f)})
            n += 1
        return n

    def _acquire(self):
        key = self.state_prefix + self.LOCK_KEY
        for _ in range(2):
            try:
                self.s3.put_object(Bucket=self.state_bucket, Key=key, Body=_stamp().encode(), IfNoneMatch="*")
                return key
            except Exception as exc:  # noqa: BLE001
                code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
                if code not in ("PreconditionFailed", "412", "ConditionalRequestConflict"):
                    raise
                body = self.s3.get_object(Bucket=self.state_bucket, Key=key)["Body"].read().decode()
                age = _now() - datetime.fromisoformat(body).timestamp()
                if age < self.lock_ttl:
                    raise LockHeld(f"run lock is held (since {age / 60:.0f} min ago)") from None
                log.warning("taking over a stale run lock (%.1f h old)", age / 3600)
                self.s3.delete_object(Bucket=self.state_bucket, Key=key)
        raise LockHeld("couldn't take the run lock")

    def _release(self, key):
        self.s3.delete_object(Bucket=self.state_bucket, Key=key)


# ---------------------------------------------------------------- from environment
def storage_from_env(env: dict | None = None) -> Storage:
    env = os.environ if env is None else env
    kind = (env.get("AGENDAWATCH_STORAGE") or "local").strip().lower()
    ttl = float(env.get("AGENDAWATCH_LOCK_TTL_HOURS") or 4)
    if kind == "local":
        return LocalStorage(env.get("AGENDAWATCH_LOCAL_DIR") or "hosted", lock_ttl_hours=ttl)
    if kind == "azure":
        return AzureBlobStorage(
            account_url=env.get("AGENDAWATCH_AZURE_ACCOUNT_URL"),
            connection_string=env.get("AZURE_STORAGE_CONNECTION_STRING"),
            state_container=env.get("AGENDAWATCH_AZURE_STATE_CONTAINER") or "state",
            site_container=env.get("AGENDAWATCH_AZURE_SITE_CONTAINER") or "$web",
            client_id=env.get("AGENDAWATCH_AZURE_CLIENT_ID") or env.get("AZURE_CLIENT_ID"),
            lock_ttl_hours=ttl,
        )
    if kind == "s3":
        if not env.get("AGENDAWATCH_S3_STATE_BUCKET") or not env.get("AGENDAWATCH_S3_SITE_BUCKET"):
            raise ValueError("Set AGENDAWATCH_S3_STATE_BUCKET and AGENDAWATCH_S3_SITE_BUCKET")
        return S3Storage(
            state_bucket=env["AGENDAWATCH_S3_STATE_BUCKET"], site_bucket=env["AGENDAWATCH_S3_SITE_BUCKET"],
            endpoint_url=env.get("AGENDAWATCH_S3_ENDPOINT_URL"),
            state_prefix=env.get("AGENDAWATCH_S3_STATE_PREFIX") or "",
            site_prefix=env.get("AGENDAWATCH_S3_SITE_PREFIX") or "",
            lock_ttl_hours=ttl,
        )
    raise ValueError(f"Unknown AGENDAWATCH_STORAGE '{kind}' (use local, azure or s3)")
