"""Spec 3.12 case 11: an injected clock proves the 20 s archive gap and the
6 s wayback-salvage gap (and the 30 s per-host live gap)."""

import unittest

from support import (FakeClock, FakeRenderer, FakeRunner, TempDir, VERSION_OK, cdx_row, fake_fetch, fake_page,
                     run, snapshots_result, tp)


class PacerTest(unittest.TestCase):
    def test_start_to_start_gap(self):
        clock = FakeClock()
        p = tp.Pacer(20, clock.time, clock.sleep)
        run(p.wait())
        self.assertEqual(clock.sleeps, [])
        run(p.wait())
        self.assertEqual(clock.sleeps, [20])
        clock.advance(5)
        run(p.wait())
        self.assertEqual(clock.sleeps, [20, 15])
        clock.advance(25)
        run(p.wait())
        self.assertEqual(clock.sleeps, [20, 15])

    def test_host_pacer_is_per_host(self):
        clock = FakeClock()
        p = tp.HostPacer(30, clock.time, clock.sleep)
        run(p.wait("a.example"))
        run(p.wait("b.example"))
        self.assertEqual(clock.sleeps, [])
        run(p.wait("a.example"))
        self.assertEqual(clock.sleeps, [30])


class WaybackGapTest(unittest.TestCase):
    def test_calls_at_least_6_s_apart_start_to_start(self):
        clock = FakeClock()
        runner = FakeRunner({"snapshots": snapshots_result([])}, clock=clock)
        with TempDir() as d:
            ws = tp.WaybackSalvage("/x/wayback_salvage.py", out_dir=d, runner=runner, clock=clock.time,
                                   sleep=clock.sleep)

            async def three():
                for _ in range(3):
                    await ws.snapshots("https://www.shop.example/", "20251017", "20251031")

            run(three())
        starts = [t for t, _ in runner.calls]
        self.assertEqual([b - a for a, b in zip(starts, starts[1:])], [6, 6])

    def test_slow_call_needs_no_extra_wait(self):
        clock = FakeClock()

        def slow(argv):
            clock.advance(10)
            return snapshots_result([])

        runner = FakeRunner({"snapshots": slow}, clock=clock)
        with TempDir() as d:
            ws = tp.WaybackSalvage("/x/wayback_salvage.py", out_dir=d, runner=runner, clock=clock.time,
                                   sleep=clock.sleep)

            async def two():
                await ws.snapshots("https://www.shop.example/", "1", "2")
                await ws.snapshots("https://www.shop.example/", "1", "2")

            run(two())
        self.assertEqual(clock.sleeps, [])


class CapturePacingTest(unittest.TestCase):
    def _capture(self, rows_csv, extra=()):
        clock = FakeClock()
        renderer = FakeRenderer(clock=clock)
        runner = FakeRunner({"--version": VERSION_OK, "snapshots": snapshots_result([cdx_row("20251024013522")]),
                             "fetch": fake_fetch, "page": fake_page}, clock=clock)
        with TempDir() as out:
            import os
            csv_path = os.path.join(out, "pages.csv")
            with open(csv_path, "w", encoding="utf-8") as fh:
                fh.write("brand,page_type,label,now_url\n" + rows_csv)
            code = tp.main(["capture", csv_path, "--date", "2026-10-24", "-o", os.path.join(out, "o"), "--quiet",
                            "--wayback-salvage", __file__] + list(extra),
                           deps={"renderer": renderer, "runner": runner, "clock": clock.time, "sleep": clock.sleep})
        self.assertEqual(code, 0)
        return renderer, runner

    def test_archive_loads_20_s_apart_and_ws_calls_6_s_apart(self):
        renderer, runner = self._capture("A,home,home,https://a.example/\nB,home,home,https://b.example/\n",
                                         ["--now-source", "archive"])
        starts = [t for t, kind, _, _ in renderer.loads if kind == "archive"]
        self.assertEqual(len(starts), 4)
        for a, b in zip(starts, starts[1:]):
            self.assertGreaterEqual(b - a, 20)
        ws_starts = [t for t, argv in runner.calls if argv[0] != "--version"]
        self.assertGreater(len(ws_starts), 4)
        for a, b in zip(ws_starts, ws_starts[1:]):
            self.assertGreaterEqual(b - a, 6)

    def test_delay_option_sets_the_archive_gap(self):
        renderer, _ = self._capture("A,home,home,https://a.example/\n", ["--now-source", "archive", "--delay", "45"])
        starts = [t for t, kind, _, _ in renderer.loads if kind == "archive"]
        self.assertGreaterEqual(starts[1] - starts[0], 45)

    def test_live_loads_30_s_apart_per_host(self):
        renderer, _ = self._capture("A,home,home,https://a.example/\nA,category,c,https://a.example/c\n"
                                    "B,home,home,https://b.example/\n", ["--now-source", "live"])
        live = [(t, url) for t, kind, url, _ in renderer.loads if kind == "live"]
        self.assertEqual(len(live), 6)
        by_host = {}
        for t, url in live:
            by_host.setdefault(url.split("/")[2], []).append(t)
        for host, times in by_host.items():
            for a, b in zip(times, times[1:]):
                self.assertGreaterEqual(b - a, 30, host)


if __name__ == "__main__":
    unittest.main()
