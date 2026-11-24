"""Hosted-run pieces: storage backends, run lock, the job, env overrides, page pruning, JSON logs."""

import io
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agendawatch.job import EXIT_LOCKED, EXIT_OK, JsonFormatter, apply_env_overrides, run_job
from agendawatch.storage import (AzureBlobStorage, LocalStorage, LockHeld, S3Storage, cache_control,
                                 content_type, storage_from_env)
from agendawatch.store import Store

from helpers import FakeHttp, make_pdf
from test_pipeline import AGENDA_P1, AGENDA_P2, TODAY, config, routes


# ---------------------------------------------------------------- local backend
def test_local_state_site_and_lock(tmp_path):
    st = LocalStorage(tmp_path / "hosted")
    assert st.get_state("db", tmp_path / "x") is False
    (tmp_path / "db").write_bytes(b"sqlite!")
    st.put_state("db", tmp_path / "db")
    assert st.get_state("db", tmp_path / "copy") and (tmp_path / "copy").read_bytes() == b"sqlite!"

    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<p>hi</p>")
    (site / "feed.xml").write_text("<feed/>")
    assert st.publish_site(site) == 2
    assert (tmp_path / "hosted/site/index.html").read_text() == "<p>hi</p>"

    with st.lock():
        with pytest.raises(LockHeld):
            with st.lock():
                pass
    with st.lock():  # released after the block
        pass


def test_local_stale_lock_is_taken_over(tmp_path):
    st = LocalStorage(tmp_path, lock_ttl_hours=1)
    (tmp_path / "state").mkdir()
    lock = tmp_path / "state/run.lock"
    lock.write_text("old")
    old = time.time() - 2 * 3600
    os.utime(lock, (old, old))
    with st.lock():
        assert lock.exists()
    assert not lock.exists()


def test_content_types_and_cache():
    assert content_type(Path("upcoming.ics")).startswith("text/calendar")
    assert content_type(Path("feed.xml")).startswith("application/atom+xml")
    assert cache_control(Path("agenda-report-2026-11-22.html")).endswith("86400")
    assert cache_control(Path("index.html")).endswith("300")


# ---------------------------------------------------------------- S3 lock (fake client)
class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None):
        if IfNoneMatch == "*" and (Bucket, Key) in self.objects:
            raise ClientError("PreconditionFailed")
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


def test_s3_lock_exclusive_and_stale_takeover():
    s3 = FakeS3()
    st = S3Storage("state", "site", state_prefix="aw", client=s3, lock_ttl_hours=1)
    with st.lock():
        assert ("state", "aw/run.lock") in s3.objects
        with pytest.raises(LockHeld):
            with st.lock():
                pass
    assert ("state", "aw/run.lock") not in s3.objects
    stale = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    s3.objects[("state", "aw/run.lock")] = stale.encode()
    with st.lock():
        assert s3.objects[("state", "aw/run.lock")].decode() != stale


# ---------------------------------------------------------------- Azure lease (fake SDK objects)
class FakeLease:
    def __init__(self, blob):
        self.blob = blob

    def release(self):
        self.blob.leased = False


class FakeBlob:
    def __init__(self):
        self.exists, self.leased, self.metadata = False, False, {}

    def upload_blob(self, data, overwrite=False, **kw):
        from azure.core.exceptions import ResourceExistsError
        if self.exists and not overwrite:
            raise ResourceExistsError("exists")
        self.exists = True

    def acquire_lease(self, lease_duration=-1):
        from azure.core.exceptions import HttpResponseError as AzureHttpError
        if self.leased:
            err = AzureHttpError(message="already leased")
            err.status_code = 409
            raise err
        self.leased = True
        return FakeLease(self)

    def break_lease(self, lease_break_period=0):
        self.leased = False

    def get_blob_properties(self):
        return type("P", (), {"metadata": self.metadata})()

    def set_blob_metadata(self, metadata, lease=None):
        self.metadata = metadata


class FakeContainer:
    def __init__(self):
        self.blobs = {}

    def get_blob_client(self, name):
        return self.blobs.setdefault(name, FakeBlob())


class FakeService:
    def __init__(self):
        self.containers = {}

    def get_container_client(self, name):
        return self.containers.setdefault(name, FakeContainer())


def test_azure_lease_lock():
    svc = FakeService()
    st = AzureBlobStorage(service=svc, lock_ttl_hours=1)
    blob = svc.containers["state"].blobs.get("run.lock") or svc.get_container_client("state").get_blob_client("run.lock")
    with st.lock():
        assert blob.leased and "acquired" in blob.metadata
        with pytest.raises(LockHeld):
            with st.lock():
                pass
    assert not blob.leased
    # a lease left behind by a crashed run is broken once it's older than the TTL
    blob.leased = True
    blob.metadata = {"acquired": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()}
    with st.lock():
        assert blob.leased
    assert not blob.leased


# ---------------------------------------------------------------- configuration
def test_env_overrides_and_storage_choice(tmp_path):
    cfg = {"settings": {"lookahead_days": 60}}
    apply_env_overrides(cfg, {"AGENDAWATCH_SETTING_LOOKAHEAD_DAYS": "30",
                              "AGENDAWATCH_SETTING_SITE_URL": "https://a.example",
                              "OTHER": "x"})
    assert cfg["settings"] == {"lookahead_days": 30, "site_url": "https://a.example"}
    assert isinstance(storage_from_env({"AGENDAWATCH_LOCAL_DIR": str(tmp_path)}), LocalStorage)
    with pytest.raises(ValueError):
        storage_from_env({"AGENDAWATCH_STORAGE": "s3"})
    with pytest.raises(ValueError):
        storage_from_env({"AGENDAWATCH_STORAGE": "ftp"})


def test_json_log_lines_carry_fields():
    rec = logging.LogRecord("agendawatch", logging.INFO, __file__, 1, "source %s ok", ("Dallas",), None)
    rec.fields = {"event": "source", "listed": 8}
    out = json.loads(JsonFormatter().format(rec))
    assert out["message"] == "source Dallas ok" and out["event"] == "source" and out["listed"] == 8


# ---------------------------------------------------------------- page pruning
def test_prune_pages_keeps_matches(tmp_path):
    from agendawatch.sources import Document
    store = Store(tmp_path / "t.db")
    old = Document("S", "C", "Council", date(2026, 1, 5), "agenda", "https://x/old.pdf")
    new = Document("S", "C", "Council", date(2026, 11, 5), "agenda", "https://x/new.pdf")
    store.save_document(old, "a", ["page one", "page two"], "ok")
    store.save_document(new, "b", ["page one"], "ok")
    assert store.prune_pages(date(2026, 6, 1)) == 1
    left = {r["url"] for r in store.db.execute("SELECT DISTINCT url FROM pages")}
    assert left == {"https://x/new.pdf"}
    assert store.get_document("https://x/old.pdf")["status"] == "ok"  # still known, so not re-downloaded
    store.close()


# ---------------------------------------------------------------- the hosted job end to end
def test_job_twice_through_local_storage(tmp_path):
    cfg = config(tmp_path)
    cfg["settings"]["site_url"] = "https://agendas.example.org"
    storage = LocalStorage(tmp_path / "hosted")
    pdf = make_pdf([AGENDA_P1, AGENDA_P2])

    assert run_job(json.loads(json.dumps(cfg)), storage, today=TODAY, http=FakeHttp(routes(pdf))) == EXIT_OK
    hosted = tmp_path / "hosted"
    assert (hosted / "state/agenda_watch.db").exists()
    data = json.loads((hosted / "site/data.json").read_text())
    assert data["meetings"] and all(x["new"] for m in data["meetings"] for x in m["matches"])
    assert not (hosted / "state/run.lock").exists()

    # second run starts from the stored database: nothing new, everything still shown
    assert run_job(json.loads(json.dumps(cfg)), storage, today=TODAY, http=FakeHttp(routes(pdf))) == EXIT_OK
    data2 = json.loads((hosted / "site/data.json").read_text())
    assert {m["id"] for m in data2["meetings"]} == {m["id"] for m in data["meetings"]}
    assert not any(x["new"] for m in data2["meetings"] for x in m["matches"])

    # a run that finds the lock held exits without touching anything
    with storage.lock():
        assert run_job(json.loads(json.dumps(cfg)), storage, today=TODAY, http=FakeHttp(routes(pdf))) == EXIT_LOCKED


def test_partial_job_saves_the_database_without_publishing(tmp_path):
    cfg = config(tmp_path)
    storage = LocalStorage(tmp_path / "hosted")
    pdf = make_pdf([AGENDA_P1, AGENDA_P2])
    assert run_job(json.loads(json.dumps(cfg)), storage, today=TODAY, http=FakeHttp(routes(pdf)), only=["Dallas"]) == EXIT_OK
    hosted = tmp_path / "hosted"
    assert (hosted / "state/agenda_watch.db").exists()
    assert not (hosted / "site/data.json").exists()       # a page built from one source would list only that source
    store = Store(hosted / "state/agenda_watch.db")
    assert {r["city"] for r in store.db.execute("SELECT DISTINCT city FROM documents")} == {"Dallas"}
    store.close()
