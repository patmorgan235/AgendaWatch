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

    def _browser_fetch(self, url: str, params: dict | None = None) -> tuple[bytes, str, str, str | None]:
        """Return (body, content_type, final_url, charset) fetched over HTTP/2."""
        client = self._h2_client()  # raises a helpful FetchError if httpx isn't installed
        import httpx
        headers = self._host_headers.get(urlparse(url).netloc.lower(), {})
        host = urlparse(url).netloc
        for attempt in range(4):
            self._throttle(url)
            try:
                with client.stream("GET", url, params=params, headers=headers) as resp:
                    if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                        time.sleep(1.5 * 2 ** attempt)
                        continue
                    if resp.status_code != 200:
                        raise FetchError(f"HTTP {resp.status_code} for {resp.url}")
                    declared = resp.headers.get("Content-Length")
                    if declared and declared.isdigit() and int(declared) > self.max_bytes:
                        raise FetchError(f"File is {int(declared) // (1024 * 1024)} MB, over the size limit")
                    chunks, total = [], 0
                    for chunk in resp.iter_bytes(chunk_size=256 * 1024):
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise FetchError("File exceeded the size limit while downloading")
                        chunks.append(chunk)
                    ctype = resp.headers.get("Content-Type", "").lower()
                    return b"".join(chunks), ctype, str(resp.url), resp.charset_encoding
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

    def get(self, url: str, params: dict | None = None, stream: bool = False) -> requests.Response:
        self._throttle(url)
        try:
            resp = self.session.get(
                url, params=params, timeout=self.timeout, stream=stream, allow_redirects=True,
                headers=self._host_headers.get(urlparse(url).netloc.lower()),
            )
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
        if self._is_browser_host(url):
            body, _, _, charset = self._browser_fetch(url, params)
            return body.decode(charset or "utf-8", errors="replace")
        resp = self.get(url, params=params)
        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text

    def get_json(self, url: str, params: dict | None = None):
        if self._is_browser_host(url):
            import json

            body, _, final, _ = self._browser_fetch(url, params)
            try:
                return json.loads(body)
            except ValueError as exc:
                raise FetchError(f"Response from {final} was not JSON") from exc
        resp = self.get(url, params=params)
        try:
            return resp.json()
        except ValueError as exc:
            raise FetchError(f"Response from {resp.url} was not JSON") from exc

    def download(self, url: str) -> tuple[bytes, str, str]:
        """Return (body, content_type, final_url), refusing files over max_bytes."""
        if self._is_browser_host(url):
            body, ctype, final, _ = self._browser_fetch(url)
            return body, ctype, final
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
