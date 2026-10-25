"""Archive request accounting in _Tracker, with fake routes (no browser).

Subrequests that fail with no HTTP status (connection refused, reset,
timed out) count toward the degraded threshold alongside 429s, and a main
document that fails that way makes the load degraded, not failed."""

import unittest

from support import run, tp  # noqa: E402

REFUSED = "Route.fetch: connect ECONNREFUSED 207.241.224.2:443"
RESET = "Route.fetch: read ECONNRESET"
TIMEOUT = "Route.fetch: Timeout 30000ms exceeded."


class FakeFrame:
    pass


class FakePage:
    def __init__(self):
        self.main_frame = FakeFrame()


class FakeRequest:
    def __init__(self, url, frame, nav=False, resource_type="image"):
        self.url = url
        self.frame = frame
        self.resource_type = "document" if nav else resource_type
        self._nav = nav

    def is_navigation_request(self):
        return self._nav


class FakeResponse:
    def __init__(self, status, body=b"x"):
        self.status = status
        self.headers = {"content-type": "image/png"}
        self._body = body

    async def body(self):
        return self._body

    async def dispose(self):
        pass


class FakeRoute:
    def __init__(self, request, outcome):
        self.request = request
        self.outcome = outcome  # an int status, or an error text to raise
        self.aborted = None
        self.fulfilled = None

    async def fetch(self, url=None, max_redirects=None, timeout=None):
        if isinstance(self.outcome, str):
            if "Timeout" in self.outcome:
                raise TimeoutError(self.outcome)
            raise RuntimeError(self.outcome)
        return FakeResponse(self.outcome)

    async def abort(self, code="aborted"):
        self.aborted = code

    async def fulfill(self, status=None, headers=None, body=None):
        self.fulfilled = status


def tracker():
    t = tp._Tracker("archive", "archive", tp.RenderConfig(), lambda url: url, [])
    t.page = FakePage()
    return t


def load(outcomes, main=200):
    """Run one fake archive load: the main document, then one subrequest
    per outcome. Returns the tracker and the main document's route."""
    t = tracker()
    main_route = FakeRoute(FakeRequest("https://web.archive.org/web/20251024013522if_/https://shop.test/",
                                       t.page.main_frame, nav=True), main)

    async def go():
        await t.handle(main_route)
        for i, outcome in enumerate(outcomes):
            req = FakeRequest("https://web.archive.org/web/20251024013522im_/https://shop.test/i%d.png" % i,
                              t.page.main_frame)
            await t.handle(FakeRoute(req, outcome))

    run(go())
    return t, main_route


class NoStatusAccountingTest(unittest.TestCase):
    def test_each_kind_of_no_status_failure_is_counted_and_logged(self):
        t, _ = load([REFUSED, RESET, TIMEOUT, 200, 404])
        s = t.summary()
        self.assertEqual(s["network_errors"], 3)
        self.assertEqual(s["http_429"], 0)
        self.assertEqual(s["failed"], 4)
        self.assertIsNone(s["main_error"])
        failures = [r for r in t.order if r["failure"]]
        self.assertEqual([r["status"] for r in failures], [None, None, None])
        self.assertEqual([r["failure"] for r in failures], [REFUSED, RESET, TIMEOUT])

    def test_aborts_are_not_network_errors(self):
        t = tracker()

        async def go():
            for url in ("https://cdn.example/x.js", "https://web.archive.org/web/1im_/https://shop.test/v.mp4"):
                await t.handle(FakeRoute(FakeRequest(url, t.page.main_frame), REFUSED))

        run(go())
        self.assertEqual(t.summary()["network_errors"], 0)
        self.assertEqual([r["aborted"] for r in t.order], ["leaked", "media"])

    def test_main_document_failure_is_recorded(self):
        for text in (REFUSED, RESET, TIMEOUT):
            with self.subTest(error=text):
                t, main = load([], main=text)
                s = t.summary()
                self.assertEqual(s["main_error"], text)
                self.assertEqual(s["network_errors"], 1)
                self.assertIsNone(s["main_status"])
                self.assertEqual(main.aborted, "failed")


class DegradedVerdictTest(unittest.TestCase):
    def test_no_status_failures_count_with_429s(self):
        self.assertFalse(tp.degraded_verdict(0, 10, None)[0])
        self.assertTrue(tp.degraded_verdict(0, 11, None)[0])
        self.assertFalse(tp.degraded_verdict(5, 5, None)[0])
        self.assertTrue(tp.degraded_verdict(6, 5, None)[0])
        self.assertTrue(tp.degraded_verdict(11, 0, None)[0])
        self.assertFalse(tp.degraded_verdict(10, 0, None)[0])

    def test_reason_names_the_counts(self):
        degraded, reason = tp.degraded_verdict(4, 8, None)
        self.assertTrue(degraded)
        self.assertIn("4 HTTP 429s", reason)
        self.assertIn("8 requests with no HTTP response", reason)

    def test_main_document_with_no_response_is_degraded(self):
        degraded, reason = tp.degraded_verdict(0, 1, RESET)
        self.assertTrue(degraded)
        self.assertIn("main document got no HTTP response (connection reset)", reason)
        self.assertEqual(tp.degraded_verdict(0, 0, None), (False, None))


class NetworkErrorTextTest(unittest.TestCase):
    def test_known_errors_get_plain_words(self):
        for text, phrase in ((REFUSED, "connection refused"), (RESET, "connection reset"),
                             ("APIRequestContext.get: socket hang up", "connection reset"),
                             (TIMEOUT, "timed out"), ("net::ERR_CONNECTION_REFUSED", "connection refused"),
                             ("net::ERR_TIMED_OUT", "timed out"),
                             ("APIRequestContext.get: getaddrinfo ENOTFOUND www.shop.example", "DNS lookup failed")):
            with self.subTest(text=text):
                self.assertEqual(tp.network_error_text(text), phrase)

    def test_unknown_errors_keep_their_first_line(self):
        self.assertEqual(tp.network_error_text("Route.fetch: something odd\nstack"), "Route.fetch: something odd")
        self.assertEqual(tp.network_error_text(""), "no response")


class TrackerFinishTest(unittest.TestCase):
    """Renderer._finish sets res['degraded'] from the tracker's counts."""

    def _finish(self, t):
        import tempfile

        r = tp.Renderer()

        class Page:
            async def unroute_all(self, behavior=None):
                pass

        res = {}
        with tempfile.TemporaryDirectory() as d:
            run(r._finish(Page(), t, d, res))
        return res

    def test_main_failure_degrades_the_load(self):
        t, _ = load([], main=REFUSED)
        res = self._finish(t)
        self.assertTrue(res["degraded"])
        self.assertIn("connection refused", res["degraded_reason"])
        res["error"] = "Page.goto: net::ERR_FAILED at https://web.archive.org/web/20251024013522if_/https://shop.test/"
        self.assertEqual(tp.classify_load(res, "archive"), ("degraded", None))

    def test_eleven_no_status_subrequests_degrade_the_load(self):
        t, _ = load([RESET] * 6 + [429] * 5)
        res = self._finish(t)
        self.assertTrue(res["degraded"])
        self.assertEqual(res["requests"]["network_errors"], 6)
        self.assertEqual(res["requests"]["http_429"], 5)

    def test_ten_is_not_degraded(self):
        t, _ = load([RESET] * 5 + [429] * 5)
        self.assertFalse(self._finish(t)["degraded"])

    def test_live_loads_are_never_degraded(self):
        t = tp._Tracker("live", "live-desktop", tp.RenderConfig(), lambda url: url, [])
        t.network_errors, t.main_error = 20, RESET
        self.assertFalse(self._finish(t)["degraded"])


if __name__ == "__main__":
    unittest.main()
