from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import stock_data


class StockDataTests(unittest.TestCase):
    def test_roc_date_conversion(self):
        self.assertEqual(stock_data.roc_date("115/11/12"), "2026-11-12")
        self.assertEqual(stock_data.roc_date("1151102"), "2026-11-02")

    def test_listed_mapping_and_company_universe_filter(self):
        master = [{"公司代號": f"{i:04d}", "公司簡稱": f"公司{i}"} for i in range(1000, 1501)]
        quotes = [{
            "Date": "1151112", "Code": f"{i:04d}", "Name": f"公司{i}",
            "OpeningPrice": "10.00", "HighestPrice": "11.00",
            "LowestPrice": "9.00", "ClosingPrice": "10.50",
            "Change": "+0.50", "TradeVolume": "1,000",
            "TradeValue": "10,500", "Transaction": "100",
        } for i in range(1000, 1501)]
        quotes.append({
            "Date": "1151112", "Code": "9999", "Name": "非公司名單證券",
            "OpeningPrice": "10", "HighestPrice": "11", "LowestPrice": "9",
            "ClosingPrice": "10", "Change": "0", "TradeVolume": "100",
            "TradeValue": "1000", "Transaction": "10",
        })
        date, items = stock_data.parse_market("listed", master, quotes)
        self.assertEqual(date, "2026-11-12")
        self.assertEqual(len(items), 501)
        self.assertEqual(items[0]["market"], "上市")
        self.assertEqual(items[0]["volume_shares"], 1000)
        self.assertNotIn("9999", {item["symbol"] for item in items})

    def test_otc_mapping(self):
        master = [{"公司代號": f"{i:04d}", "公司簡稱": f"上櫃{i}"} for i in range(2000, 2301)]
        quotes = [{
            "Date": "1151112", "SecuritiesCompanyCode": f"{i:04d}",
            "CompanyName": f"上櫃{i}", "Open": "20", "High": "22",
            "Low": "19", "Close": "21", "Change": "1",
            "TradingShares": "2,000", "TransactionAmount": "42,000",
            "TransactionNumber": "200",
        } for i in range(2000, 2301)]
        date, items = stock_data.parse_market("otc", master, quotes)
        self.assertEqual(date, "2026-11-12")
        self.assertEqual(len(items), 301)
        self.assertEqual(items[0]["market"], "上櫃")
        self.assertEqual(items[0]["close"], 21)

    def test_failed_source_preserves_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "stocks.json")
            previous = {
                "schema_version": 1,
                "markets": {
                    "listed": {
                        "trading_date": "2026-11-11",
                        "status": "success",
                        "items": [{"symbol": "2330", "close": 1000}],
                    },
                    "otc": {
                        "trading_date": "2026-11-11",
                        "status": "success",
                        "items": [{"symbol": "6488", "close": 100}],
                    },
                },
            }
            with open(path, "w", encoding="utf-8") as file:
                json.dump(previous, file)
            with patch.object(stock_data, "fetch_json", side_effect=RuntimeError("source unavailable")):
                result = stock_data.update(path, dt.datetime(2026, 11, 13, 18, 0, tzinfo=stock_data.TZ_TAIPEI))
            self.assertEqual(result["update_status"], "failed")
            self.assertEqual(result["markets"]["listed"]["items"][0]["close"], 1000)
            self.assertEqual(result["markets"]["listed"]["trading_date"], "2026-11-11")
            self.assertEqual(result["markets"]["listed"]["status"], "failed")
            self.assertEqual(result["markets"]["otc"]["items"][0]["close"], 100)


if __name__ == "__main__":
    unittest.main()
