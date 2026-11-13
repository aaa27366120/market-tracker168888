import datetime as dt
import unittest
from unittest import mock

import fetch_data


class FuturesDateTests(unittest.TestCase):
    def test_friday_morning_uses_thursday_for_both_sessions(self):
        now = dt.datetime(2026, 11, 13, 8, 7, tzinfo=fetch_data.TZ_TAIPEI)
        self.assertEqual(
            fetch_data.expected_futures_dates(now),
            {"TX": "2026-11-12", "TXALL": "2026-11-12"},
        )

    def test_monday_morning_uses_previous_friday_for_both_sessions(self):
        now = dt.datetime(2026, 11, 16, 8, 7, tzinfo=fetch_data.TZ_TAIPEI)
        self.assertEqual(
            fetch_data.expected_futures_dates(now),
            {"TX": "2026-11-13", "TXALL": "2026-11-13"},
        )

    def test_tuesday_morning_uses_previous_monday_for_both_sessions(self):
        now = dt.datetime(2026, 11, 17, 8, 7, tzinfo=fetch_data.TZ_TAIPEI)
        self.assertEqual(
            fetch_data.expected_futures_dates(now),
            {"TX": "2026-11-16", "TXALL": "2026-11-16"},
        )

    def test_saturday_morning_uses_friday_for_both_sessions(self):
        now = dt.datetime(2026, 11, 14, 7, 10, tzinfo=fetch_data.TZ_TAIPEI)
        self.assertEqual(
            fetch_data.expected_futures_dates(now),
            {"TX": "2026-11-13", "TXALL": "2026-11-13"},
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

    def test_intraday_retries_empty_http_200_then_parses_csv(self):
        class Response:
            status_code = 200
            headers = {"Content-Type": "text/csv"}
            def __init__(self, text):
                self.text = text
                self.content = text.encode("utf-8")
            def raise_for_status(self):
                return None

        now = dt.datetime(2026, 11, 13, 10, 7, tzinfo=fetch_data.TZ_TAIPEI)
        good_csv = '時間,"發行量加權股價指數"\n09:05,"27,130.50"\n'
        with mock.patch("fetch_data.requests.get", side_effect=[Response(""), Response(good_csv)]) as get, \
             mock.patch("fetch_data.time.sleep"):
            result = fetch_data.fetch_twse_intraday(now)

        self.assertEqual(result["price"], 27130.50)
        self.assertEqual(result["time"], "09:05")
        self.assertEqual(result["source"], "證交所 MI_5MINS_INDEX")
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs["params"], {"response": "csv", "date": "20261113"})

    def test_intraday_empty_response_reports_http_metadata_after_retries(self):
        class Response:
            status_code = 200
            headers = {"Content-Type": "text/html"}
            text = ""
            content = b""
            def raise_for_status(self):
                return None

        now = dt.datetime(2026, 11, 13, 10, 7, tzinfo=fetch_data.TZ_TAIPEI)
        with mock.patch("fetch_data.requests.get", return_value=Response()) as get, \
             mock.patch("fetch_data.time.sleep"):
            with self.assertRaisesRegex(ValueError, "HTTP 200.*body_bytes=0"):
                fetch_data.fetch_twse_intraday(now)

        self.assertEqual(get.call_count, 3)


if __name__ == "__main__":
    unittest.main()
