"""Spec 3.12 case 8: diff logic on three fixture fact pairs (home, PLP, PDP)."""

import copy
import json
import os
import unittest

from support import load_json, quiet_main, tp  # noqa: E402
from support import (FakeClock, FakeRenderer, FakeRunner, TempDir, VERSION_OK, cdx_row, fake_fetch, fake_page,
                     snapshots_result)


def pair(name):
    return load_json("facts", name + "_then.json"), load_json("facts", name + "_now.json")


class HomeDiffTest(unittest.TestCase):
    def setUp(self):
        self.d = tp.diff_facts(*pair("home"))

    def test_scalars(self):
        title = self.d["scalars"]["title"]
        self.assertEqual(title, {"then": "Shop Example | Fall Sale", "now": "Shop Example | Holiday Gifts",
                                 "changed": True})
        self.assertFalse(self.d["scalars"]["description"]["changed"])
        self.assertFalse(self.d["scalars"]["og_image"]["changed"])
        self.assertEqual(set(self.d["scalars"]), {"title", "description", "canonical", "robots", "lang",
                                                  "og_title", "og_image"})

    def test_list_sets(self):
        nav = self.d["lists"]["nav"]
        self.assertEqual(nav["added"], ["Gifts"])
        self.assertEqual(nav["removed"], ["Clearance"])
        self.assertEqual(nav["kept"], ["Women", "Men", "Kids"])
        self.assertFalse(nav["order_changed"])
        ctas = self.d["lists"]["ctas"]
        self.assertEqual(ctas["added"], ["Shop Gifts"])
        self.assertEqual(ctas["removed"], ["Shop Men"])
        self.assertEqual(self.d["lists"]["promos"]["removed"], ["Free shipping over $50", "Up to 40% off outerwear"])
        self.assertEqual(self.d["lists"]["above_fold_text"]["added"], ["Free shipping on everything", "Holiday Gifts"])
        self.assertEqual(self.d["lists"]["h1"]["added"], ["Holiday Gifts"])

    def test_order_change_with_same_set(self):
        h2 = self.d["lists"]["h2"]
        self.assertEqual(h2["added"], [])
        self.assertEqual(h2["removed"], [])
        self.assertTrue(h2["order_changed"])

    def test_list_names(self):
        self.assertEqual(set(self.d["lists"]), {"h1", "h2", "nav", "ctas", "above_fold_text",
                                                "above_fold_buttons", "promos"})

    def test_martech_added_and_removed_by_vendor(self):
        m = self.d["martech"]
        self.assertEqual(m["added"], {"reviews and UGC": ["apps.bazaarvoice.com"], "consent": ["cdn.cookielaw.org"]})
        self.assertEqual(m["removed"], {"testing and personalisation": ["cdn.optimizely.com"]})
        self.assertEqual(m["kept"], ["www.googletagmanager.com"])
        self.assertEqual(m["mentioned_only"], {"then": ["www.facebook.com"], "now": ["www.instagram.com"]})

    def test_counts_with_deltas(self):
        c = self.d["counts"]
        self.assertEqual(c["images"], {"then": 42, "now": 35, "delta": -7})
        self.assertEqual(c["iframes"], {"then": 1, "now": 3, "delta": 2})
        self.assertEqual(c["links"]["delta"], 20)

    def test_list_matching_ignores_case_and_spacing(self):
        then, now = pair("home")
        now = copy.deepcopy(now)
        now["ctas"] = ["shop   women", "Shop Gifts", "SIGN UP"]
        ctas = tp.diff_facts(then, now)["lists"]["ctas"]
        self.assertEqual(ctas["kept"], ["Shop Women", "Sign Up"])
        self.assertEqual(ctas["removed"], ["Shop Men"])


class PlpDiffTest(unittest.TestCase):
    def setUp(self):
        self.d = tp.diff_facts(*pair("plp"))

    def test_item_list_and_breadcrumbs(self):
        sd = self.d["structured_data"]
        self.assertEqual(sd["types_added"], [])
        self.assertEqual(sd["types_removed"], [])
        self.assertEqual(sd["item_list_count"], {"then": 48, "now": 60, "changed": True})
        self.assertEqual(sd["breadcrumbs"]["then"], ["Home", "Men", "Jackets"])
        self.assertTrue(sd["breadcrumbs"]["changed"])

    def test_plp_heuristics(self):
        h = self.d["heuristics"]
        self.assertEqual(h["label"], "heuristic")
        self.assertEqual(h["plp"]["price_like_in_main"], {"then": 48, "now": 60, "delta": 12})
        self.assertEqual(h["plp"]["filter_elements"], {"then": 6, "now": 9, "delta": 3})
        self.assertEqual(h["plp"]["filter_labels"]["added"], ["Brand"])

    def test_h2_added(self):
        self.assertEqual(self.d["lists"]["h2"]["added"], ["Fleece"])
        self.assertFalse(self.d["lists"]["h2"]["order_changed"])


class PdpDiffTest(unittest.TestCase):
    def setUp(self):
        self.d = tp.diff_facts(*pair("pdp"))

    def test_product_price_availability_rating(self):
        p = self.d["structured_data"]["product"]
        self.assertEqual(p["price"], {"then": "129.00", "now": "99.00", "changed": True})
        self.assertEqual(p["availability"], {"then": "InStock", "now": "OutOfStock", "changed": True})
        self.assertEqual(p["rating"], {"then": "4.5", "now": "4.6", "changed": True})
        self.assertEqual(p["review_count"]["now"], "388")
        self.assertFalse(p["currency"]["changed"])

    def test_types(self):
        sd = self.d["structured_data"]
        self.assertEqual(sd["types_removed"], ["BreadcrumbList"])
        self.assertEqual(sd["types_added"], [])

    def test_pdp_heuristics(self):
        h = self.d["heuristics"]["pdp"]
        self.assertEqual(h["first_price_after_h1"], {"then": "$129.00", "now": "$99.00", "changed": True})
        self.assertEqual(h["add_to_cart"]["then"], {"present": True, "text": "Add to Bag"})
        self.assertTrue(h["add_to_cart"]["changed"])

    def test_schema_org_prefixes_do_not_count_as_change(self):
        then, now = pair("pdp")
        then = copy.deepcopy(then)
        now = copy.deepcopy(then)
        then["structured_data"]["product"]["availability"] = "http://schema.org/InStock"
        now["structured_data"]["product"]["availability"] = "https://schema.org/InStock"
        p = tp.diff_facts(then, now)["structured_data"]["product"]
        self.assertFalse(p["availability"]["changed"])
        self.assertEqual(p["availability"]["now"], "InStock")

    def test_product_missing_on_one_side(self):
        then, now = pair("pdp")
        now = copy.deepcopy(now)
        now["structured_data"]["product"] = None
        p = tp.diff_facts(then, now)["structured_data"]["product"]
        self.assertEqual(p["price"], {"then": "129.00", "now": None, "changed": True})


def side(kind, date, distance=None, failed=0, leaked=0, suspect=None, blocked=False, truncated=False):
    then, _ = pair("home")
    return {
        "facts": then,
        "capture": {
            "kind": kind,
            "mobile_basis": "desktop-capture-resized" if kind == "archive" else "mobile-device-profile",
            "capture": {"date": date, "distance_days": distance, "timestamp": date.replace("-", "") + "120000"},
            "requests": {"failed": failed, "leaked": leaked, "requests": 100},
            "flags": {"suspect": suspect, "blocked": blocked, "degraded": False, "truncated": truncated},
            "screenshots": {"desktop": "desktop.png", "desktop-fold": "desktop-fold.png",
                            "mobile": "mobile.png", "mobile-fold": "mobile-fold.png"},
        },
    }


PACK = {
    "row": {"brand": "Shop", "page_type": "home", "label": "home", "now_url": "https://www.shop.example/",
            "then_url": "https://www.shop.example/", "notes": ""},
    "target_date": "2025-10-24", "date": "2026-10-24", "years_back": 1, "kind": "pair",
}


class BuildDiffTest(unittest.TestCase):
    def test_header_and_asymmetries_live(self):
        sides = {"then": side("archive", "2025-10-23", 1, failed=14, leaked=3), "now-live": side("live", "2026-10-24")}
        diff = tp.build_diff(PACK, sides)
        self.assertEqual([c["name"] for c in diff["comparisons"]], ["then-vs-now-live"])
        c = diff["comparisons"][0]
        h = c["header"]
        self.assertEqual(h["brand"], "Shop")
        self.assertEqual(h["target_date"], "2025-10-24")
        self.assertEqual(h["then_date"], "2025-10-23")
        self.assertEqual(h["distance_days"], 1)
        self.assertEqual(h["now_source"], "live")
        self.assertEqual(h["mobile_basis"], {"then": "desktop-capture-resized", "now": "mobile-device-profile"})
        self.assertEqual(h["requests"]["then"], {"failed": 14, "leaked": 3})
        self.assertIn("Then is the archive crawler's desktop view resized to mobile width; "
                      "now is a live mobile device profile.", c["asymmetries"])
        self.assertTrue(any("logged out" in s for s in c["asymmetries"]))
        self.assertEqual(c["warnings"], [])
        self.assertEqual(c["screenshots"]["then"]["desktop"], "then/desktop.png")
        self.assertEqual(c["screenshots"]["now"]["mobile-fold"], "now-live/mobile-fold.png")

    def test_distance_warning_over_7_days(self):
        sides = {"then": side("archive", "2025-11-05", 12), "now-archive": side("archive", "2026-10-20", 4)}
        c = tp.build_diff(PACK, sides)["comparisons"][0]
        self.assertEqual(c["name"], "then-vs-now-archive")
        self.assertTrue(any("12 days" in w for w in c["warnings"]))
        sides["then"] = side("archive", "2025-10-31", 7)
        c = tp.build_diff(PACK, sides)["comparisons"][0]
        self.assertFalse(any("days from the target" in w for w in c["warnings"]))

    def test_both_now_sources_give_two_comparisons(self):
        sides = {"then": side("archive", "2025-10-24", 0), "now-live": side("live", "2026-10-24"),
                 "now-archive": side("archive", "2026-10-22", 2)}
        names = [c["name"] for c in tp.build_diff(PACK, sides)["comparisons"]]
        self.assertEqual(names, ["then-vs-now-live", "then-vs-now-archive"])

    def test_blocked_live_is_stated(self):
        sides = {"then": side("archive", "2025-10-24", 0),
                 "now-live": side("live", "2026-10-24", blocked=True, suspect={"signature": "phrase-access-denied",
                                                                               "detail": "title"})}
        c = tp.build_diff(PACK, sides)["comparisons"][0]
        self.assertTrue(c["header"]["flags"]["now"]["blocked"])
        self.assertTrue(any("blocked" in w.lower() for w in c["warnings"]))

    def test_high_failed_counts_are_called_out(self):
        sides = {"then": side("archive", "2025-10-24", 0, failed=60), "now-live": side("live", "2026-10-24")}
        c = tp.build_diff(PACK, sides)["comparisons"][0]
        self.assertTrue(any("missing archive assets" in s for s in c["asymmetries"] + c["warnings"]))

    def test_no_then_no_comparisons(self):
        self.assertEqual(tp.build_diff(PACK, {"now-live": side("live", "2026-10-24")})["comparisons"], [])


class StaleNowArchiveTest(unittest.TestCase):
    """A now-archive capture more than 7 days before --date is warned about
    the way a then capture far from its target is, with both dates and the
    gap. Live run: REI men's jackets 20260924 (30 days before 2026-10-24),
    Patagonia Better Sweater 20261007 (17 days)."""

    PACK = dict(PACK, sides={"then": {"status": "done"}, "now-live": {"status": "done"},
                             "now-archive": {"status": "done"}})

    def comparisons(self, now_date):
        sides = {"then": side("archive", "2025-10-24", 0), "now-live": side("live", "2026-10-24"),
                 "now-archive": side("archive", now_date)}
        diff = tp.build_diff(self.PACK, sides)
        return diff, sides, {c["name"]: c for c in diff["comparisons"]}

    def stale(self, comparison):
        return [w for w in comparison["warnings"] if w.startswith("Now-archive capture")]

    def test_live_run_examples(self):
        for now_date, gap in (("2026-09-24", 30), ("2026-10-07", 17)):
            with self.subTest(now_date=now_date):
                _, _, comps = self.comparisons(now_date)
                c = comps["then-vs-now-archive"]
                self.assertEqual(self.stale(c), [
                    "Now-archive capture is %d days before --date (%s vs 2026-10-24); retail merchandising changes "
                    "weekly and seasonally, so it may not be the page as it is now." % (gap, now_date)])
                self.assertEqual(c["header"]["now_distance_days"], gap)
                self.assertEqual(self.stale(comps["then-vs-now-live"]), [])
                self.assertIsNone(comps["then-vs-now-live"]["header"]["now_distance_days"])

    def test_seven_days_is_not_stale_eight_is(self):
        _, _, comps = self.comparisons("2026-10-17")
        self.assertEqual(self.stale(comps["then-vs-now-archive"]), [])
        self.assertEqual(comps["then-vs-now-archive"]["header"]["now_distance_days"], 7)
        _, _, comps = self.comparisons("2026-10-16")
        self.assertEqual(len(self.stale(comps["then-vs-now-archive"])), 1)
        self.assertIn("8 days before --date", self.stale(comps["then-vs-now-archive"])[0])

    def test_same_day_capture(self):
        _, _, comps = self.comparisons("2026-10-24")
        self.assertEqual(self.stale(comps["then-vs-now-archive"]), [])
        self.assertEqual(comps["then-vs-now-archive"]["header"]["now_distance_days"], 0)

    def test_diff_md_and_compare_html_show_it(self):
        diff, sides, _ = self.comparisons("2026-09-24")
        md = tp.render_diff_md(diff)
        self.assertIn("- Now-archive capture is 30 days before --date (2026-09-24 vs 2026-10-24)", md)
        self.assertIn("Now-archive capture 2026-09-24: 30 days before --date 2026-10-24.", md)
        page = tp.render_compare_html("/nonexistent-pack", self.PACK, diff, sides)
        self.assertIn('<ul class="notes warn"><li>Now-archive capture is 30 days before --date (2026-09-24 vs '
                      '2026-10-24)', page)
        self.assertEqual(page.count("Now-archive capture is 30 days"), 1)


class StaleNowArchiveCommandTest(unittest.TestCase):
    """capture writes the warning; diff, offline, adds it to a pack whose
    diff.md and compare.html were written without it."""

    def test_capture_then_offline_diff(self):
        def snaps(argv):
            frm = argv[argv.index("--from") + 1]
            ts = "20260924040234" if frm.startswith("2026") else "20251026015232"
            return snapshots_result([cdx_row(ts)])

        with TempDir() as out:
            clock = FakeClock()
            renderer = FakeRenderer(clock=clock)
            runner = FakeRunner({"--version": VERSION_OK, "snapshots": snaps, "fetch": fake_fetch, "page": fake_page},
                                clock=clock)
            code = quiet_main(["capture", "--url", "https://www.shop.example/", "--brand", "Shop", "--type", "home",
                               "--label", "home", "--date", "2026-10-24", "--now-source", "archive", "-o", out,
                               "--quiet", "--wayback-salvage", __file__],
                              deps={"renderer": renderer, "runner": runner, "clock": clock.time, "sleep": clock.sleep})
            self.assertEqual(code, 0)
            pack = os.path.join(out, "shop", "home", "home", "2025-10-24")
            warning = "Now-archive capture is 30 days before --date (2026-09-24 vs 2026-10-24)"
            for name in ("diff.md", "compare.html"):
                with open(os.path.join(pack, name), encoding="utf-8") as fh:
                    self.assertIn(warning, fh.read())
            # a pack from 0.1.0: same files, no warning yet
            for name in ("diff.md", "compare.html", "diff.json"):
                with open(os.path.join(pack, name), "w", encoding="utf-8") as fh:
                    fh.write("old\n")
            loads = len(renderer.loads)
            code = quiet_main(["diff", out, "--json"], deps={"renderer": FakeRenderer(clock=clock), "runner": runner})
            self.assertEqual(code, 0)
            self.assertEqual(len(renderer.loads), loads)
            for name in ("diff.md", "compare.html"):
                with open(os.path.join(pack, name), encoding="utf-8") as fh:
                    self.assertIn(warning, fh.read())
            with open(os.path.join(pack, "diff.json"), encoding="utf-8") as fh:
                c = json.load(fh)["comparisons"][0]
            self.assertEqual(c["header"]["now_distance_days"], 30)


class MarkdownTest(unittest.TestCase):
    def test_markdown(self):
        then, now = pair("pdp")
        sides = {"then": side("archive", "2025-10-23", 9, failed=5), "now-live": side("live", "2026-10-24")}
        sides["then"]["facts"], sides["now-live"]["facts"] = then, now
        md = tp.render_diff_md(tp.build_diff(PACK, sides))
        self.assertNotIn("\u2014", md)
        self.assertTrue(md.startswith("# "))
        for needle in ("then-vs-now-live", "Asymmetries", "Title", "Martech", "Counts", "129.00", "99.00",
                       "then/desktop.png", "now-live/mobile-fold.png", "9 days"):
            self.assertIn(needle, md)


if __name__ == "__main__":
    unittest.main()
