"""Spec 3.12 case 7: vendor-map grouping of script hosts."""

import unittest

from support import tp


class VendorMapTest(unittest.TestCase):
    def test_categories_are_the_spec_list(self):
        self.assertEqual(list(tp.VENDOR_CATEGORIES), [
            "analytics", "tag management", "testing and personalisation", "reviews and UGC",
            "chat", "ads and affiliates", "consent", "other",
        ])

    def test_known_hosts(self):
        cases = {
            "www.google-analytics.com": "analytics",
            "www.googletagmanager.com": "tag management",
            "tags.tiqcdn.com": "tag management",
            "assets.adobedtm.com": "tag management",
            "cdn.optimizely.com": "testing and personalisation",
            "cdn.dynamicyield.com": "testing and personalisation",
            "apps.bazaarvoice.com": "reviews and UGC",
            "widget.intercom.io": "chat",
            "connect.facebook.net": "ads and affiliates",
            "securepubads.g.doubleclick.net": "ads and affiliates",
            "cdn.cookielaw.org": "consent",
            "consent.cookiebot.com": "consent",
            "unknown-vendor.example": "other",
        }
        for host, cat in cases.items():
            with self.subTest(host=host):
                self.assertEqual(tp.vendor_category(host), cat)

    def test_longest_suffix_wins(self):
        self.assertEqual(tp.vendor_category("shop.sc.omtrdc.net"), "analytics")
        self.assertEqual(tp.vendor_category("shop.tt.omtrdc.net"), "testing and personalisation")

    def test_suffix_matches_whole_labels_only(self):
        self.assertEqual(tp.vendor_category("notgoogletagmanager.com"), "other")
        self.assertEqual(tp.vendor_category("GoogleTagManager.com."), "tag management")

    def test_group_hosts(self):
        grouped = tp.group_hosts(["z.example", "www.googletagmanager.com", "cdn.cookielaw.org",
                                  "www.google-analytics.com", "a.example", "cdn.cookielaw.org"])
        self.assertEqual(grouped, {
            "analytics": ["www.google-analytics.com"],
            "tag management": ["www.googletagmanager.com"],
            "consent": ["cdn.cookielaw.org"],
            "other": ["a.example", "z.example"],
        })
        self.assertEqual(list(grouped), ["analytics", "tag management", "consent", "other"])

    def test_group_hosts_empty(self):
        self.assertEqual(tp.group_hosts([]), {})


class ScriptHostsTest(unittest.TestCase):
    """Now-live martech is parsed from the live HTML with wayback-salvage's rule."""

    HTML = """<html><head>
    <script src="https://www.googletagmanager.com/gtm.js?id=GTM-1"></script>
    <script src="//cdn.cookielaw.org/otSDKStub.js"></script>
    <script src="/static/app.js"></script>
    <script src="https://img.shop.example/x.js"></script>
    <script type="application/ld+json">{"sameAs": "https://www.instagram.com/shop"}</script>
    <script type="text/template"><script src="https://tmpl.example/t.js"></script></script>
    <script>var u = "https:\\/\\/www.facebook.com\\/shop"; var g = "https://www.googletagmanager.com/x";</script>
    </head><body></body></html>"""

    def test_loaded_and_mentioned(self):
        loaded, mentioned = tp.script_hosts_from_html(self.HTML, "https://www.shop.example/")
        self.assertEqual(loaded, [["cdn.cookielaw.org", 1], ["www.googletagmanager.com", 1]])
        self.assertEqual(mentioned, [["www.facebook.com", 1]])

    def test_matches_wayback_salvage_page_fixture(self):
        import json
        from support import ws_record
        page = json.loads(ws_record("page")["stdout"])
        from record_ws_fixtures import PAGE_HTML
        loaded, mentioned = tp.script_hosts_from_html(PAGE_HTML, page["base_url"])
        self.assertEqual(loaded, page["script_src_host"])
        self.assertEqual(mentioned, page["script_text_host"])


if __name__ == "__main__":
    unittest.main()
