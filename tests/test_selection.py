"""Spec 3.12 case 2: capture candidate selection."""

import datetime
import unittest

from support import FakeClock, FakeRunner, TempDir, VERSION_OK, cdx_row, run, snapshots_result, tp

T = datetime.date(2025, 10, 24)


def stamps(selection):
    return [c["timestamp"] for c in selection["candidates"]]


class SelectCandidatesTest(unittest.TestCase):
    def test_nearest_wins(self):
        rows = [cdx_row("20251020120000"), cdx_row("20251026120000"), cdx_row("20251023120000")]
        sel = tp.select_candidates(rows, T)
        self.assertEqual(stamps(sel), ["20251023120000", "20251026120000", "20251020120000"])

    def test_ties_go_to_the_earlier_capture(self):
        rows = [cdx_row("20251025080000"), cdx_row("20251023200000")]
        self.assertEqual(stamps(tp.select_candidates(rows, T))[0], "20251023200000")

    def test_same_day_ties_go_to_the_earlier_time(self):
        rows = [cdx_row("20251024150000"), cdx_row("20251024010000")]
        self.assertEqual(stamps(tp.select_candidates(rows, T)), ["20251024010000", "20251024150000"])

    def test_non_html_and_non_200_rows_are_dropped(self):
        rows = [
            cdx_row("20251024000100", status="301"),
            cdx_row("20251024000200", mimetype="application/json"),
            cdx_row("20251024000300", mimetype="warc/revisit"),
            cdx_row("20251024000400", status="-"),
            cdx_row("20251024000500", mimetype="text/html; charset=utf-8"),
            cdx_row("20251024000600"),
        ]
        self.assertEqual(stamps(tp.select_candidates(rows, T)), ["20251024000500", "20251024000600"])

    def test_window_7_holds_only_rows_within_7_days(self):
        rows = [cdx_row("20251021000000"), cdx_row("20251113000000")]
        sel = tp.select_candidates(rows, T)
        self.assertEqual(sel["window_days"], 7)
        self.assertEqual(stamps(sel), ["20251021000000"])

    def test_widens_to_30_when_7_is_empty(self):
        rows = [cdx_row("20251113000000"), cdx_row("20251010000000")]
        sel = tp.select_candidates(rows, T)
        self.assertEqual(sel["window_days"], 30)
        self.assertEqual(stamps(sel), ["20251010000000", "20251113000000"])

    def test_90_only_with_wide(self):
        rows = [cdx_row("20251223000000")]  # 60 days after
        self.assertEqual(tp.select_candidates(rows, T), {"window_days": None, "candidates": []})
        sel = tp.select_candidates(rows, T, wide=True)
        self.assertEqual(sel["window_days"], 90)
        self.assertEqual(stamps(sel), ["20251223000000"])

    def test_nothing_beyond_90(self):
        rows = [cdx_row("20260301000000")]
        self.assertEqual(tp.select_candidates(rows, T, wide=True)["candidates"], [])

    def test_records_distance_and_weekdays(self):
        cand = tp.select_candidates([cdx_row("20251023224510")], T)["candidates"][0]
        self.assertEqual(cand["distance_days"], 1)
        self.assertEqual(cand["offset_days"], -1)
        self.assertEqual(cand["date"], "2025-10-23")
        self.assertEqual(cand["target_weekday"], "Fri")
        self.assertEqual(cand["capture_weekday"], "Thu")
        self.assertEqual(cand["original"], "https://www.shop.example/")

    def test_now_archive_window_is_minus_30_to_0_newest_first(self):
        d = datetime.date(2026, 10, 24)
        rows = [cdx_row(ts) for ts in ("20261001000000", "20261024090000", "20261020000000",
                                         "20261025000000", "20260901000000", "20261024010000")]
        sel = tp.select_now_candidates(rows, d)
        self.assertEqual(stamps(sel), ["20261024090000", "20261024010000", "20261020000000", "20261001000000"])
        self.assertEqual(sel["candidates"][2]["distance_days"], 4)

    def test_cdx_windows(self):
        self.assertEqual(tp.cdx_window(T, 7), ("20251017", "20251031"))
        self.assertEqual(tp.cdx_window(T, 30), ("20250924", "20251123"))
        self.assertEqual(tp.now_window(datetime.date(2026, 10, 24)), ("20260924", "20261024"))


class LazyWideningTest(unittest.TestCase):
    """The capture queries +/-7 first, then +/-30, then +/-90 only with --wide."""

    def _ws(self, out, results):
        clock = FakeClock()
        runner = FakeRunner({"--version": VERSION_OK, "snapshots": results}, clock=clock)
        return tp.WaybackSalvage("/x/wayback_salvage.py", out_dir=out, runner=runner,
                                 clock=clock.time, sleep=clock.sleep), runner

    def _collect(self, ws, wide):
        async def go():
            return [c async for c in tp.archive_candidates(ws, "https://www.shop.example/", T, wide)]
        return run(go())

    def windows(self, runner):
        out = []
        for _, argv in runner.calls:
            out.append((argv[argv.index("--from") + 1], argv[argv.index("--to") + 1]))
        return out

    def test_stops_at_30_without_wide(self):
        with TempDir() as d:
            ws, runner = self._ws(d, [snapshots_result([])])
            self.assertEqual(self._collect(ws, wide=False), [])
            self.assertEqual(self.windows(runner), [("20251017", "20251031"), ("20250924", "20251123")])

    def test_goes_to_90_with_wide(self):
        with TempDir() as d:
            ws, runner = self._ws(d, [snapshots_result([])])
            self._collect(ws, wide=True)
            self.assertEqual(self.windows(runner)[-1], ("20250726", "20260122"))
            self.assertEqual(len(runner.calls), 3)

    def test_no_wider_query_when_7_has_enough(self):
        rows = [cdx_row("20251024000000"), cdx_row("20251023000000"), cdx_row("20251025000000")]
        with TempDir() as d:
            ws, runner = self._ws(d, [snapshots_result(rows)])

            async def first_three():
                out = []
                async for c in tp.archive_candidates(ws, "https://www.shop.example/", T, False):
                    out.append(c["timestamp"])
                    if len(out) == 3:
                        break
                return out

            self.assertEqual(run(first_three()), ["20251024000000", "20251023000000", "20251025000000"])
            self.assertEqual(len(runner.calls), 1)

    def test_widening_yields_only_new_rows(self):
        near = [cdx_row("20251022000000")]
        wider = near + [cdx_row("20251110000000")]
        with TempDir() as d:
            ws, runner = self._ws(d, [snapshots_result(near), snapshots_result(wider)])
            got = self._collect(ws, wide=False)
            self.assertEqual([c["timestamp"] for c in got], ["20251022000000", "20251110000000"])
            self.assertEqual([c["window_days"] for c in got], [7, 30])


if __name__ == "__main__":
    unittest.main()


class CandidateAttemptsTest(unittest.TestCase):
    """Suspect or degraded archive loads move on to the next candidate, up to
    3 loads in total, and every attempt is recorded in pack.json."""

    SNAPS = [cdx_row("20251024013522", digest="X"), cdx_row("20251023224510", digest="X"),
             cdx_row("20251025120000", digest="Y"), cdx_row("20251026120000", digest="Z"),
             cdx_row("20251021120000", digest="W")]

    def _capture(self, out, archive_results, snaps=None):
        import json
        import os
        from support import FakeRenderer, fake_fetch, fake_page
        clock = FakeClock()
        renderer = FakeRenderer(clock=clock, archive_results=archive_results)
        runner = FakeRunner({"--version": VERSION_OK, "snapshots": snapshots_result(snaps or self.SNAPS),
                             "fetch": fake_fetch, "page": fake_page}, clock=clock)
        code = tp.main(["capture", "--url", "https://www.shop.example/", "--brand", "Shop", "--type", "home",
                        "--label", "home", "--date", "2026-10-24", "--now-source", "live", "-o", out, "--quiet",
                        "--wayback-salvage", __file__],
                       deps={"renderer": renderer, "runner": runner, "clock": clock.time, "sleep": clock.sleep})
        with open(os.path.join(out, "shop", "home", "home", "2025-10-24", "pack.json"), encoding="utf-8") as fh:
            return code, renderer, json.load(fh)["sides"]["then"]

    @staticmethod
    def offline():
        from support import basic_facts
        return {"facts": basic_facts(title="Temporarily Offline"), "main_status": 200}

    def test_suspect_capture_tries_the_next_candidate(self):
        with TempDir() as out:
            code, renderer, then = self._capture(out, [self.offline(), {}])
            self.assertEqual(code, 0)
            self.assertEqual(then["status"], "done")
            self.assertEqual([a["outcome"] for a in then["attempts"]], ["suspect", "done"])
            self.assertEqual(then["attempts"][0]["signature"], "phrase-temporarily-offline")
            # the second-nearest capture shares the suspect one's digest, so it is skipped
            self.assertEqual(then["capture"]["timestamp"], "20251025120000")
            self.assertEqual(then["skipped_candidates"][0]["timestamp"], "20251023224510")
            archive_urls = [l[2] for l in renderer.loads if l[1] == "archive"]
            self.assertEqual(archive_urls, ["https://web.archive.org/web/20251024013522if_/https://www.shop.example/",
                                            "https://web.archive.org/web/20251025120000if_/https://www.shop.example/"])

    def test_at_most_three_attempts(self):
        with TempDir() as out:
            snaps = [cdx_row(ts) for ts in ("20251024013522", "20251023224510", "20251025120000", "20251026120000")]
            code, renderer, then = self._capture(out, [self.offline()] * 4, snaps)
            self.assertEqual(code, 0)
            self.assertEqual(len([l for l in renderer.loads if l[1] == "archive"]), 3)
            self.assertEqual(then["status"], "suspect")
            self.assertEqual(len(then["attempts"]), 3)

    def test_degraded_load_tries_the_next_candidate(self):
        with TempDir() as out:
            code, renderer, then = self._capture(out, [{"degraded": True}, {}])
            self.assertEqual([a["outcome"] for a in then["attempts"]], ["degraded", "done"])
            # a degraded load says nothing about the capture, so the same digest is not skipped
            self.assertEqual(then["capture"]["timestamp"], "20251023224510")

    def test_no_captures_fails_without_loads(self):
        with TempDir() as out:
            code, renderer, then = self._capture(out, [], snaps=[cdx_row("20250101000000")])
            self.assertEqual(then["status"], "failed")
            self.assertIn("no 200 text/html capture", then["reason"])
            self.assertEqual([l for l in renderer.loads if l[1] == "archive"], [])
