"""Spec 3.12 case 3: pages.csv parsing."""

import unittest

from support import tp

HEADER = "brand,page_type,label,now_url,then_url,notes\n"


class CsvTest(unittest.TestCase):
    def test_rows_and_then_url_default(self):
        rows = tp.parse_pages_csv(HEADER
                                  + "Target,home,home,https://www.target.com/,,\n"
                                  + "REI,category,mens-jackets,https://www.rei.com/c/mens-jackets,"
                                  + "https://www.rei.com/c/mens-jackets?old=1,moved in spring\n")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].then_url, "https://www.target.com/")
        self.assertEqual(rows[0].notes, "")
        self.assertEqual(rows[1].then_url, "https://www.rei.com/c/mens-jackets?old=1")
        self.assertEqual(rows[1].notes, "moved in spring")
        self.assertEqual(rows[1].page_type, "category")

    def test_optional_columns_may_be_absent(self):
        rows = tp.parse_pages_csv("brand,page_type,label,now_url\nREI,home,home,https://www.rei.com/\n")
        self.assertEqual(rows[0].then_url, "https://www.rei.com/")

    def test_missing_required_column(self):
        with self.assertRaises(tp.UsageError) as cm:
            tp.parse_pages_csv("brand,page_type,now_url\nREI,home,https://www.rei.com/\n")
        self.assertIn("label", str(cm.exception))

    def test_header_row_required(self):
        with self.assertRaises(tp.UsageError):
            tp.parse_pages_csv("Target,home,home,https://www.target.com/\n")

    def test_bad_page_type(self):
        with self.assertRaises(tp.UsageError) as cm:
            tp.parse_pages_csv(HEADER + "Target,pdp,shoe,https://www.target.com/p/1,,\n")
        msg = str(cm.exception)
        self.assertIn("page_type", msg)
        self.assertIn("line 2", msg)

    def test_duplicate_labels_per_brand_and_type(self):
        with self.assertRaises(tp.UsageError) as cm:
            tp.parse_pages_csv(HEADER
                               + "REI,category,Mens Jackets,https://www.rei.com/c/a,,\n"
                               + "REI,category,mens-jackets,https://www.rei.com/c/b,,\n")
        self.assertIn("duplicate", str(cm.exception).lower())

    def test_same_label_allowed_across_types_and_brands(self):
        rows = tp.parse_pages_csv(HEADER
                                  + "REI,category,jackets,https://www.rei.com/c/jackets,,\n"
                                  + "REI,product,jackets,https://www.rei.com/p/jackets,,\n"
                                  + "Patagonia,category,jackets,https://www.patagonia.com/c/jackets,,\n")
        self.assertEqual(len(rows), 3)

    def test_required_value_missing(self):
        with self.assertRaises(tp.UsageError) as cm:
            tp.parse_pages_csv(HEADER + "Target,home,home,,,\n")
        self.assertIn("now_url", str(cm.exception))

    def test_urls_must_be_http(self):
        with self.assertRaises(tp.UsageError):
            tp.parse_pages_csv(HEADER + "Target,home,home,www.target.com,,\n")
        with self.assertRaises(tp.UsageError):
            tp.parse_pages_csv(HEADER + "Target,home,home,https://www.target.com/,ftp://x/,\n")

    def test_comment_and_blank_rows_are_skipped(self):
        rows = tp.parse_pages_csv(HEADER
                                  + "\n"
                                  + "# Brand,category,label,https://example.com/c,,\n"
                                  + "Target,home,home,https://www.target.com/,,\n")
        self.assertEqual([r.brand for r in rows], ["Target"])

    def test_bom_and_whitespace(self):
        rows = tp.parse_pages_csv("﻿" + HEADER + " Target , Home , home , https://www.target.com/ ,,\n")
        self.assertEqual(rows[0].brand, "Target")
        self.assertEqual(rows[0].page_type, "home")
        self.assertEqual(rows[0].now_url, "https://www.target.com/")

    def test_all_errors_reported_together(self):
        with self.assertRaises(tp.UsageError) as cm:
            tp.parse_pages_csv(HEADER + "Target,pdp,a,https://t.example/,,\nREI,home,b,,,\n")
        self.assertIn("line 2", str(cm.exception))
        self.assertIn("line 3", str(cm.exception))

    def test_no_rows(self):
        with self.assertRaises(tp.UsageError):
            tp.parse_pages_csv(HEADER)

    def test_single_page_form(self):
        row = tp.row_from_args("https://www.rei.com/", "REI", "home", "home", None)
        self.assertEqual(row.then_url, "https://www.rei.com/")
        with self.assertRaises(tp.UsageError):
            tp.row_from_args("https://www.rei.com/", "REI", "landing", "home", None)


if __name__ == "__main__":
    unittest.main()
