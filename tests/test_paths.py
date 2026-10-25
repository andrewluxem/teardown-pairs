"""Spec 3.12 case 4: slugging and path safety."""

import datetime
import os
import unittest

from support import TempDir, tp


class SlugTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(tp.slugify("Target"), "target")
        self.assertEqual(tp.slugify("Patagonia Home!"), "patagonia-home")
        self.assertEqual(tp.slugify("  Men's   Jackets / Sale  "), "men-s-jackets-sale")

    def test_unicode_folds_to_ascii(self):
        self.assertEqual(tp.slugify("Ünïcödé Brånd"), "unicode-brand")

    def test_traversal_is_neutralised(self):
        self.assertEqual(tp.slugify("../../etc/passwd"), "etc-passwd")
        self.assertEqual(tp.slugify(".."), tp.slugify(".."))
        self.assertNotIn("/", tp.slugify("a/b\\c"))
        self.assertNotIn("..", tp.slugify(".."))

    def test_capped_at_80(self):
        s = tp.slugify("x" * 50 + " " + "y" * 50)
        self.assertLessEqual(len(s), 80)
        self.assertFalse(s.endswith("-"))

    def test_empty_gets_a_stable_fallback(self):
        a = tp.slugify("!!!")
        self.assertTrue(a)
        self.assertEqual(a, tp.slugify("!!!"))
        self.assertNotEqual(a, tp.slugify("???"))
        self.assertRegex(a, r"^[a-z0-9-]+$")


class PathSafetyTest(unittest.TestCase):
    def test_pack_dir_layout(self):
        with TempDir() as out:
            row = tp.Row("Target", "home", "Home Page", "https://www.target.com/", "https://www.target.com/", "")
            path = tp.pack_dir(out, row, datetime.date(2025, 10, 24))
            self.assertEqual(path, os.path.join(os.path.realpath(out), "target", "home", "home-page", "2025-10-24"))

    def test_safe_path_rejects_escapes(self):
        with TempDir() as out:
            for segs in (("..", "x"), ("/etc",), ("a", "..", "..", "b")):
                with self.subTest(segs=segs):
                    with self.assertRaises(ValueError):
                        tp.safe_path(out, *segs)

    def test_safe_path_rejects_symlink_escape(self):
        with TempDir() as out, TempDir() as elsewhere:
            os.symlink(elsewhere, os.path.join(out, "link"))
            with self.assertRaises(ValueError):
                tp.safe_path(out, "link", "x")

    def test_safe_path_accepts_inside(self):
        with TempDir() as out:
            self.assertEqual(tp.safe_path(out, "a", "b"), os.path.join(os.path.realpath(out), "a", "b"))


if __name__ == "__main__":
    unittest.main()
