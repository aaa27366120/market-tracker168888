"""台指期新鮮度模型的回歸測試。

事實基礎（2026-11-13 ～ 11-17 的 docs/data.json 歷史）：
期交所 DailyMarketReportFut 在交易日 D 的「隔天早上之後」才公布 D，從不在 D 當天公布；
發布時間不固定（07:46、08:59，11/16 的資料延到 11/17 白天）。
"""
import datetime as dt
import unittest
from unittest.mock import patch

import fetch_data


def at(day, hour, minute=0):
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=fetch_data.TZ_TAIPEI)


def row(date, close=44000, session="day"):
    return {"date": date, "m": "202612", "close": close, "high": close + 100,
            "low": close - 100, "vol": 1000, "session": session}


class ExpectedDateTests(unittest.TestCase):
    def expect(self, moment, date):
        self.assertEqual(fetch_data.expected_futures_dates(moment), {"TX": date, "TXALL": date})

    def test_never_expects_same_day_data(self):
        # 舊版 16:00 後預期當天，導致每天傍晚起都誤報失敗（11/13 16:57、11/16 16:41、11/17 16:10）。
        for clock in ((7, 0), (9, 7), (12, 0), (16, 7), (18, 7), (23, 59)):
            self.expect(at(17, *clock), "2026-11-16")

    def test_before_publish_time_looks_one_more_day_back(self):
        self.expect(at(17, 6, 59), "2026-11-13")  # 週二清晨：週一資料最快 07:00 後才會出現
        self.expect(at(16, 6, 0), "2026-11-13")   # 週一清晨：週五資料已於週六公布

    def test_weekend_expects_friday(self):
        self.expect(at(14, 7, 10), "2026-11-13")
        self.expect(at(15, 12, 0), "2026-11-13")

    def test_trading_days_behind_skips_weekend(self):
        self.assertEqual(fetch_data.trading_days_behind("2026-11-13", "2026-11-16"), 1)
        self.assertEqual(fetch_data.trading_days_behind("2026-11-12", "2026-11-16"), 2)
        self.assertEqual(fetch_data.trading_days_behind("2026-11-16", "2026-11-16"), 0)


class FetchDueTests(unittest.TestCase):
    def hist(self, date):
        return {"TX": [row(date)], "TXALL": [row(date, session="night")]}

    def test_daytime_check_when_behind(self):
        # 11/17 09:00–16:00 舊版不查期交所，錯過白天發布的 11/16 資料。
        for clock in ((9, 8), (11, 22), (14, 32), (15, 13)):
            self.assertTrue(fetch_data.futures_fetch_due(self.hist("2026-11-13"), at(17, *clock)))

    def test_no_repeat_fetch_once_caught_up(self):
        for clock in ((7, 45), (12, 0), (16, 10), (22, 7)):
            self.assertFalse(fetch_data.futures_fetch_due(self.hist("2026-11-16"), at(17, *clock)))

    def test_one_session_behind_still_fetches(self):
        hist = {"TX": [row("2026-11-16")], "TXALL": [row("2026-11-13", session="night")]}
        self.assertTrue(fetch_data.futures_fetch_due(hist, at(17, 12)))


class UpdateFuturesTests(unittest.TestCase):
    def run_update(self, records, now, hist=None):
        with patch.object(fetch_data, "tx_rows", return_value={"x": {}}), patch.object(
            fetch_data, "fut_records", return_value=records
        ):
            return fetch_data.update_futures(hist or {}, fetch=True, now=now)

    def by_sym(self, items):
        return {item["sym"]: item for item in items}

    def test_fresh_data_is_success(self):
        _, items = self.run_update(
            {"TX": [row("2026-11-16")], "TXALL": [row("2026-11-16", session="night")]}, at(17, 16, 10))
        for item in items:
            self.assertEqual(item["data_status"], "success")
            self.assertFalse(item["stale"])
            self.assertTrue(item["checked_this_run"])
            self.assertEqual(item["checked_at"], "2026-11-17 16:10")

    def test_api_ok_but_not_published_is_waiting_not_failed(self):
        # 11/17 07:45：API 恢復，但只有 11/13 → 應為「尚未公布」，不是紅字失敗。
        _, items = self.run_update(
            {"TX": [row("2026-11-13")], "TXALL": [row("2026-11-13", session="night")]}, at(17, 7, 45))
        for item in items:
            self.assertEqual(item["data_status"], "waiting")
            self.assertFalse(item["stale"])
            self.assertIn("尚未公布 2026-11-16", item["status_note"])

    def test_two_trading_days_behind_is_overdue(self):
        _, items = self.run_update(
            {"TX": [row("2026-11-12")], "TXALL": [row("2026-11-12", session="night")]}, at(17, 12))
        for item in items:
            self.assertEqual(item["data_status"], "overdue")
            self.assertTrue(item["stale"])
            self.assertEqual(item["days_behind"], 2)

    def test_sessions_are_judged_independently(self):
        _, items = self.run_update(
            {"TX": [row("2026-11-16")], "TXALL": [row("2026-11-13", session="night")]}, at(17, 12))
        by = self.by_sym(items)
        self.assertEqual(by["TX"]["data_status"], "success")
        self.assertEqual(by["TXALL"]["data_status"], "waiting")

    def test_missing_session_without_history_is_unavailable(self):
        _, items = self.run_update({"TX": [row("2026-11-16")], "TXALL": []}, at(17, 12))
        by = self.by_sym(items)
        self.assertEqual(by["TX"]["data_status"], "success")
        self.assertEqual(by["TXALL"]["data_status"], "unavailable")
        self.assertTrue(by["TXALL"]["stale"])

    def test_fetch_exception_keeps_values_and_marks_failed_when_behind(self):
        hist = {"TX": [row("2026-11-13")], "TXALL": [row("2026-11-13", session="night")]}
        with patch.object(fetch_data, "tx_rows", side_effect=RuntimeError("non-JSON")):
            updated, items = fetch_data.update_futures(hist, fetch=True, now=at(17, 12))
        self.assertEqual(updated["TX"][0]["close"], 44000)
        for item in items:
            self.assertEqual(item["data_status"], "failed")
            self.assertTrue(item["stale"])
            self.assertIn("non-JSON", item["fetch_error"])

    def test_fetch_exception_when_already_current_is_not_red(self):
        hist = {"TX": [row("2026-11-16")], "TXALL": [row("2026-11-16", session="night")]}
        with patch.object(fetch_data, "tx_rows", side_effect=RuntimeError("timeout")):
            _, items = fetch_data.update_futures(hist, fetch=True, now=at(17, 20))
        for item in items:
            self.assertEqual(item["data_status"], "success")
            self.assertIn("timeout", item["fetch_error"])

    def test_not_fetched_keeps_previous_check_time(self):
        hist = {"TX": [row("2026-11-16")], "TXALL": [row("2026-11-16", session="night")]}
        previous = {"TX": {"checked_at": "2026-11-17 16:10"}, "TXALL": {"checked_at": "2026-11-17 16:10"}}
        _, items = fetch_data.update_futures(hist, fetch=False, now=at(17, 18, 7), previous_items=previous)
        for item in items:
            self.assertEqual(item["checked_at"], "2026-11-17 16:10")
            self.assertFalse(item["checked_this_run"])
            self.assertIn("本次未查詢", fetch_data.futures_log_line(item))

    def test_not_fetched_preserves_previous_failure_while_behind(self):
        hist = {"TX": [row("2026-11-13")], "TXALL": [row("2026-11-13", session="night")]}
        previous = {"TX": {"data_status": "failed", "fetch_error": "HTTP 503"}}
        _, items = fetch_data.update_futures(hist, fetch=False, now=at(17, 12), previous_items=previous)
        by = self.by_sym(items)
        self.assertEqual(by["TX"]["data_status"], "failed")
        self.assertEqual(by["TX"]["fetch_error"], "HTTP 503")
        self.assertEqual(by["TXALL"]["data_status"], "waiting")


class TxRowsDiagnosticTests(unittest.TestCase):
    def test_non_json_error_includes_http_diagnostic(self):
        class Resp:
            status_code = 200
            headers = {"Content-Type": "text/html"}
            content = b"<html>maintenance</html>"
            encoding = "utf-8"

            def raise_for_status(self):
                pass

            def json(self):
                raise ValueError("Expecting value: line 1 column 1 (char 0)")

        with patch.object(fetch_data, "retry", return_value=Resp()):
            with self.assertRaises(RuntimeError) as ctx:
                fetch_data.tx_rows()
        message = str(ctx.exception)
        self.assertIn("HTTP 200", message)
        self.assertIn("Content-Type=text/html", message)
        self.assertIn("maintenance", message)


if __name__ == "__main__":
    unittest.main()
