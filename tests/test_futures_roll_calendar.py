"""台指期換月（連續序列）與台股交易日曆的測試。"""
import datetime as dt
import importlib.util
import unittest
from unittest.mock import patch

import fetch_data

D = dt.date
# 測試用日曆：模擬 2026 年 2 月春節封關（2/12–2/20）與 10/9 國慶補假。
TEST_CAL = fetch_data.TradingCalendar(
    frozenset({D(2026, 2, 12), D(2026, 2, 13), D(2026, 2, 16), D(2026, 2, 17), D(2026, 2, 18),
               D(2026, 2, 19), D(2026, 2, 20), D(2026, 10, 9)}),
    D(2026, 1, 1), D(2026, 12, 31), "test")


def row(date, m, close, nxt=None, next_close=None, vol=1000):
    r = {"date": date, "m": m, "close": close, "high": close + 50, "low": close - 50,
         "vol": vol, "session": "day"}
    if nxt:
        r.update({"next_m": nxt, "next_close": next_close, "next_vol": 10})
    return r


class CalendarCase(unittest.TestCase):
    def setUp(self):
        self._p = patch.object(fetch_data, "_TW_CAL", TEST_CAL)
        self._p.start()

    def tearDown(self):
        self._p.stop()


class TradingCalendarTests(CalendarCase):
    def test_previous_trading_day_skips_holiday_and_weekend(self):
        self.assertEqual(TEST_CAL.previous_trading_day(D(2026, 10, 12)), D(2026, 10, 8))
        self.assertEqual(TEST_CAL.previous_trading_day(D(2026, 2, 23)), D(2026, 2, 11))

    def test_expected_date_after_long_holiday(self):
        # 春節後第一個交易日早上：預期仍是封關前最後一個交易日，不會誤判落後一週。
        now = dt.datetime(2026, 2, 23, 8, 0, tzinfo=fetch_data.TZ_TAIPEI)
        self.assertEqual(fetch_data.expected_futures_dates(now)["TX"], "2026-02-11")
        self.assertEqual(fetch_data.trading_days_behind("2026-02-11", "2026-02-11"), 0)

    def test_long_holiday_is_not_overdue(self):
        # 舊版（平日近似）會把春節算成落後 5～7 天，標成「過久未更新」。
        now = dt.datetime(2026, 2, 24, 10, 0, tzinfo=fetch_data.TZ_TAIPEI)
        hist = {"TX": [row("2026-02-11", "202603", 30000)]}
        _, items = fetch_data.update_futures(hist, fetch=False, now=now)
        tx = next(i for i in items if i["sym"] == "TX")
        self.assertEqual(tx["expected_date"], "2026-02-23")
        self.assertEqual(tx["days_behind"], 1)
        self.assertEqual(tx["data_status"], "waiting")

    def test_outside_calendar_range_uses_relaxed_threshold(self):
        self.assertEqual(fetch_data.overdue_threshold("2026-11-16"), fetch_data.FUT_OVERDUE_DAYS)
        self.assertEqual(fetch_data.overdue_threshold("2030-01-02"), fetch_data.FUT_OVERDUE_DAYS_APPROX)

    def test_fallback_calendar_is_weekday_only(self):
        cal = fetch_data.TradingCalendar()
        self.assertFalse(cal.covers(D(2026, 10, 9)))
        self.assertTrue(cal.is_trading_day(D(2026, 10, 9)))
        self.assertFalse(cal.is_trading_day(D(2026, 10, 10)))


@unittest.skipUnless(importlib.util.find_spec("exchange_calendars"), "exchange_calendars 未安裝")
class RealCalendarTests(unittest.TestCase):
    """確認實際載入的 XTAI 日曆包含 2026 年已知的休市日，且台指期 202611 到期日為交易日。"""

    @classmethod
    def setUpClass(cls):
        cls.cal = fetch_data.load_tw_calendar()

    def test_loaded(self):
        self.assertTrue(self.cal.covers(D(2026, 11, 18)), self.cal.source)
        self.assertIn("XTAI", self.cal.source)

    def test_known_2026_closures(self):
        for day in (D(2026, 1, 1), D(2026, 2, 16), D(2026, 2, 27), D(2026, 4, 6),
                    D(2026, 5, 1), D(2026, 6, 19), D(2026, 9, 25), D(2026, 10, 9)):
            self.assertFalse(self.cal.is_trading_day(day), day)

    def test_known_2026_trading_days(self):
        for day in (D(2026, 11, 16), D(2026, 11, 17), D(2026, 11, 18), D(2026, 11, 19)):
            self.assertTrue(self.cal.is_trading_day(day), day)


class ContractExpiryTests(CalendarCase):
    def test_third_wednesday(self):
        self.assertEqual(fetch_data.contract_last_trading_day("202611"), D(2026, 11, 18))
        self.assertEqual(fetch_data.contract_last_trading_day("202612"), D(2026, 12, 16))

    def test_postponed_when_third_wednesday_is_closed(self):
        # 2026/02 第三個週三（2/18）為春節休市 → 順延至次一交易日 2/23。
        self.assertEqual(fetch_data.contract_last_trading_day("202602"), D(2026, 2, 23))

    def test_expiry_notes(self):
        self.assertIn("將於 11/18 到期", fetch_data.expiry_note(row("2026-11-16", "202611", 1, "202612", 1)))
        self.assertIn("最後交易日", fetch_data.expiry_note(row("2026-11-18", "202611", 1, "202612", 1)))
        self.assertIsNone(fetch_data.expiry_note(row("2026-11-09", "202611", 1, "202612", 1)))


class ContinuousSeriesTests(CalendarCase):
    def test_no_roll_returns_same_prices(self):
        hist = [row("2026-11-16", "202611", 100), row("2026-11-17", "202611", 110)]
        series, rolls = fetch_data.continuous_futures(hist)
        self.assertEqual([x["close"] for x in series], [100, 110])
        self.assertEqual(rolls, [])

    def test_back_adjusts_with_spread_on_last_old_day(self):
        hist = [
            row("2026-11-16", "202611", 40000, "202612", 40150),
            row("2026-11-17", "202611", 40100, "202612", 40260),
            row("2026-11-18", "202611", 40200, "202612", 40380),  # 舊合約最後交易日：價差 +180
            row("2026-11-19", "202612", 40500, "202701", 40600),
        ]
        series, rolls = fetch_data.continuous_futures(hist)
        self.assertEqual([x["close"] for x in series], [40180, 40280, 40380, 40500])
        self.assertEqual([x["low"] for x in series][:1], [40130])
        self.assertEqual(rolls[0]["spread"], 180)
        self.assertTrue(rolls[0]["adjusted"])
        # 原始歷史不得被修改
        self.assertEqual(hist[0]["close"], 40000)

    def test_missing_spread_restarts_series_instead_of_splicing(self):
        hist = [row("2026-11-17", "202611", 40100), row("2026-11-18", "202611", 40200),
                row("2026-11-19", "202612", 40500)]
        series, rolls = fetch_data.continuous_futures(hist)
        self.assertEqual([x["date"] for x in series], ["2026-11-19"])
        self.assertFalse(rolls[0]["adjusted"])

    def test_two_rolls_accumulate(self):
        hist = [
            row("2026-10-21", "202610", 100, "202611", 110),
            row("2026-10-22", "202611", 120, "202612", 125),
            row("2026-11-18", "202611", 130, "202612", 140),
            row("2026-11-19", "202612", 150),
        ]
        series, _ = fetch_data.continuous_futures(hist)
        self.assertEqual([x["close"] for x in series], [120, 130, 140, 150])

    def test_fut_item_uses_adjusted_support_and_same_contract_volume(self):
        old = [row(f"2026-11-{d:02d}", "202611", 40000 + d, "202612", 40100 + d, vol=50000)
               for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 16, 17, 18)]
        hist = old + [row("2026-11-19", "202612", 40300, "202701", 40400, vol=60000)]
        now = dt.datetime(2026, 11, 20, 9, 0, tzinfo=fetch_data.TZ_TAIPEI)
        item = fetch_data.fut_item("TX", "台指期（近月）", hist, now)
        self.assertEqual(item["contract"], "202612")
        # 支撐 = 換月前最低價（40002 - 50）+ 價差 100
        self.assertEqual(item["support"], 40052)
        self.assertIsNone(item["vol_ratio"])
        self.assertEqual(item["volume_status"], "換月後累積中")
        self.assertIn("2026-11-19 起近月改為 202612 合約", item["contract_note"])
        self.assertIn("價差（+100 點）", item["contract_note"])
        self.assertEqual(item["spark"][0], 40102)


class TxRowsNextMonthTests(unittest.TestCase):
    def test_keeps_near_and_next_month_and_skips_weekly(self):
        rows = [
            {"Date": "20261118", "Contract": "TX", "ContractMonth(Week)": "202612", "TradingSession": "一般",
             "Last": "40380", "High": "1", "Low": "1", "%": "1", "Volume": "500", "OpenInterest": "1"},
            {"Date": "20261118", "Contract": "TX", "ContractMonth(Week)": "202611", "TradingSession": "一般",
             "Last": "40200", "High": "1", "Low": "1", "%": "1", "Volume": "90000", "OpenInterest": "1"},
            {"Date": "20261118", "Contract": "TX", "ContractMonth(Week)": "202611W4", "TradingSession": "一般",
             "Last": "1", "High": "1", "Low": "1", "%": "1", "Volume": "1", "OpenInterest": "1"},
            {"Date": "20261118", "Contract": "TX", "ContractMonth(Week)": "202701", "TradingSession": "一般",
             "Last": "40500", "High": "1", "Low": "1", "%": "1", "Volume": "10", "OpenInterest": "1"},
        ]

        class Resp:
            status_code, headers, content, encoding = 200, {}, b"[]", "utf-8"

            def raise_for_status(self):
                pass

            def json(self):
                return rows

        with patch.object(fetch_data, "retry", return_value=Resp()):
            by = fetch_data.tx_rows()
        day = by["20261118"]["day"]
        self.assertEqual((day["m"], day["last"], day["next_m"], day["next_last"]), ("202611", 40200, "202612", 40380))
        rec = fetch_data.fut_records(by)["TX"][0]
        self.assertEqual((rec["next_m"], rec["next_close"]), ("202612", 40380))


if __name__ == "__main__":
    unittest.main()
