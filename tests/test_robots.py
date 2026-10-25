"""Spec 3.12 case 6: robots handling with fixture robots.txt files."""

import datetime
import json
import os
import unittest

from support import quiet_main  # noqa: E402
from support import FakeClock, FakeRenderer, FakeRunner, TempDir, VERSION_OK, fixture_path, run, tp


def robots_text(name):
    with open(fixture_path("robots", name), encoding="utf-8") as fh:
        return fh.read()


class RobotsVerdictTest(unittest.TestCase):
    def test_allowed(self):
        allowed, reason = tp.robots_verdict("https://www.shop.example/c/jackets", 200, robots_text("allowed.txt"))
        self.assertTrue(allowed)
        self.assertEqual(reason, "allowed")

    def test_allowed_file_still_disallows_its_paths(self):
        allowed, reason = tp.robots_verdict("https://www.shop.example/checkout", 200, robots_text("allowed.txt"))
        self.assertFalse(allowed)
        self.assertEqual(reason, "disallowed")

    def test_disallowed(self):
        allowed, reason = tp.robots_verdict("https://www.shop.example/", 200, robots_text("disallowed.txt"))
        self.assertFalse(allowed)
        self.assertEqual(reason, "disallowed")

    def test_missing(self):
        for status in (404, 410):
            allowed, reason = tp.robots_verdict("https://www.shop.example/", status, "")
            self.assertTrue(allowed)
            self.assertEqual(reason, "missing")

    def test_rules_for_other_agents_do_not_apply_to_star(self):
        allowed, _ = tp.robots_verdict("https://www.shop.example/", 200, robots_text("other-agent-only.txt"))
        self.assertTrue(allowed)

    def test_forbidden_and_unreachable_disallow(self):
        self.assertEqual(tp.robots_verdict("https://a.example/", 403, ""), (False, "robots-forbidden"))
        self.assertEqual(tp.robots_verdict("https://a.example/", 503, ""), (False, "robots-unreachable"))
        self.assertEqual(tp.robots_verdict("https://a.example/", None, ""), (False, "robots-unreachable"))

    def test_detail_is_the_http_status_or_the_error_in_plain_words(self):
        self.assertEqual(tp.robots_detail(403, None), "HTTP 403")
        self.assertEqual(tp.robots_detail(200, None), "HTTP 200")
        self.assertEqual(tp.robots_detail(None, "APIRequestContext.get: read ECONNRESET"), "connection reset")
        self.assertEqual(tp.robots_detail(None, "APIRequestContext.get: connect ECONNREFUSED 23.1.2.3:443"),
                         "connection refused")
        self.assertEqual(tp.robots_detail(None, "APIRequestContext.get: Timeout 20000ms exceeded."), "timed out")
        self.assertEqual(tp.robots_detail(None, "APIRequestContext.get: unexpected thing"),
                         "APIRequestContext.get: unexpected thing")

    def test_robots_url(self):
        self.assertEqual(tp.robots_url("https://www.shop.example/c/x?y=1"), "https://www.shop.example/robots.txt")
        self.assertEqual(tp.robots_url("http://127.0.0.1:8123/live/a.html"), "http://127.0.0.1:8123/robots.txt")


class RobotsInCaptureTest(unittest.TestCase):
    """A live side is skipped (live_skipped=robots) unless --ignore-robots."""

    def _capture(self, out, robots, extra_args=(), result=None):
        clock = FakeClock()
        renderer = FakeRenderer(clock=clock)

        async def fetch_robots(url):
            renderer.robots.append(url)
            return result or (200, robots_text(robots), None)

        renderer.fetch_robots = fetch_robots
        runner = FakeRunner({"--version": VERSION_OK}, clock=clock)
        args = ["now-only", "--url", "https://www.shop.example/", "--brand", "Shop", "--type", "home",
                "--label", "home", "--date", "2026-10-24", "-o", out, "--quiet"] + list(extra_args)
        code = quiet_main(args, deps={"renderer": renderer, "runner": runner, "clock": clock.time,
                                   "sleep": clock.sleep})
        pack = os.path.join(out, "shop", "home", "home", "2026-10-24", "pack.json")
        with open(pack, encoding="utf-8") as fh:
            return code, renderer, json.load(fh)

    def test_disallowed_skips_the_live_load(self):
        with TempDir() as out:
            code, renderer, pack = self._capture(out, "disallowed.txt")
            self.assertEqual(code, 0)
            self.assertEqual(renderer.loads, [])
            side = pack["sides"]["now-live"]
            self.assertEqual(side["status"], "skipped")
            self.assertEqual(side["live_skipped"], "robots")
            self.assertEqual(renderer.robots, ["https://www.shop.example/robots.txt"])

    def test_ignore_robots_loads_anyway(self):
        with TempDir() as out:
            code, renderer, pack = self._capture(out, "disallowed.txt", ["--ignore-robots"])
            self.assertEqual(code, 0)
            self.assertEqual([l[1] for l in renderer.loads], ["live", "live"])
            self.assertEqual(renderer.robots, [])
            self.assertEqual(pack["sides"]["now-live"]["status"], "done")

    def test_allowed_loads(self):
        with TempDir() as out:
            code, renderer, pack = self._capture(out, "allowed.txt")
            self.assertEqual([l[3] for l in renderer.loads], ["desktop", "mobile"])
            side = pack["sides"]["now-live"]
            self.assertEqual(side["robots"], "allowed")
            self.assertEqual(side["robots_status"], 200)
            self.assertIsNone(side["robots_error"])
            self.assertEqual(side["robots_url"], "https://www.shop.example/robots.txt")

    def test_skip_reasons_carry_the_status_or_the_error(self):
        reset = "APIRequestContext.get: read ECONNRESET"
        cases = (
            ((403, "Forbidden", None), "robots-forbidden (HTTP 403)", 403, None),
            ((503, "", None), "robots-unreachable (HTTP 503)", 503, None),
            ((None, "", reset), "robots-unreachable (connection reset)", None, reset),
            ((200, robots_text("disallowed.txt"), None), "disallowed (HTTP 200)", 200, None),
        )
        for result, phrase, status, error in cases:
            with self.subTest(phrase=phrase), TempDir() as out:
                code, renderer, pack = self._capture(out, "allowed.txt", result=result)
                self.assertEqual(code, 0)
                self.assertEqual(renderer.loads, [])
                side = pack["sides"]["now-live"]
                self.assertEqual(side["status"], "skipped")
                self.assertEqual(side["robots_status"], status)
                self.assertEqual(side["robots_error"], error)
                self.assertIn("robots.txt for * says %s;" % phrase, side["reason"])
                with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
                    self.assertIn(phrase, fh.read())

    def test_ignore_robots_records_no_robots_status(self):
        with TempDir() as out:
            _, renderer, pack = self._capture(out, "disallowed.txt", ["--ignore-robots"])
            side = pack["sides"]["now-live"]
            self.assertNotIn("robots_status", side)
            self.assertNotIn("robots_error", side)

    def test_rerun_with_ignore_robots_drops_the_old_robots_record(self):
        with TempDir() as out:
            _, _, pack = self._capture(out, "allowed.txt", result=(None, "", "APIRequestContext.get: read ECONNRESET"))
            self.assertEqual(pack["sides"]["now-live"]["robots_error"], "APIRequestContext.get: read ECONNRESET")
            _, renderer, pack = self._capture(out, "allowed.txt", ["--ignore-robots"])
            side = pack["sides"]["now-live"]
            self.assertEqual(side["status"], "done")
            self.assertEqual(side["robots"], "ignored (--ignore-robots)")
            for key in ("robots_url", "robots_status", "robots_error"):
                self.assertNotIn(key, side)


if __name__ == "__main__":
    unittest.main()
