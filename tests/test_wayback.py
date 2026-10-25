"""Spec 3.12 case 10: the wayback-salvage wrapper. Parses recorded --json
fixtures for snapshots, fetch and page; the version check rejects 0.1.0;
wayback-salvage exits 2 and 3 propagate as teardown-pairs exits 2 and 3."""

import json
import os
import unittest

from support import quiet_main  # noqa: E402
from support import (FakeClock, FakeRenderer, FakeRunner, TempDir, VERSION_OK, record_result, run, tp,
                     ws_record)


def wrapper(out, runner, clock=None):
    clock = clock or FakeClock()
    runner.clock = clock
    return tp.WaybackSalvage("/opt/ws/wayback_salvage.py", out_dir=out, runner=runner,
                             clock=clock.time, sleep=clock.sleep)


class ResolvePathTest(unittest.TestCase):
    def _touch(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("# stub\n")
        return path

    def test_precedence(self):
        with TempDir() as d:
            home = os.path.join(d, "home")
            repo = os.path.join(d, "sites", "teardown-pairs")
            os.makedirs(repo)
            sibling = self._touch(os.path.join(d, "sites", "wayback-salvage", "scripts", "wayback_salvage.py"))
            self.assertEqual(tp.resolve_ws_path(None, {}, home, repo), sibling)
            skill = self._touch(os.path.join(home, ".claude", "skills", "wayback-salvage", "scripts",
                                             "wayback_salvage.py"))
            self.assertEqual(tp.resolve_ws_path(None, {}, home, repo), skill)
            env_path = self._touch(os.path.join(d, "env", "wayback_salvage.py"))
            self.assertEqual(tp.resolve_ws_path(None, {"WAYBACK_SALVAGE": env_path}, home, repo), env_path)
            arg = self._touch(os.path.join(d, "arg", "wayback_salvage.py"))
            self.assertEqual(tp.resolve_ws_path(arg, {"WAYBACK_SALVAGE": env_path}, home, repo), arg)

    def test_explicit_missing_path_does_not_fall_back(self):
        with TempDir() as d:
            home = os.path.join(d, "home")
            self._touch(os.path.join(home, ".claude", "skills", "wayback-salvage", "scripts", "wayback_salvage.py"))
            with self.assertRaises(tp.WaybackMissing):
                tp.resolve_ws_path(os.path.join(d, "nope.py"), {}, home, d)
            with self.assertRaises(tp.WaybackMissing):
                tp.resolve_ws_path(None, {"WAYBACK_SALVAGE": os.path.join(d, "nope.py")}, home, d)

    def test_nothing_found(self):
        with TempDir() as d:
            with self.assertRaises(tp.WaybackMissing):
                tp.resolve_ws_path(None, {}, os.path.join(d, "home"), os.path.join(d, "repo"))


class VersionTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(tp.parse_ws_version(record_result("version")[1]), (0, 2, 1))
        self.assertEqual(tp.parse_ws_version("wayback-salvage 0.10.3\n"), (0, 10, 3))
        self.assertIsNone(tp.parse_ws_version("something else"))

    def _check(self, out_text, code=0):
        with TempDir() as d:
            ws = wrapper(d, FakeRunner({"--version": (code, out_text, "")}))
            return run(ws.check_version())

    def test_rejects_old_versions(self):
        for text in ("wayback-salvage 0.1.0\n", "wayback-salvage 0.2.0\n", "garbage\n"):
            with self.subTest(text=text):
                with self.assertRaises(tp.WaybackMissing) as cm:
                    self._check(text)
                self.assertIn("0.2.1", str(cm.exception))

    def test_rejects_failed_version_call(self):
        with self.assertRaises(tp.WaybackMissing):
            self._check("", code=1)

    def test_accepts_current_and_newer(self):
        for text in ("wayback-salvage 0.2.1\n", "wayback-salvage 0.3.0\n", "wayback-salvage 1.0.0\n"):
            with self.subTest(text=text):
                self.assertEqual(self._check(text), text.split()[1])


class ParseTest(unittest.TestCase):
    def test_snapshots(self):
        with TempDir() as d:
            runner = FakeRunner({"snapshots": record_result("snapshots")})
            ws = wrapper(d, runner)
            rows = run(ws.snapshots("https://www.shop.example/", "20251017", "20251031"))
            self.assertEqual(len(rows), 5)
            self.assertEqual(set(rows[0]), {"timestamp", "original", "status", "mimetype", "digest", "length",
                                            "wayback_url"})
            argv = runner.calls[0][1]
            self.assertEqual(argv[:8], ["snapshots", "https://www.shop.example/", "--from", "20251017", "--to",
                                        "20251031", "--status", "200"])
            self.assertIn("--json", argv)
            self.assertEqual(argv[argv.index("-o") + 1], d)

    def test_snapshots_prefix_lookup_args(self):
        with TempDir() as d:
            runner = FakeRunner({"snapshots": record_result("snapshots_empty")})
            ws = wrapper(d, runner)
            rows = run(ws.snapshots("https://www.shop.example/c/x", "20250924", "20251123", match="prefix",
                                    limit=20, collapse="urlkey"))
            self.assertEqual(rows, [])
            argv = runner.calls[0][1]
            for flag, value in (("--match", "prefix"), ("--limit", "20"), ("--collapse", "urlkey")):
                self.assertEqual(argv[argv.index(flag) + 1], value)

    def test_fetch(self):
        with TempDir() as d:
            runner = FakeRunner({"fetch": record_result("fetch")})
            ws = wrapper(d, runner)
            info = run(ws.fetch("https://www.shop.example/", "20251024013522", "/abs/then/page.html"))
            self.assertEqual(info["http_status"], 200)
            self.assertEqual(info["timestamp"], "20251024013522")
            argv = runner.calls[0][1]
            self.assertEqual(argv[:6], ["fetch", "https://www.shop.example/", "-t", "20251024013522", "--save",
                                        "/abs/then/page.html"])

    def test_page(self):
        with TempDir() as d:
            ws = wrapper(d, FakeRunner({"page": record_result("page")}))
            info = run(ws.page("https://www.shop.example/", "20251024013522"))
            self.assertEqual(info["script_src_host"], [["www.googletagmanager.com", 2], ["cdn.cookielaw.org", 1]])
            self.assertEqual(info["script_text_host"], [["schema.org", 1], ["www.facebook.com", 1]])
            self.assertEqual(tp.martech_from_analysis(info), {
                "source": "wayback-salvage page --json",
                "loaded": [["www.googletagmanager.com", 2], ["cdn.cookielaw.org", 1]],
                "mentioned": [["schema.org", 1], ["www.facebook.com", 1]],
            })

    def test_images_args(self):
        with TempDir() as d:
            summary = {"page": "https://www.shop.example/", "status": {"ok": 3}, "exit_code": 0}
            runner = FakeRunner({"images": (0, json.dumps(summary), "")})
            ws = wrapper(d, runner)
            got = run(ws.images("https://www.shop.example/", "20251024013522", "/abs/then/images"))
            self.assertEqual(got["status"], {"ok": 3})
            argv = runner.calls[0][1]
            self.assertEqual(argv[:6], ["images", "https://www.shop.example/", "-t", "20251024013522", "-o",
                                        "/abs/then/images"])

    def test_recorded_fixtures_are_from_0_2_1(self):
        for name in ("snapshots", "fetch", "page", "page_exit2", "page_exit3"):
            self.assertEqual(ws_record(name)["wayback_salvage_version"], "0.2.1")


class ExitCodeTest(unittest.TestCase):
    def _page(self, result):
        with TempDir() as d:
            ws = wrapper(d, FakeRunner({"page": result}))
            return run(ws.page("https://www.shop.example/", "20251024013522"))

    def test_exit_3_is_a_throttle_stop(self):
        with self.assertRaises(tp.StopRun) as cm:
            self._page(record_result("page_exit3"))
        self.assertEqual(cm.exception.code, 3)
        self.assertIn("throttling", str(cm.exception))

    def test_exit_2_is_a_cap_stop(self):
        with self.assertRaises(tp.StopRun) as cm:
            self._page(record_result("page_exit2"))
        self.assertEqual(cm.exception.code, 2)

    def test_other_failures_are_errors(self):
        for result in ((1, "", "error: refusing a prefix listing on shared host\n"), (4, "", "TLS\n"),
                       (0, "not json", "")):
            with self.subTest(result=result):
                with self.assertRaises(tp.WaybackError):
                    self._page(result)


class MainExitTest(unittest.TestCase):
    def _main(self, args, runner):
        clock = FakeClock()
        runner.clock = clock
        with TempDir() as out:
            return quiet_main(args + ["-o", out, "--quiet", "--wayback-salvage", __file__],
                           deps={"runner": runner, "renderer": FakeRenderer(clock=clock), "clock": clock.time,
                                 "sleep": clock.sleep})

    SINGLE = ["--url", "https://www.shop.example/", "--brand", "Shop", "--type", "home", "--label", "home",
              "--date", "2026-10-24"]

    def test_coverage_propagates_exit_3(self):
        runner = FakeRunner({"--version": VERSION_OK,
                             "snapshots": (3, "", "stopped: archive.org kept throttling or failing (x)\n")})
        self.assertEqual(self._main(["coverage"] + self.SINGLE, runner), 3)

    def test_capture_propagates_exit_2(self):
        runner = FakeRunner({"--version": VERSION_OK, "snapshots": record_result("page_exit2")})
        self.assertEqual(self._main(["capture"] + self.SINGLE, runner), 2)

    def test_old_version_exits_4(self):
        runner = FakeRunner({"--version": (0, "wayback-salvage 0.1.0\n", "")})
        self.assertEqual(self._main(["coverage"] + self.SINGLE, runner), 4)
        self.assertEqual(runner.names(), ["--version"])

    def test_missing_wayback_salvage_exits_4(self):
        clock = FakeClock()
        with TempDir() as out:
            code = quiet_main(["coverage"] + self.SINGLE + ["-o", out, "--quiet", "--wayback-salvage",
                                                         os.path.join(out, "missing.py")],
                           deps={"runner": FakeRunner({"--version": VERSION_OK}), "clock": clock.time,
                                 "sleep": clock.sleep})
        self.assertEqual(code, 4)


if __name__ == "__main__":
    unittest.main()
