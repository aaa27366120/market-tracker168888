import datetime as dt
import unittest

import fetch_data


class FuturesDateTests(unittest.TestCase):
    def test_friday_morning_night_session_is_thursday_opened_session(self):
        now = dt.datetime(2026, 11, 13, 9, 7, tzinfo=fetch_data.TZ_TAIPEI)
        self.assertEqual(
            fetch_data.expected_futures_dates(now),
            {"TX": "2026-11-12", "TXALL": "2026-11-12"},
        )

    def test_monday_morning_night_session_uses_monday_trading_date(self):
        now = dt.datetime(2026, 11, 16, 8, 7, tzinfo=fetch_data.TZ_TAIPEI)
        self.assertEqual(
            fetch_data.expected_futures_dates(now),
            {"TX": "2026-11-13", "TXALL": "2026-11-16"},
        )

    def test_after_close_uses_current_weekday_for_both_sessions(self):
        now = dt.datetime(2026, 11, 13, 16, 7, tzinfo=fetch_data.TZ_TAIPEI)
        self.assertEqual(
            fetch_data.expected_futures_dates(now),
            {"TX": "2026-11-13", "TXALL": "2026-11-13"},
        )


class TwseCsvParserTests(unittest.TestCase):
    def test_accepts_bom_quoted_equals_and_commas(self):
        raw = '\ufeff時間,"發行量加權股價指數"\n="09:00","27,123.45"\n="09:05","27,130.50"\n'
        self.assertEqual(
            fetch_data.parse_twse_intraday_csv(raw, "2026-11-13"),
            {"date": "2026-11-13", "time": "09:05", "price": 27130.50},
        )

    def test_rejects_unrecognized_csv_with_diagnostic_preview(self):
        with self.assertRaisesRegex(ValueError, "回應開頭"):
            fetch_data.parse_twse_intraday_csv("錯誤,資料\n1,2\n", "2026-11-13")


if __name__ == "__main__":
    unittest.main()
