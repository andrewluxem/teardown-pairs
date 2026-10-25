"""Spec 3.12 case 9: resume. A pack with then done and now-live failed redoes
only now-live and regenerates the diff. Also covers the other resumable
stops (--max-pages, throttling) and what a rerun leaves alone."""

import json
import os
import unittest

from support import (FakeClock, FakeRenderer, FakeRunner, TempDir, VERSION_OK, basic_facts, cdx_row,
                     fake_fetch, fake_page, snapshots_result, tp)

URL = "https://www.shop.example/"
PACK_REL = os.path.join("shop", "home", "home", "2025-10-24")
SNAPS = [cdx_row("20251024013522"), cdx_row("20251023224510"), cdx_row("20251025120000"),
         cdx_row("20251026120000")]


def make_runner(clock, snapshots=None):
    return FakeRunner({
        "--version": VERSION_OK,
        "snapshots": snapshots or snapshots_result(SNAPS),
        "fetch": fake_fetch,
        "page": fake_page,
    }, clock=clock)


def capture(out, renderer, runner, clock, *extra):
    args = ["capture", "--url", URL, "--brand", "Shop", "--type", "home", "--label", "home",
            "--date", "2026-10-24", "-o", out, "--quiet", "--wayback-salvage", __file__] + list(extra)
    return tp.main(args, deps={"renderer": renderer, "runner": runner, "clock": clock.time, "sleep": clock.sleep})


def read_pack(out):
    with open(os.path.join(out, PACK_REL, "pack.json"), encoding="utf-8") as fh:
        return json.load(fh)


def read_diff(out):
    with open(os.path.join(out, PACK_REL, "diff.json"), encoding="utf-8") as fh:
        return json.load(fh)


LIVE_FAIL = {"error": "net::ERR_CONNECTION_RESET", "screenshots": {}, "facts": None, "main_status": None,
             "main_body": None}


class ResumeTest(unittest.TestCase):
    def test_then_done_now_live_failed_redoes_only_now_live(self):
        with TempDir() as out:
            clock = FakeClock()
            r1 = FakeRenderer(clock=clock, live_results=[LIVE_FAIL, LIVE_FAIL])
            code = capture(out, r1, make_runner(clock), clock, "--now-source", "live")
            self.assertEqual(code, 0)
            pack = read_pack(out)
            self.assertEqual(pack["sides"]["then"]["status"], "done")
            self.assertEqual(pack["sides"]["now-live"]["status"], "failed")
            then_attempts = pack["sides"]["then"]["attempts"]
            self.assertEqual(len(then_attempts), 1)
            self.assertEqual(read_diff(out)["comparisons"], [])

            r2 = FakeRenderer(clock=clock)
            runner2 = make_runner(clock)
            code = capture(out, r2, runner2, clock, "--now-source", "live")
            self.assertEqual(code, 0)
            self.assertEqual([l[1] for l in r2.loads], ["live", "live"])
            self.assertEqual(runner2.names(), ["--version"])
            pack = read_pack(out)
            self.assertEqual(pack["sides"]["then"]["attempts"], then_attempts)
            self.assertEqual(pack["sides"]["now-live"]["status"], "done")
            diff = read_diff(out)
            self.assertEqual([c["name"] for c in diff["comparisons"]], ["then-vs-now-live"])
            self.assertTrue(os.path.exists(os.path.join(out, PACK_REL, "compare.html")))
            self.assertEqual(len(r2.compare_shots), 2)

    def test_rerun_of_a_finished_pack_loads_nothing_and_keeps_the_diff(self):
        with TempDir() as out:
            clock = FakeClock()
            capture(out, FakeRenderer(clock=clock), make_runner(clock), clock)
            before = read_pack(out)
            diff_path = os.path.join(out, PACK_REL, "diff.json")
            mtime = os.stat(diff_path).st_mtime_ns
            r2 = FakeRenderer(clock=clock)
            runner2 = make_runner(clock)
            self.assertEqual(capture(out, r2, runner2, clock), 0)
            self.assertEqual(r2.loads, [])
            self.assertEqual(r2.compare_shots, [])
            self.assertEqual(runner2.names(), ["--version"])
            after = read_pack(out)
            for name in ("then", "now-live", "now-archive"):
                self.assertEqual(after["sides"][name]["attempts"], before["sides"][name]["attempts"])
            self.assertEqual(os.stat(diff_path).st_mtime_ns, mtime)

    def test_blocked_live_side_is_not_retried_on_rerun(self):
        with TempDir() as out:
            clock = FakeClock()
            blocked = {"facts": basic_facts(title="Access Denied"), "main_status": 403}
            r1 = FakeRenderer(clock=clock, live_results=[blocked])
            capture(out, r1, make_runner(clock), clock, "--now-source", "live")
            pack = read_pack(out)
            side = pack["sides"]["now-live"]
            self.assertEqual(side["status"], "blocked")
            self.assertTrue(side["live_blocked"])
            self.assertEqual(side["signature"], "status-403")
            # no retries when blocked: the mobile load is not attempted either
            self.assertEqual([l[3] for l in r1.loads if l[1] == "live"], ["desktop"])
            r2 = FakeRenderer(clock=clock)
            capture(out, r2, make_runner(clock), clock, "--now-source", "live")
            self.assertEqual(r2.loads, [])

    def test_max_pages_stops_with_exit_2_and_resumes(self):
        with TempDir() as out:
            clock = FakeClock()
            r1 = FakeRenderer(clock=clock)
            code = capture(out, r1, make_runner(clock), clock, "--now-source", "live", "--max-pages", "1")
            self.assertEqual(code, tp.EXIT_MAX_PAGES)
            self.assertEqual(len(r1.loads), 1)
            pack = read_pack(out)
            self.assertEqual(pack["sides"]["then"]["status"], "done")
            self.assertEqual(pack["sides"]["now-live"]["status"], "planned")
            self.assertTrue(os.path.exists(os.path.join(out, "index.html")))
            r2 = FakeRenderer(clock=clock)
            code = capture(out, r2, make_runner(clock), clock, "--now-source", "live", "--max-pages", "4")
            self.assertEqual(code, 0)
            self.assertEqual([l[1] for l in r2.loads], ["live", "live"])

    def test_three_degraded_loads_stop_with_exit_3_after_saving_state(self):
        with TempDir() as out:
            clock = FakeClock()
            degraded = {"degraded": True}
            r1 = FakeRenderer(clock=clock, archive_results=[degraded, degraded, degraded])
            code = capture(out, r1, make_runner(clock), clock, "--now-source", "archive")
            self.assertEqual(code, tp.EXIT_THROTTLED)
            pack = read_pack(out)
            then = pack["sides"]["then"]
            self.assertEqual(then["status"], "failed")
            self.assertEqual([a["outcome"] for a in then["attempts"]], ["degraded"] * 3)
            self.assertEqual(pack["sides"]["now-archive"]["status"], "planned")
            r2 = FakeRenderer(clock=clock)
            self.assertEqual(capture(out, r2, make_runner(clock), clock, "--now-source", "archive"), 0)
            self.assertEqual(read_pack(out)["sides"]["then"]["status"], "done")
            self.assertEqual(len(read_pack(out)["sides"]["then"]["attempts"]), 4)

    def test_pack_dir_holding_another_pack_is_refused(self):
        with TempDir() as out:
            clock = FakeClock()
            capture(out, FakeRenderer(clock=clock), make_runner(clock), clock)
            path = os.path.join(out, PACK_REL, "pack.json")
            with open(path, encoding="utf-8") as fh:
                pack = json.load(fh)
            pack["date"] = "2026-01-01"
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(pack, fh)
            r2 = FakeRenderer(clock=clock)
            code = capture(out, r2, make_runner(clock), clock)
            self.assertEqual(code, tp.EXIT_ERROR)
            self.assertEqual(r2.loads, [])

    def test_second_years_back_pack_reuses_the_now_sides(self):
        with TempDir() as out:
            clock = FakeClock()

            def snaps(argv):
                frm = argv[argv.index("--from") + 1]
                year = frm[:4]
                if frm.startswith("2026"):
                    return snapshots_result([cdx_row("20261020120000")])
                return snapshots_result([cdx_row(year + "1024013522")])

            runner = FakeRunner({"--version": VERSION_OK, "snapshots": snaps, "fetch": fake_fetch,
                                 "page": fake_page}, clock=clock)
            r = FakeRenderer(clock=clock)
            code = capture(out, r, runner, clock, "--years-back", "1,2")
            self.assertEqual(code, 0)
            kinds = [l[1] for l in r.loads]
            self.assertEqual(kinds.count("live"), 2)
            self.assertEqual(kinds.count("archive"), 3)
            other = os.path.join(out, "shop", "home", "home", "2024-10-24", "pack.json")
            with open(other, encoding="utf-8") as fh:
                pack2 = json.load(fh)
            self.assertEqual(pack2["sides"]["now-live"]["status"], "done")
            self.assertIn("reused_from", pack2["sides"]["now-live"])
            self.assertTrue(os.path.exists(os.path.join(out, "shop", "home", "home", "2024-10-24", "now-live",
                                                        "desktop.png")))


if __name__ == "__main__":
    unittest.main()
