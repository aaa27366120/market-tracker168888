from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import stock_data


def fixture_rows(market="listed", count=501, date="1151112"):
    master, quotes = [], []
    for i in range(count):
        code = f"{(1000 if market == 'listed' else 2000) + i:04d}"
        master.append({"公司代號": code, "公司簡稱": f"公司{code}"})
        if market == "listed":
            quotes.append({
                "Date": date, "Code": code, "Name": f"公司{code}",
                "OpeningPrice": "10.00", "HighestPrice": "11.00",
                "LowestPrice": "9.00", "ClosingPrice": "10.50",
                "Change": "+0.50", "TradeVolume": "1,000",
                "TradeValue": "10,500", "Transaction": "100",
            })
        else:
            quotes.append({
                "Date": date, "SecuritiesCompanyCode": code, "CompanyName": f"公司{code}",
                "Open": "20", "High": "22", "Low": "19", "Close": "21",
                "Change": "1", "TradingShares": "2,000",
                "TransactionAmount": "42,000", "TransactionNumber": "200",
            })
    return master, quotes


class StockDataTests(unittest.TestCase):
    def test_roc_date_conversion(self):
        self.assertEqual(stock_data.roc_date("115/11/12"), "2026-11-12")
        self.assertEqual(stock_data.roc_date("1151102"), "2026-11-02")

    def test_listed_mapping_filters_non_company(self):
        master, quotes = fixture_rows()
        quotes.append({**quotes[0], "Code": "9999", "Name": "非公司名單證券"})
        date, items = stock_data.parse_market("listed", master, quotes)
        self.assertEqual(date, "2026-11-12")
        self.assertEqual(len(items), 501)
        self.assertEqual(items[0]["market"], "上市")
        self.assertEqual(items[0]["volume_shares"], 1000)
        self.assertNotIn("9999", {item["symbol"] for item in items})

    def test_otc_mapping(self):
        master, quotes = fixture_rows("otc", 301)
        date, items = stock_data.parse_market("otc", master, quotes)
        self.assertEqual(date, "2026-11-12")
        self.assertEqual(len(items), 301)
        self.assertEqual(items[0]["market"], "上櫃")
        self.assertEqual(items[0]["close"], 21)

    def test_single_bad_ohlc_row_is_skipped_and_counted(self):
        master, quotes = fixture_rows()
        quotes[0]["ClosingPrice"] = "0.00"
        date, items, stats = stock_data.parse_market("listed", master, quotes, return_stats=True)
        self.assertEqual(date, "2026-11-12")
        self.assertEqual(len(items), 500)
        self.assertEqual(stats["bad_rows"], 1)

    def test_bad_row_ratio_over_threshold_fails_market(self):
        master, quotes = fixture_rows()
        for row in quotes[:30]:
            row["ClosingPrice"] = "0"
        with self.assertRaises(stock_data.StockDataError):
            stock_data.parse_market("listed", master, quotes)

    def test_duplicate_symbols_fail(self):
        master, quotes = fixture_rows()
        quotes[-1] = dict(quotes[0])
        with self.assertRaisesRegex(stock_data.StockDataError, "重複"):
            stock_data.parse_market("listed", master, quotes)

    def test_too_few_rows_fail(self):
        master, quotes = fixture_rows(count=499)
        with self.assertRaisesRegex(stock_data.StockDataError, "筆數過少"):
            stock_data.parse_market("listed", master, quotes)

    def test_failed_source_preserves_previous_snapshot_and_partial_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "stocks.json")
            previous = {"schema_version": 1, "markets": {
                "listed": {"trading_date": "2026-11-11", "status": "success",
                           "count": 1000, "items": [{"symbol": "2330", "close": 1000}]},
                "otc": {"trading_date": "2026-11-11", "status": "success",
                        "count": 301, "items": [{"symbol": "6488", "close": 100}]},
            }}
            with open(path, "w", encoding="utf-8") as file:
                json.dump(previous, file)
            with patch.object(stock_data, "fetch_json", side_effect=[
                *fixture_rows(), RuntimeError("TPEx unavailable")
            ]):
                result = stock_data.update(path, dt.datetime(2026, 11, 13, 18, 0, tzinfo=stock_data.TZ_TAIPEI))
            self.assertEqual(result["update_status"], "partial")
            self.assertEqual(result["markets"]["listed"]["status"], "success")
            self.assertEqual(result["markets"]["otc"]["status"], "failed")
            self.assertEqual(result["markets"]["otc"]["items"][0]["close"], 100)
            self.assertEqual(result["markets"]["otc"]["last_success_date"], "2026-11-11")

    def test_date_regression_preserves_snapshot_and_fails_market(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "stocks.json")
            old = {"schema_version": 1, "markets": {"listed": {
                "trading_date": "2026-11-13", "status": "success", "count": 501,
                "items": [{"symbol": "2330", "close": 1000}]}}}
            with open(path, "w", encoding="utf-8") as file:
                json.dump(old, file)
            master, quotes = fixture_rows(date="1151112")
            with patch.object(stock_data, "fetch_json", side_effect=[master, quotes, RuntimeError("offline")]):
                result = stock_data.update(path, dt.datetime(2026, 11, 13, 18, 0, tzinfo=stock_data.TZ_TAIPEI))
            self.assertEqual(result["markets"]["listed"]["status"], "failed")
            self.assertEqual(result["markets"]["listed"]["trading_date"], "2026-11-13")
            self.assertIn("日期倒退", result["markets"]["listed"]["error"])

    def test_relative_count_floor_rejects_truncated_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "stocks.json")
            old = {"schema_version": 1, "markets": {"listed": {
                "trading_date": "2026-11-11", "status": "success", "count": 501,
                "items": []}}}
            with open(path, "w", encoding="utf-8") as file:
                json.dump(old, file)
            master, quotes = fixture_rows(count=799)
            with patch.object(stock_data, "fetch_json", side_effect=[master, quotes, RuntimeError("offline")]):
                result = stock_data.update(path, dt.datetime(2026, 11, 13, 18, 0, tzinfo=stock_data.TZ_TAIPEI))
            self.assertEqual(result["markets"]["listed"]["status"], "failed")
            self.assertIn("下降過多", result["markets"]["listed"]["error"])

    def test_write_atomic_creates_valid_compact_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "nested", "stocks.json")
            stock_data.write_atomic(path, {"markets": {"listed": {"status": "success"}}})
            with open(path, encoding="utf-8") as file:
                content = file.read()
            self.assertNotIn("\n  ", content)
            self.assertEqual(json.loads(content)["markets"]["listed"]["status"], "success")
            self.assertEqual(os.listdir(os.path.dirname(path)), ["stocks.json"])

    def test_snapshot_age_days(self):
        self.assertEqual(stock_data.snapshot_age_days(
            {"trading_date": "2026-11-09"}, dt.date(2026, 11, 13)), 4)
        self.assertIsNone(stock_data.snapshot_age_days({}, dt.date(2026, 11, 13)))


if __name__ == "__main__":
    unittest.main()
