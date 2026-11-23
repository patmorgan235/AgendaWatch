"""Polite HTTP client: retries, per-host throttling, download size cap, conditional downloads."""

from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = (
    "agenda-watch/1.0 (personal civic monitoring script; "
    "checks public meeting agendas once a day)"
)

# Some city sites (cor.net, behind Akamai) refuse any client that doesn't look like a
# browser: they need a browser User-Agent *and* HTTP/2, which `requests` can't speak.
# Sources opt in with `user_agent: browser` in config.yaml; those hosts are fetched with
# httpx over HTTP/2 (pip install "httpx[http2]").
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class FetchError(Exception):
    """Raised when a page or file can't be retrieved."""


@dataclass
class Download:
    """A downloaded file, or (not_modified=True, empty body) the server's word that it hasn't changed."""

    body: bytes
    content_type: str
    url: str
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


def conditional_headers(etag: str | None, last_modified: str | None) -> dict[str, str]:
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    return headers


@dataclass
class _Response:
    status: int
    body: bytes
    content_type: str
    url: str
    charset: str | None
    headers: dict[str, str]


class Http:
    def __init__(self, delay: float = 1.0, timeout: float = 60.0, max_bytes: int = 80 * 1024 * 1024):
        self.delay = delay
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._last_hit: dict[str, float] = {}
        self._host_headers: dict[str, dict[str, str]] = {}
        self._browser_hosts: set[str] = set()
        self._h2 = None  # httpx.Client, created on first use
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

    def set_host_headers(self, host: str, headers: dict[str, str]) -> None:
        """Send these headers (e.g. a different User-Agent) on every request to `host`."""
        self._host_headers.setdefault(host.lower(), {}).update(headers)

    def set_browser_host(self, host: str) -> None:
        """Fetch `host` like a browser would: browser headers over HTTP/2."""
        host = host.lower()
        self._browser_hosts.add(host)
        self.set_host_headers(host, BROWSER_HEADERS)

    def _is_browser_host(self, url: str) -> bool:
        return urlparse(url).netloc.lower() in self._browser_hosts

    def _headers_for(self, url: str, extra: dict[str, str] | None = None) -> dict[str, str] | None:
        headers = dict(self._host_headers.get(urlparse(url).netloc.lower(), {}))
        headers.update(extra or {})
        return headers or None

    def _h2_client(self):
        if self._h2 is None:
            try:
                import httpx
                import h2  # noqa: F401 - httpx needs it for HTTP/2
            except ImportError as exc:
                raise FetchError(
                    'this site needs HTTP/2; run: pip install "httpx[http2]"'
                ) from exc
            self._h2 = httpx.Client(http2=True, follow_redirects=True, timeout=self.timeout)
        return self._h2

    def _browser_fetch(self, url: str, params: dict | None = None,
                       extra_headers: dict[str, str] | None = None) -> _Response:
        """Fetch over HTTP/2. A 304 comes back as an empty response with status 304."""
        client = self._h2_client()  # raises a helpful FetchError if httpx isn't installed
        import httpx
        headers = self._headers_for(url, extra_headers) or {}
        host = urlparse(url).netloc
        for attempt in range(4):
            self._throttle(url)
            try:
                with client.stream("GET", url, params=params, headers=headers) as resp:
                    if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                        time.sleep(1.5 * 2 ** attempt)
                        continue
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                    ctype = resp_headers.get("content-type", "").lower()
                    if resp.status_code == 304 and extra_headers:
                        return _Response(304, b"", ctype, str(resp.url), None, resp_headers)
                    if resp.status_code != 200:
                        raise FetchError(f"HTTP {resp.status_code} for {resp.url}")
                    declared = resp_headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self.max_bytes:
                        raise FetchError(f"File is {int(declared) // (1024 * 1024)} MB, over the size limit")
                    chunks, total = [], 0
                    for chunk in resp.iter_bytes(chunk_size=256 * 1024):
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise FetchError("File exceeded the size limit while downloading")
                        chunks.append(chunk)
                    return _Response(200, b"".join(chunks), ctype, str(resp.url), resp.charset_encoding,
                                     resp_headers)
            except httpx.ConnectError as exc:
                raise FetchError(f"couldn't connect to {host} (site down, or blocked on this network)") from exc
            except httpx.TimeoutException as exc:
                raise FetchError(f"{host} timed out") from exc
            except httpx.HTTPError as exc:
                raise FetchError(f"{type(exc).__name__}: {exc}") from exc
        raise FetchError(f"{host} kept returning server errors")

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def get(self, url: str, params: dict | None = None, stream: bool = False,
            headers: dict[str, str] | None = None, ok_statuses: tuple[int, ...] = (200,)) -> requests.Response:
        self._throttle(url)
        try:
            resp = self.session.get(
                url, params=params, timeout=self.timeout, stream=stream, allow_redirects=True,
                headers=self._headers_for(url, headers),
            )
        except requests.ConnectionError as exc:
            host = urlparse(url).netloc
            raise FetchError(f"couldn't connect to {host} (site down, or blocked on this network)") from exc
        except requests.Timeout as exc:
            raise FetchError(f"{urlparse(url).netloc} timed out") from exc
        except requests.RequestException as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code not in ok_statuses:
            resp.close()
            raise FetchError(f"HTTP {resp.status_code} for {resp.url}")
        return resp

    def get_text(self, url: str, params: dict | None = None) -> str:
        if self._is_browser_host(url):
            r = self._browser_fetch(url, params)
            return r.body.decode(r.charset or "utf-8", errors="replace")
        resp = self.get(url, params=params)
        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text

    def get_json(self, url: str, params: dict | None = None):
        if self._is_browser_host(url):
            import json

            r = self._browser_fetch(url, params)
            try:
                return json.loads(r.body)
            except ValueError as exc:
                raise FetchError(f"Response from {r.url} was not JSON") from exc
        resp = self.get(url, params=params)
        try:
            return resp.json()
        except ValueError as exc:
            raise FetchError(f"Response from {resp.url} was not JSON") from exc

    def fetch(self, url: str, etag: str | None = None, last_modified: str | None = None) -> Download:
        """Download a file, refusing files over max_bytes.

        With `etag` / `last_modified` from an earlier download, asks the server to skip the body
        if the file hasn't changed; a 304 reply comes back as Download(not_modified=True).
        """
        cond = conditional_headers(etag, last_modified)
        if self._is_browser_host(url):
            r = self._browser_fetch(url, extra_headers=cond)
            return Download(r.body, r.content_type, r.url, r.headers.get("etag"), r.headers.get("last-modified"),
                            not_modified=r.status == 304)
        resp = self.get(url, stream=True, headers=cond, ok_statuses=(200, 304) if cond else (200,))
        ctype = resp.headers.get("Content-Type", "").lower()
        new_etag, new_lm = resp.headers.get("ETag"), resp.headers.get("Last-Modified")
        if resp.status_code == 304:
            resp.close()
            return Download(b"", ctype, resp.url, new_etag or etag, new_lm or last_modified, not_modified=True)
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
        return Download(b"".join(chunks), ctype, resp.url, new_etag, new_lm)

    def download(self, url: str) -> tuple[bytes, str, str]:
        """Return (body, content_type, final_url), refusing files over max_bytes."""
        d = self.fetch(url)
        return d.body, d.content_type, d.url
