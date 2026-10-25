"""Spec 3.12 case 1: target date math, including 29 Feb."""

import datetime
import unittest

from support import tp


class TargetDateTest(unittest.TestCase):
    def test_same_month_and_day_one_year_back(self):
        self.assertEqual(tp.target_date(datetime.date(2026, 10, 24), 1), datetime.date(2025, 10, 24))

    def test_several_years_back(self):
        self.assertEqual(tp.target_date(datetime.date(2026, 10, 24), 3), datetime.date(2023, 10, 24))

    def test_leap_day_maps_to_28_feb(self):
        self.assertEqual(tp.target_date(datetime.date(2028, 2, 29), 1), datetime.date(2027, 2, 28))
        self.assertEqual(tp.target_date(datetime.date(2028, 2, 29), 3), datetime.date(2025, 2, 28))

    def test_leap_day_to_leap_year_stays(self):
        self.assertEqual(tp.target_date(datetime.date(2028, 2, 29), 4), datetime.date(2024, 2, 29))

    def test_28_feb_stays_28_feb(self):
        self.assertEqual(tp.target_date(datetime.date(2025, 2, 28), 1), datetime.date(2024, 2, 28))


class YearsBackTest(unittest.TestCase):
    def test_default_and_lists(self):
        self.assertEqual(tp.parse_years_back("1"), [1])
        self.assertEqual(tp.parse_years_back("1,2"), [1, 2])
        self.assertEqual(tp.parse_years_back(" 2, 1 ,2"), [2, 1])

    def test_rejects_bad_values(self):
        for bad in ("", "0", "-1", "a", "1,,2", "1.5"):
            with self.subTest(bad=bad):
                with self.assertRaises(tp.UsageError):
                    tp.parse_years_back(bad)


class DateArgTest(unittest.TestCase):
    def test_parse_date(self):
        self.assertEqual(tp.parse_date("2026-10-24"), datetime.date(2026, 10, 24))

    def test_parse_date_rejects_other_formats(self):
        for bad in ("2026/10/24", "24-10-2026", "2026-02-30", "today"):
            with self.subTest(bad=bad):
                with self.assertRaises(tp.UsageError):
                    tp.parse_date(bad)

    def test_default_date_is_today_in_denver(self):
        # 03:00 UTC on 25 Oct is 21:00 on 24 Oct in Denver (MDT, UTC-6).
        now = datetime.datetime(2026, 10, 25, 3, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(tp.today_denver(now), datetime.date(2026, 10, 24))

    def test_weekday_names(self):
        self.assertEqual(tp.weekday(datetime.date(2025, 10, 24)), "Fri")
        self.assertEqual(tp.weekday(datetime.date(2025, 10, 26)), "Sun")


if __name__ == "__main__":
    unittest.main()
