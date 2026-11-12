"""Regression tests for independent TAIFEX day/night freshness checks."""
import datetime as dt
import unittest
from unittest.mock import patch

import fetch_data


class TaifexFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 11, 13, 16, 10, tzinfo=fetch_data.TZ_TAIPEI)
        self.fresh_date = "2026-11-13"
        self.records = {
            "TX": [{
                "date": self.fresh_date, "m": "202612", "close": 44000,
                "high": 44100, "low": 43900, "vol": 1000, "session": "day",
            }],
            "TXALL": [{
                "date": self.fresh_date, "m": "202612", "close": 44100,
                "high": 44200, "low": 44000, "vol": 900, "session": "night",
            }],
        }

    def run_update(self, records):
        with patch.object(fetch_data, "tx_rows", return_value={"response": {}}), patch.object(
            fetch_data, "fut_records", return_value=records
        ):
            return fetch_data.update_futures({}, fetch=True, now=self.now)

    def test_both_sessions_fresh(self):
        _, items = self.run_update(self.records)
        by_symbol = {item["sym"]: item for item in items}
        self.assertFalse(by_symbol["TX"]["stale"])
        self.assertFalse(by_symbol["TXALL"]["stale"])

    def test_one_session_missing_does_not_inherit_other_success(self):
        _, items = self.run_update({"TX": self.records["TX"], "TXALL": []})
        by_symbol = {item["sym"]: item for item in items}
        self.assertFalse(by_symbol["TX"]["stale"])
        self.assertTrue(by_symbol["TXALL"]["stale"])
        self.assertEqual(by_symbol["TXALL"]["data_status"], "failed")
        self.assertIn("未包含此交易時段", by_symbol["TXALL"]["fetch_error"])

    def test_saturday_morning_uses_friday_day_session_and_monday_night_date(self):
        saturday_morning = dt.datetime(
            2026, 11, 14, 7, 10, tzinfo=fetch_data.TZ_TAIPEI
        )
        self.assertEqual(
            fetch_data.expected_futures_dates(saturday_morning),
            {"TX": "2026-11-13", "TXALL": "2026-11-16"},
        )


    def test_fetch_false_preserves_previous_failure_status(self):
        previous = {
            "TX": {
                "sym": "TX", "stale": True, "data_status": "failed",
                "fetch_error": "07:07 夜盤資料抓取失敗",
            }
        }
        hist = {"TX": self.records["TX"], "TXALL": self.records["TXALL"]}
        _, items = fetch_data.update_futures(
            hist, fetch=False, now=self.now, previous_items=previous
        )
        day = next(item for item in items if item["sym"] == "TX")
        self.assertTrue(day["stale"])
        self.assertEqual(day["data_status"], "failed")
        self.assertEqual(day["fetch_error"], "07:07 夜盤資料抓取失敗")

    def test_fetch_false_keeps_successful_previous_status(self):
        previous = {
            "TX": {"sym": "TX", "stale": False, "data_status": "success"}
        }
        hist = {"TX": self.records["TX"], "TXALL": self.records["TXALL"]}
        _, items = fetch_data.update_futures(
            hist, fetch=False, now=self.now, previous_items=previous
        )
        day = next(item for item in items if item["sym"] == "TX")
        self.assertFalse(day["stale"])
        self.assertEqual(day["data_status"], "success")

    def test_fetch_exception_preserves_last_successful_values_and_marks_failed(self):
        hist = {"TX": self.records["TX"], "TXALL": self.records["TXALL"]}
        with patch.object(fetch_data, "tx_rows", side_effect=RuntimeError("network down")):
            updated_hist, items = fetch_data.update_futures(
                hist, fetch=True, now=self.now
            )
        self.assertEqual(updated_hist["TX"][0]["close"], 44000)
        for item in items:
            self.assertTrue(item["stale"])
            self.assertEqual(item["data_status"], "failed")
            self.assertIn("network down", item["fetch_error"])

    def test_expected_date_is_not_available_between_morning_and_close_windows(self):
        for clock in ((9, 7), (12, 0), (15, 59)):
            moment = dt.datetime(
                2026, 11, 13, clock[0], clock[1], tzinfo=fetch_data.TZ_TAIPEI
            )
            self.assertIsNone(fetch_data.expected_futures_dates(moment))

    def test_newer_session_date_is_not_marked_stale(self):
        newer = {
            "TX": [{**self.records["TX"][0], "date": "2026-11-14"}],
            "TXALL": [{**self.records["TXALL"][0], "date": "2026-11-14"}],
        }
        _, items = self.run_update(newer)
        self.assertTrue(all(not item["stale"] for item in items))

    def test_old_session_date_is_marked_stale(self):
        old_night = [{
            **self.records["TXALL"][0],
            "date": "2026-11-12",
        }]
        _, items = self.run_update({"TX": self.records["TX"], "TXALL": old_night})
        by_symbol = {item["sym"]: item for item in items}
        self.assertFalse(by_symbol["TX"]["stale"])
        self.assertTrue(by_symbol["TXALL"]["stale"])
        self.assertIn("預期 2026-11-13", by_symbol["TXALL"]["fetch_error"])


if __name__ == "__main__":
    unittest.main()
