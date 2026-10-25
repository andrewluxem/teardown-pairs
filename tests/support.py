"""Shared test plumbing: loopback-only network guard, fakes, fixture loaders.

Importing this module installs the guard, so every test module imports it
before anything else. Connecting or resolving anything other than
127.0.0.1, ::1 or localhost raises NetworkBlocked.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
REPO = os.path.dirname(HERE)
SCRIPTS = os.path.join(REPO, "scripts")

LOOPBACK = ("127.0.0.1", "::1", "localhost")


class NetworkBlocked(RuntimeError):
    pass


def _host_of(address):
    if isinstance(address, (tuple, list)) and address:
        return str(address[0])
    return None


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo
_real_create_connection = socket.create_connection


def _check(address):
    if isinstance(address, (str, bytes)):  # AF_UNIX path
        return
    host = _host_of(address)
    if host not in LOOPBACK:
        raise NetworkBlocked("tests may only reach loopback, not %r" % (address,))


def _connect(self, address):
    _check(address)
    return _real_connect(self, address)


def _connect_ex(self, address):
    _check(address)
    return _real_connect_ex(self, address)


def _getaddrinfo(host, *args, **kwargs):
    if host is not None and str(host) not in LOOPBACK:
        raise NetworkBlocked("tests may not resolve %r" % (host,))
    return _real_getaddrinfo(host, *args, **kwargs)


def _create_connection(address, *args, **kwargs):
    _check(address)
    return _real_create_connection(address, *args, **kwargs)


socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex
socket.getaddrinfo = _getaddrinfo
socket.create_connection = _create_connection

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import teardown_pairs as tp  # noqa: E402


# ------------------------------------------------------------- fixtures


def fixture_path(*parts) -> str:
    return os.path.join(FIXTURES, *parts)


def load_json(*parts):
    with open(fixture_path(*parts), encoding="utf-8") as fh:
        return json.load(fh)


def ws_record(name: str) -> dict:
    """A recorded wayback-salvage run: args, exit_code, stdout, stderr."""
    return load_json("ws", name + ".json")


def png_bytes(width: int, height: int, rgb=(200, 80, 40)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def png_size(path: str) -> tuple:
    with open(path, "rb") as fh:
        head = fh.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("%s is not a PNG" % path)
    return struct.unpack(">II", head[16:24])


class TempDir:
    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="tp-test-")
        return self.path

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ fakes


class FakeClock:
    """Injected monotonic clock whose sleep advances time."""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRunner:
    """Stands in for the wayback-salvage subprocess.

    routes maps a subcommand name ('--version', 'snapshots', 'fetch', ...)
    to a list of (exit_code, stdout, stderr) results or callables taking
    the argv and returning one; the last result repeats. Every call is
    recorded with the clock time it started.
    """

    def __init__(self, routes=None, clock=None):
        self.routes = {k: list(v) if isinstance(v, list) else [v] for k, v in (routes or {}).items()}
        self.calls = []
        self.clock = clock

    def add(self, name, *results):
        self.routes.setdefault(name, []).extend(results)
        return self

    async def __call__(self, argv):
        name = argv[0]
        self.calls.append((self.clock.time() if self.clock else None, list(argv)))
        results = self.routes.get(name)
        if not results:
            raise AssertionError("no fake result for wayback-salvage %r" % (argv,))
        item = results[0] if len(results) == 1 else results.pop(0)
        if callable(item):
            item = item(argv)
        return item

    def names(self):
        return [argv[0] for _, argv in self.calls]


def record_result(name: str) -> tuple:
    rec = ws_record(name)
    return rec["exit_code"], rec["stdout"], rec["stderr"]


VERSION_OK = (0, "wayback-salvage 0.2.1\n", "")


def snapshots_result(rows: list) -> tuple:
    return 0, json.dumps(rows, indent=2) + "\n", ""


def cdx_row(ts: str, original: str = "https://www.shop.example/", status="200", mimetype="text/html", digest=None):
    return {
        "timestamp": ts, "original": original, "status": status, "mimetype": mimetype,
        "digest": digest or ("D" + ts), "length": "1000",
        "wayback_url": "https://web.archive.org/web/%s/%s" % (ts, original),
    }


def fake_fetch(argv):
    """fetch --save PATH: write a small page and print the recorded shape."""
    path = argv[argv.index("--save") + 1]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("<html><head><title>archived</title></head><body>archived page</body></html>")
    rec = json.loads(ws_record("fetch")["stdout"])
    rec["saved"] = path
    rec["timestamp"] = rec["requested_timestamp"] = argv[argv.index("-t") + 1]
    return 0, json.dumps(rec, indent=2) + "\n", ""


def fake_page(argv):
    rec = json.loads(ws_record("page")["stdout"])
    rec["timestamp"] = rec["requested_timestamp"] = argv[argv.index("-t") + 1]
    return 0, json.dumps(rec, indent=2) + "\n", ""


def basic_facts(title="Shop", h1=("Fall Sale",), signals=None):
    facts = {
        "page": {"url": "https://www.shop.example/", "final_url": "https://www.shop.example/",
                 "title": title, "description": "", "canonical": "", "robots": "", "og_title": "",
                 "og_image": "", "lang": "en"},
        "headings": {"h1": list(h1), "h2": []},
        "nav": [],
        "above_fold": {"text_blocks": [{"text": "Fall Sale", "font_size": 32.0}], "buttons": [], "image_count": 2},
        "ctas": [],
        "promos": [],
        "counts": {"images": 5, "links": 40, "forms": 1, "iframes": 0, "price_like": 0, "price_scope": "body"},
        "structured_data": {"blocks": 0, "errors": [], "types": [], "product": None, "breadcrumbs": [], "item_list": None},
        "pdp_heuristic": {"label": "heuristic", "first_price_after_h1": None, "add_to_cart": None, "review_count_text": None},
        "plp_heuristic": {"label": "heuristic", "price_like_in_main": 0, "filter_elements": 0, "filter_labels": []},
        "signals": {"text_chars": 2400, "text_sample": "Fall Sale jackets boots", "captcha": [], "reference_block": False},
    }
    if signals:
        facts["signals"].update(signals)
    return facts


class FakeRenderer:
    """Stands in for the Playwright renderer: writes tiny PNGs and returns
    canned load results. Records every call with the clock time."""

    chromium_version = "fake-chromium"
    playwright_version = "fake-playwright"

    def __init__(self, clock=None, archive_results=None, live_results=None):
        self.clock = clock
        self.loads = []
        self.robots = []
        self.compare_shots = []
        self.archive_results = list(archive_results or [])
        self.live_results = list(live_results or [])
        self.started = False
        self.closed = False

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    def _shots(self, side_dir, names):
        shots = {}
        for name, (w, h) in names.items():
            path = os.path.join(side_dir, name + ".png")
            with open(path, "wb") as fh:
                fh.write(png_bytes(4, 4))
            shots[name] = {"file": name + ".png", "width": w, "height": h, "truncated": False, "page_height": h}
        return shots

    def _result(self, kind, url, viewport, extra):
        res = {
            "kind": kind, "url": url, "final_url": url, "viewport": viewport, "main_status": 200,
            "load_timeout": False, "error": None, "degraded": False, "suspect": None,
            "consent": {"clicked": None, "escape": True},
            "requests": {"requests": 10, "failed": 1, "aborted_budget": 0, "leaked": 0, "aborted_media": 0,
                         "unrouted": 0, "http_429": 0, "bytes": 12345, "main_status": 200},
            "facts": basic_facts(),
            "user_agent": "fake-agent",
            "main_body": b"<html><head><script src='https://www.googletagmanager.com/gtm.js'></script></head></html>",
        }
        res.update(extra or {})
        return res

    async def load_archive(self, url, side_dir, viewports):
        self.loads.append((self.clock.time() if self.clock else None, "archive", url, tuple(viewports)))
        extra = self.archive_results.pop(0) if self.archive_results else {}
        names = {}
        if "desktop" in viewports:
            names.update({"desktop": (1440, 2000), "desktop-fold": (1440, 900)})
        if "mobile" in viewports:
            names.update({"mobile": (390, 3000), "mobile-fold": (390, 844)})
        res = self._result("archive", url, "desktop+mobile", extra)
        res.setdefault("screenshots", self._shots(side_dir, names))
        res["mobile_basis"] = "desktop-capture-resized"
        return res

    async def load_live(self, url, side_dir, viewport):
        self.loads.append((self.clock.time() if self.clock else None, "live", url, viewport))
        extra = self.live_results.pop(0) if self.live_results else {}
        names = {"desktop": (1440, 2000), "desktop-fold": (1440, 900)} if viewport == "desktop" else \
            {"mobile": (390, 3000), "mobile-fold": (390, 844)}
        res = self._result("live", url, viewport, extra)
        res.setdefault("screenshots", self._shots(side_dir, names))
        res["mobile_basis"] = "mobile-device-profile"
        return res

    async def fetch_robots(self, robots_url):
        self.robots.append(robots_url)
        return 404, ""

    async def screenshot_html(self, html_path, out_png, width, height, fragment=""):
        self.compare_shots.append((html_path, out_png, fragment))
        with open(out_png, "wb") as fh:
            fh.write(png_bytes(4, 4))
