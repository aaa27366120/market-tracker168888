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
