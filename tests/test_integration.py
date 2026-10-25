"""Integration tests: real Chromium against a local http.server.

Skipped when Playwright or Chromium is not available. The renderer's
page.route("**/*") handler sends every request through a resolver: archive
URLs (https://web.archive.org/web/<ts>if_/https://shop.test/...) map to the
local fixture server, loopback URLs pass, anything else is aborted and
listed as unrouted. Every test asserts that nothing went unrouted, so no
request can reach the internet.
"""

import asyncio
import http.server
import json
import os
import re
import socket
import struct
import threading
import time
import unittest
import urllib.parse

from support import quiet_main  # noqa: E402
from support import (FakeRunner, TempDir, VERSION_OK, cdx_row, fake_fetch, fake_page, fixture_path, load_json,
                     png_bytes, png_size, snapshots_result, tp)

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    async_playwright = None

SITE = fixture_path("site")
THEN_TS = "20251024013522"
NOW_TS = "20261020120000"
ARCHIVE_RE = re.compile(r"^https://web\.archive\.org/web/(\d{14})([a-z]{2}_)?/(https?://[^/]+)(/.*)?$")
FAST = dict(scroll_pause_ms=100, settle_ms=300, idle_timeout_ms=3000)


def setUpModule():
    if async_playwright is None:
        raise unittest.SkipTest("Playwright is not installed")

    async def probe():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            await browser.close()

    try:
        asyncio.run(probe())
    except Exception as e:  # noqa: BLE001
        raise unittest.SkipTest("Chromium is not available: %s" % str(e).splitlines()[0])


class FixtureServer:
    def __init__(self):
        self.paths = []
        self.lock = threading.Lock()
        self.inflight = 0
        self.max_inflight = 0
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, ctype, body):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                with server.lock:
                    server.paths.append(path)
                if path.startswith("/img/reset-") or path == "/main-reset.html":
                    # close with RST and no response: "read ECONNRESET"
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    self.connection.close()
                    self.close_connection = True
                    return
                if path.startswith("/hang/") or path == "/main-hang.html":
                    time.sleep(2.5)  # longer than the test's request_timeout_ms
                    try:
                        return self._send(200, "image/png", png_bytes(20, 20))
                    except OSError:
                        return
                if path.startswith("/img/t-"):
                    return self._send(429, "text/plain", b"slow down")
                if path.startswith("/img/") and path.endswith(".png"):
                    return self._send(200, "image/png", png_bytes(60, 40, (40, 90, 160)))
                if path.endswith(".mp4"):
                    return self._send(200, "video/mp4", b"\x00" * 64)
                if path.startswith("/slow/"):
                    with server.lock:
                        server.inflight += 1
                        server.max_inflight = max(server.max_inflight, server.inflight)
                    time.sleep(0.4)
                    with server.lock:
                        server.inflight -= 1
                    return self._send(200, "image/png", png_bytes(20, 20))
                rel = os.path.normpath(path.lstrip("/"))
                full = os.path.join(SITE, rel)
                if rel.startswith("..") or not os.path.isfile(full):
                    return self._send(404, "text/plain", b"not found")
                ctype = {".html": "text/html; charset=utf-8", ".css": "text/css", ".txt": "text/plain"}.get(
                    os.path.splitext(full)[1], "application/octet-stream")
                with open(full, "rb") as fh:
                    return self._send(200, ctype, fh.read())

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        # a loopback port nobody listens on: connections to it are refused
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.refused_base = "http://127.0.0.1:%d" % probe.getsockname()[1]
        probe.close()
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def resolver(self):
        base, refused = self.base, self.refused_base

        def resolve(url):
            m = ARCHIVE_RE.match(url)
            if m:
                ts, _mod, origin, path = m.groups()
                if origin != "https://shop.test":
                    return None
                path = path or "/"
                if path == "/":
                    path = "/home-then.html" if ts.startswith("2025") else "/home-now.html"
                if path.startswith("/refused/") or path == "/main-refused.html":
                    return refused + path
                return base + path
            if url.startswith(base + "/"):
                return url
            return None

        return resolve


def archive(path, ts=THEN_TS):
    return tp.archive_url(ts, "https://shop.test/" + path)


class RendererCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FixtureServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def setUp(self):
        self._tmp = TempDir()
        self.out = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def render(self, fn, **cfg):
        """Run fn(renderer) inside a started renderer; returns (result, renderer)."""
        settings = dict(FAST)
        settings.update(cfg)
        renderer = tp.Renderer(tp.RenderConfig(**settings), resolve=self.server.resolver())

        async def go():
            await renderer.start()
            try:
                return await fn(renderer)
            finally:
                await renderer.close()

        result = asyncio.run(go())
        self.assertEqual(renderer.unrouted, [], "requests went unrouted")
        return result, renderer

    def side(self, name="side"):
        path = os.path.join(self.out, name)
        os.makedirs(path, exist_ok=True)
        return path

    def requests_log(self, side_dir):
        with open(os.path.join(side_dir, "requests.jsonl"), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


class ArchiveLoadTest(RendererCase):
    def test_screenshots_widths_and_fold_heights(self):
        d = self.side()
        res, _ = self.render(lambda r: r.load_archive(archive(""), d, ["desktop", "mobile"]))
        self.assertIsNone(res["error"])
        self.assertEqual(res["main_status"], 200)
        self.assertEqual(res["mobile_basis"], "desktop-capture-resized")
        self.assertEqual(png_size(os.path.join(d, "desktop-fold.png")), (1440, 900))
        self.assertEqual(png_size(os.path.join(d, "mobile-fold.png")), (390, 844))
        self.assertEqual(png_size(os.path.join(d, "desktop.png"))[0], 1440)
        self.assertEqual(png_size(os.path.join(d, "mobile.png"))[0], 390)
        self.assertGreater(png_size(os.path.join(d, "mobile.png"))[1], 844)
        self.assertEqual(res["facts"]["page"]["title"], "Shop Test | Fall Sale")
        self.assertIsNone(tp.detect_suspect(tp.suspect_signals(res["facts"], res["main_status"])))
        log = self.requests_log(d)
        self.assertTrue(any(e["url"].endswith("img/hero-then.png") and e["status"] == 200 for e in log))
        self.assertEqual(res["requests"]["leaked"], 0)

    def test_facts_strip_archive_prefixes(self):
        d = self.side()
        res, _ = self.render(lambda r: r.load_archive(archive(""), d, ["desktop"]))
        facts = res["facts"]
        self.assertEqual(facts["page"]["canonical"], "https://shop.test/")
        self.assertEqual([n["href"] for n in facts["nav"]][:2], ["https://shop.test/women", "https://shop.test/men"])
        self.assertTrue(facts["page"]["final_url"].startswith("https://web.archive.org/web/"))
        self.assertEqual(facts["headings"]["h2"], ["New Arrivals", "Best Sellers"])
        self.assertIn("Fall Sale: 30% off outerwear with code FALL30", facts["promos"])
        self.assertIn("Shop Women", facts["ctas"])
        self.assertEqual(facts["above_fold"]["viewport"], {"width": 1440, "height": 900})
        blocks = [b["text"] for b in facts["above_fold"]["text_blocks"]]
        self.assertLess(blocks.index("Fall Sale"), blocks.index("Up to 30% off jackets and boots"))
        h1 = [b for b in facts["above_fold"]["text_blocks"] if b["text"] == "Fall Sale"][0]
        self.assertEqual(h1["font_size"], 48)

    def test_lazy_images_load_after_scrolling(self):
        d = self.side()
        res, _ = self.render(lambda r: r.load_archive(archive("lazy.html"), d, ["desktop"]))
        loaded = {e["url"].rsplit("/", 1)[-1] for e in self.requests_log(d) if e["status"] == 200}
        for i in range(1, 7):
            self.assertIn("lazy-%d.png" % i, loaded)

    def test_challenge_and_outage_pages_are_flagged(self):
        for page, signature in (("challenge.html", "phrase-just-a-moment"), ("outage.html",
                                                                             "phrase-temporarily-offline")):
            with self.subTest(page=page):
                d = self.side(page)
                res, _ = self.render(lambda r: r.load_archive(archive(page), d, ["desktop"]))
                got = tp.detect_suspect(tp.suspect_signals(res["facts"], res["main_status"]))
                self.assertEqual(got["signature"], signature)

    def test_budget_aborts_and_counts(self):
        d = self.side()
        res, _ = self.render(lambda r: r.load_archive(archive("budget.html"), d, ["desktop"]), budget=10)
        log = self.requests_log(d)
        passed = [e for e in log if e["aborted"] is None]
        budget = [e for e in log if e["aborted"] == "budget"]
        self.assertEqual(len(passed), 10)
        self.assertGreaterEqual(len(budget), 21)
        self.assertEqual(res["requests"]["aborted_budget"], len(budget))
        served = [p for p in self.server.paths if p.startswith("/img/b-")]
        self.assertEqual(len(served), 9)

    def test_leaked_requests_are_aborted_and_counted(self):
        d = self.side()
        before = list(self.server.paths)
        res, _ = self.render(lambda r: r.load_archive(archive("leak.html"), d, ["desktop"]))
        log = self.requests_log(d)
        leaked = sorted(e["host"] for e in log if e["aborted"] == "leaked")
        self.assertEqual(leaked, ["cdn.leaky.example", "fonts.leaky.example", "www.google-analytics.com"])
        self.assertEqual(res["requests"]["leaked"], 3)
        self.assertGreaterEqual(res["requests"]["aborted_media"], 1)
        self.assertNotIn("/img/clip.mp4", self.server.paths[len(before):])
        self.assertTrue(any(e["url"].endswith("img/kept.png") and e["status"] == 200 for e in log))

    def test_at_most_4_archive_requests_in_flight(self):
        d = self.side()
        self.server.max_inflight = 0
        self.render(lambda r: r.load_archive(archive("slow.html"), d, ["desktop"]))
        self.assertLessEqual(self.server.max_inflight, 4)
        self.assertGreaterEqual(self.server.max_inflight, 2)

    def test_429s_mark_the_load_degraded(self):
        d = self.side()
        res, _ = self.render(lambda r: r.load_archive(archive("throttle.html"), d, ["desktop"]))
        self.assertEqual(res["requests"]["http_429"], 12)
        self.assertTrue(res["degraded"])

    def test_no_status_subrequests_count_toward_degraded(self):
        d = self.side()
        res, _ = self.render(lambda r: r.load_archive(archive("noresponse.html"), d, ["desktop"]),
                             request_timeout_ms=1000)
        self.assertIsNone(res["error"])
        self.assertEqual(res["requests"]["http_429"], 0)
        self.assertEqual(res["requests"]["network_errors"], 12)
        self.assertTrue(res["degraded"])
        self.assertIn("12 requests with no HTTP response", res["degraded_reason"])
        failed = [e for e in self.requests_log(d) if e["failure"]]
        self.assertTrue(all(e["status"] is None for e in failed))
        self.assertEqual(sorted(tp.network_error_text(e["failure"]) for e in failed),
                         ["connection refused"] * 4 + ["connection reset"] * 4 + ["timed out"] * 4)
        self.assertEqual(tp.classify_load(res, "archive")[0], "degraded")

    def test_no_status_failures_add_to_429s(self):
        for page, total, degraded in (("mixed.html", 11, True), ("mixed-ten.html", 10, False)):
            with self.subTest(page=page):
                d = self.side(page)
                res, _ = self.render(lambda r: r.load_archive(archive(page), d, ["desktop"]))
                self.assertEqual(res["requests"]["http_429"], 5)
                self.assertEqual(res["requests"]["network_errors"], total - 5)
                self.assertEqual(res["degraded"], degraded)

    def test_main_document_with_no_response_is_degraded_not_failed(self):
        for page, phrase in (("main-reset.html", "connection reset"), ("main-refused.html", "connection refused"),
                             ("main-hang.html", "timed out")):
            with self.subTest(page=page):
                d = self.side(page)
                res, _ = self.render(lambda r: r.load_archive(archive(page), d, ["desktop", "mobile"]),
                                     request_timeout_ms=1000)
                self.assertTrue(res["error"])
                self.assertIsNone(res["main_status"])
                self.assertIsNone(res["facts"])
                self.assertTrue(res["degraded"])
                self.assertIn("main document got no HTTP response (%s)" % phrase, res["degraded_reason"])
                self.assertEqual(tp.classify_load(res, "archive"), ("degraded", None))

    def test_tall_page_is_truncated_at_20000(self):
        d = self.side()
        res, _ = self.render(lambda r: r.load_archive(archive("tall.html"), d, ["desktop"]))
        self.assertEqual(png_size(os.path.join(d, "desktop.png")), (1440, 20000))
        self.assertTrue(res["screenshots"]["desktop"]["truncated"])
        self.assertFalse(res["screenshots"]["desktop-fold"]["truncated"])

    def test_pdp_and_plp_facts_match_expected(self):
        for page, expected in (("pdp.html", "pdp_facts.json"), ("plp.html", "plp_facts.json")):
            with self.subTest(page=page):
                d = self.side(page)
                res, _ = self.render(lambda r: r.load_archive(archive(page), d, ["desktop"]))
                facts = res["facts"]
                for dotted, value in load_json("expected", expected).items():
                    got = facts
                    for part in dotted.split("."):
                        got = got[part]
                    self.assertEqual(got, value, dotted)
                if page == "pdp.html":
                    self.assertEqual(len(facts["structured_data"]["errors"]), 1)
                    self.assertEqual(facts["structured_data"]["errors"][0]["index"], 1)


class LiveLoadTest(RendererCase):
    def test_consent_button_clicked_and_recorded(self):
        d = self.side()
        url = self.server.base + "/consent.html"
        res, _ = self.render(lambda r: r.load_live(url, d, "desktop"))
        self.assertEqual(res["consent"]["clicked"], "#onetrust-accept-btn-handler")
        self.assertTrue(res["consent"]["escape"])
        self.assertEqual(res["facts"]["page"]["title"], "Shop Test | Consent accepted")

    def test_exact_text_consent_button_without_vendor_ids(self):
        d = self.side()
        url = self.server.base + "/consent-text.html"
        res, _ = self.render(lambda r: r.load_live(url, d, "desktop"))
        self.assertEqual(res["consent"]["clicked"], 'button text "Accept all"')
        self.assertEqual(res["facts"]["page"]["title"], "Shop Test | Generic accepted")

    def test_no_banner_means_nothing_is_clicked(self):
        d = self.side()
        url = self.server.base + "/home-now.html"
        res, _ = self.render(lambda r: r.load_live(url, d, "desktop"))
        self.assertIsNone(res["consent"]["clicked"])
        self.assertTrue(res["consent"]["escape"])

    def test_mobile_device_profile(self):
        d = self.side()
        url = self.server.base + "/home-now.html"
        res, _ = self.render(lambda r: r.load_live(url, d, "mobile"))
        self.assertEqual(res["mobile_basis"], "mobile-device-profile")
        self.assertIn("iPhone", res["user_agent"])
        self.assertEqual(png_size(os.path.join(d, "mobile-fold.png")), (390, 844))
        self.assertEqual(png_size(os.path.join(d, "mobile.png"))[0], 390)
        self.assertIn(b"Holiday Gifts", res["main_body"])

    def test_desktop_live_keeps_the_stock_user_agent(self):
        d = self.side()
        url = self.server.base + "/home-now.html"
        res, _ = self.render(lambda r: r.load_live(url, d, "desktop"))
        self.assertIn("HeadlessChrome", res["user_agent"])
        self.assertEqual(png_size(os.path.join(d, "desktop-fold.png")), (1440, 900))

    def test_robots_fetch(self):
        (status, text), _ = self.render(lambda r: r.fetch_robots(self.server.base + "/robots.txt"))
        self.assertEqual(status, 200)
        self.assertIn("Disallow: /private/", text)


class EndToEndTest(RendererCase):
    def test_capture_builds_a_pack_with_compare_page_and_report(self):
        base = self.server.base
        csv_path = os.path.join(self.out, "pages.csv")
        with open(csv_path, "w", encoding="utf-8") as fh:
            fh.write("brand,page_type,label,now_url,then_url,notes\n")
            fh.write("Shop Test,home,home,%s/home-now.html,https://shop.test/,fixture\n" % base)

        def snaps(argv):
            frm = argv[argv.index("--from") + 1]
            ts = NOW_TS if frm.startswith("2026") else THEN_TS
            return snapshots_result([cdx_row(ts, original="https://shop.test/")])

        runner = FakeRunner({"--version": VERSION_OK, "snapshots": snaps, "fetch": fake_fetch, "page": fake_page})
        settings = dict(FAST)
        renderer = tp.Renderer(tp.RenderConfig(**settings), resolve=self.server.resolver())
        out = os.path.join(self.out, "out")
        code = quiet_main(["capture", csv_path, "--date", "2026-10-24", "--delay", "0", "-o", out, "--quiet",
                        "--wayback-salvage", __file__],
                       deps={"renderer": renderer, "runner": runner, "live_gap": 0, "ws_gap": 0})
        self.assertEqual(code, 0)
        self.assertEqual(renderer.unrouted, [])
        pack_dir = os.path.join(out, "shop-test", "home", "home", "2025-10-24")
        with open(os.path.join(pack_dir, "pack.json"), encoding="utf-8") as fh:
            pack = json.load(fh)
        self.assertEqual({k: v["status"] for k, v in pack["sides"].items()},
                         {"then": "done", "now-live": "done", "now-archive": "done"})
        self.assertEqual(pack["versions"]["teardown-pairs"], tp.VERSION)
        self.assertEqual(pack["versions"]["wayback-salvage"], "0.2.2")
        for side in ("then", "now-live", "now-archive"):
            for name in ("desktop.png", "desktop-fold.png", "mobile.png", "mobile-fold.png", "facts.json",
                         "capture.json", "requests.jsonl", "page.html"):
                self.assertTrue(os.path.exists(os.path.join(pack_dir, side, name)), "%s/%s" % (side, name))
        self.assertTrue(os.path.exists(os.path.join(pack_dir, "then", "analysis.json")))
        self.assertFalse(os.path.exists(os.path.join(pack_dir, "now-live", "analysis.json")))
        with open(os.path.join(pack_dir, "then", "capture.json"), encoding="utf-8") as fh:
            cap = json.load(fh)
        for key in ("requests", "failed", "aborted_budget", "leaked", "http_429", "bytes", "main_status"):
            self.assertIn(key, cap["requests"])
        with open(os.path.join(pack_dir, "diff.json"), encoding="utf-8") as fh:
            diff = json.load(fh)
        self.assertEqual([c["name"] for c in diff["comparisons"]], ["then-vs-now-live", "then-vs-now-archive"])
        title = diff["comparisons"][0]["facts"]["scalars"]["title"]
        self.assertEqual((title["then"], title["now"]), ("Shop Test | Fall Sale", "Shop Test | Holiday Gifts"))
        with open(os.path.join(pack_dir, "diff.md"), encoding="utf-8") as fh:
            md = fh.read()
        self.assertNotIn("\u2014", md)
        self.assertIn("Then is the archive crawler's desktop view resized to mobile width", md)
        for name in ("compare-desktop.png", "compare-mobile.png"):
            w, h = png_size(os.path.join(pack_dir, name))
            self.assertGreater(w, 0)
            self.assertGreater(h, 0)
        self.assertTrue(os.path.exists(os.path.join(out, "index.html")))
        self.assert_compare_renders(os.path.join(pack_dir, "compare.html"))
        self.assert_compare_renders(os.path.join(out, "index.html"))

    def assert_compare_renders(self, path):
        async def go():
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page(viewport={"width": 1500, "height": 1000})
                errors, external = [], []
                page.on("pageerror", lambda e: errors.append(str(e)))

                async def guard(route):
                    if route.request.url.startswith("file://"):
                        await route.continue_()
                    else:
                        external.append(route.request.url)
                        await route.abort()

                await page.route("**/*", guard)
                await page.goto("file://" + path)
                broken = await page.evaluate("""() => Array.from(document.images)
                    .filter(i => i.offsetParent !== null && !(i.complete && i.naturalWidth > 0))
                    .map(i => i.getAttribute('src'))""")
                shown = await page.evaluate("document.images.length")
                await browser.close()
                return errors, external, broken, shown

        errors, external, broken, shown = asyncio.run(go())
        self.assertEqual(errors, [])
        self.assertEqual(external, [], "%s must be self-contained" % os.path.basename(path))
        self.assertEqual(broken, [])
        self.assertGreater(shown, 0)


if __name__ == "__main__":
    unittest.main()
