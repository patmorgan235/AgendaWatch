"""Test helpers: a fake HTTP client and a tiny PDF generator."""

from __future__ import annotations

import io
import json
from urllib.parse import urlencode

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from agendawatch.http import FetchError


def make_pdf(pages: list[str]) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for text in pages:
        y = 740
        for line in text.split("\n"):
            c.drawString(60, y, line)
            y -= 16
        c.showPage()
    c.save()
    return buf.getvalue()


class FakeHttp:
    """Maps URLs (optionally with query params) to canned responses."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[str] = []
        self.host_headers: dict[str, dict] = {}

    def set_host_headers(self, host, headers):
        self.host_headers.setdefault(host, {}).update(headers)

    def set_browser_host(self, host):
        self.set_host_headers(host, {"User-Agent": "Mozilla/5.0 (test)", "_http2": "yes"})

    def _key(self, url, params=None):
        return f"{url}?{urlencode(params)}" if params else url

    def _lookup(self, url, params=None):
        self.calls.append(url)
        for key in (self._key(url, params), url):
            if key in self.routes:
                val = self.routes[key]
                if isinstance(val, Exception):
                    raise val
                return val
        raise FetchError(f"HTTP 404 for {url}")

    def get_text(self, url, params=None):
        val = self._lookup(url, params)
        return val.decode() if isinstance(val, bytes) else str(val)

    def get_json(self, url, params=None):
        val = self._lookup(url, params)
        return json.loads(val) if isinstance(val, (str, bytes)) else val

    def download(self, url):
        val = self._lookup(url)
        if isinstance(val, bytes):
            ctype = "application/pdf" if val.startswith(b"%PDF") else "text/html"
            return val, ctype, url
        return str(val).encode(), "text/html", url
