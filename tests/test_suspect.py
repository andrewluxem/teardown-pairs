"""Spec 3.12 case 5: every suspect signature in 3.6 matches its fixture, and
a normal page with the word "sale" does not."""

import glob
import json
import os
import unittest

from support import basic_facts, fixture_path, tp


def fixtures():
    out = {}
    for path in sorted(glob.glob(fixture_path("suspect", "*.json"))):
        with open(path, encoding="utf-8") as fh:
            out[os.path.basename(path)[:-5]] = json.load(fh)
    return out


class SuspectTest(unittest.TestCase):
    def test_every_signature_has_a_fixture(self):
        ids = [s[0] for s in tp.SUSPECT_SIGNATURES]
        self.assertEqual(len(ids), len(set(ids)))
        names = set(fixtures())
        for sid in ids:
            self.assertIn(sid, names, "no fixture for signature %s" % sid)

    def test_signature_list_covers_the_spec(self):
        ids = {s[0] for s in tp.SUSPECT_SIGNATURES}
        for status in (403, 429, 503):
            self.assertIn("status-%d" % status, ids)
        for phrase in ("Access Denied", "Just a moment", "Attention Required", "Pardon Our Interruption",
                       "Request unsuccessful", "Verify you are human", "Are you a robot", "Temporarily Offline"):
            self.assertIn("phrase-" + tp.slugify(phrase), ids)
        for marker in ("hcaptcha", "recaptcha", "px-captcha"):
            self.assertIn("captcha-" + marker, ids)
        self.assertIn("captcha-delivery", ids)
        self.assertIn("akamai-reference", ids)
        self.assertIn("thin-page", ids)

    def test_each_fixture_matches_its_signature(self):
        for name, fx in fixtures().items():
            with self.subTest(fixture=name):
                got = tp.detect_suspect(fx["signals"])
                if fx["expect"] is None:
                    self.assertIsNone(got)
                else:
                    self.assertIsNotNone(got)
                    self.assertEqual(got["signature"], fx["expect"])
                    self.assertTrue(got["detail"])

    def test_normal_sale_page_is_not_suspect(self):
        self.assertIsNone(tp.detect_suspect(fixtures()["normal-sale"]["signals"]))

    def test_phrase_match_is_case_insensitive_in_title(self):
        sig = dict(fixtures()["normal-sale"]["signals"], title="ACCESS DENIED")
        self.assertEqual(tp.detect_suspect(sig)["signature"], "phrase-access-denied")

    def test_signals_from_facts(self):
        facts = basic_facts(title="Just a moment...", signals={"text_chars": 120, "text_sample": "Checking"})
        facts["above_fold"]["image_count"] = 0
        sig = tp.suspect_signals(facts, 403)
        self.assertEqual(sig["status"], 403)
        self.assertEqual(sig["title"], "Just a moment...")
        self.assertEqual(sig["text_chars"], 120)
        self.assertEqual(sig["fold_images"], 0)
        self.assertEqual(tp.detect_suspect(sig)["signature"], "status-403")

    def test_missing_facts_are_suspect_free_but_status_still_counts(self):
        self.assertEqual(tp.detect_suspect(tp.suspect_signals(None, 503))["signature"], "status-503")
        self.assertIsNone(tp.detect_suspect(tp.suspect_signals(None, 200)))


if __name__ == "__main__":
    unittest.main()
