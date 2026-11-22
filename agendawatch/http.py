"""Polite HTTP client: retries, per-host throttling, download size cap."""

from __future__ import annotations

import time
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = (
    "agenda-watch/1.0 (personal civic monitoring script; "
    "checks public meeting agendas once a day)"
)


class FetchError(Exception):
    """Raised when a page or file can't be retrieved."""


class Http:
    def __init__(self, delay: float = 1.0, timeout: float = 60.0, max_bytes: int = 80 * 1024 * 1024):
        self.delay = delay
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._last_hit: dict[str, float] = {}
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
        retry = Retry(
            total=3,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def get(self, url: str, params: dict | None = None, stream: bool = False) -> requests.Response:
        self._throttle(url)
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout, stream=stream, allow_redirects=True)
        except requests.ConnectionError as exc:
            host = urlparse(url).netloc
            raise FetchError(f"couldn't connect to {host} (site down, or blocked on this network)") from exc
        except requests.Timeout as exc:
            raise FetchError(f"{urlparse(url).netloc} timed out") from exc
        except requests.RequestException as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code != 200:
            resp.close()
            raise FetchError(f"HTTP {resp.status_code} for {resp.url}")
        return resp

    def get_text(self, url: str, params: dict | None = None) -> str:
        resp = self.get(url, params=params)
        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text

    def get_json(self, url: str, params: dict | None = None):
        resp = self.get(url, params=params)
        try:
            return resp.json()
        except ValueError as exc:
            raise FetchError(f"Response from {resp.url} was not JSON") from exc

    def download(self, url: str) -> tuple[bytes, str, str]:
        """Return (body, content_type, final_url), refusing files over max_bytes."""
        resp = self.get(url, stream=True)
        ctype = resp.headers.get("Content-Type", "").lower()
        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            resp.close()
            raise FetchError(f"File is {int(declared) // (1024 * 1024)} MB, over the size limit")
        chunks, total = [], 0
        try:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                total += len(chunk)
                if total > self.max_bytes:
                    raise FetchError("File exceeded the size limit while downloading")
                chunks.append(chunk)
        except requests.RequestException as exc:
            raise FetchError(f"Download interrupted: {exc}") from exc
        finally:
            resp.close()
        return b"".join(chunks), ctype, resp.url
