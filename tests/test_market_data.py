import datetime as dt
import unittest
from unittest.mock import MagicMock, patch

import fetch_data
from fetch_data import fut_records, parse_twse_intraday_csv, parse_twse_mis_intraday, signed_num


def test_futures_night_and_day_are_separate():
    by = {
        "20261110": {
            "day": {"m": "202611", "last": 42623, "high": 42700, "low": 42400,
                    "vol": 12000, "oi": 1000, "chg_pct": 0.5},
            "night": {"m": "202611", "last": 42623, "high": 42800, "low": 42500,
                      "vol": 8000, "oi": 1100, "chg_pct": 0.7},
        }
    }
    result = fut_records(by)
    assert result["TX"][0]["close"] == 42623
    assert result["TX"][0]["vol"] == 12000
    assert result["TXALL"][0]["close"] == 42623
    assert result["TXALL"][0]["high"] == 42800
    assert result["TXALL"][0]["low"] == 42500
    assert result["TXALL"][0]["vol"] == 8000
    assert result["TXALL"][0]["session"] == "night"


def test_night_only_record_is_kept_without_day_record():
    by = {
        "20261111": {
            "night": {"m": "202611", "last": 42750, "high": 42800, "low": 42600,
                      "vol": 5000, "oi": 1200, "chg_pct": 0.3}
        }
    }
    result = fut_records(by)
    assert result["TX"] == []
    assert result["TXALL"][0]["close"] == 42750


def test_twse_intraday_csv_parser_uses_last_valid_taiex_row():
    raw = """每日收盤行情
時間,發行量加權股價指數,電子類指數
=09:00:00,"42,500.10","1,200.00"
=09:00:05,"42,501.20","1,201.00"
=09:00:10,"42,502.30","1,202.00"
"""
    parsed = parse_twse_intraday_csv(raw, "2026-11-10")
    assert parsed == {"date": "2026-11-10", "time": "09:00:10", "price": 42502.3}



def test_taifex_down_arrow_is_negative():
    assert signed_num("▼0.85%") == -0.85
    assert signed_num("▲0.35%") == 0.35
    assert signed_num("-0.20%") == -0.2



def test_twse_mis_intraday_parser_reads_taiex_quote():
    payload = {
        "rtcode": "0000",
        "msgArray": [{
            "c": "t00", "d": "20261113", "t": "10:30:05",
            "z": "42711.92",
        }],
    }
    assert parse_twse_mis_intraday(payload, "2026-11-13") == {
        "date": "2026-11-13", "time": "10:30:05", "price": 42711.92
    }


def test_twse_mis_intraday_parser_rejects_stale_date():
    payload = {
        "rtcode": "0000",
        "msgArray": [{
            "c": "t00", "d": "20261112", "t": "13:30:00",
            "z": "42600.00",
        }],
    }
    try:
        parse_twse_mis_intraday(payload, "2026-11-13")
    except ValueError as exc:
        assert "日期不符" in str(exc)
    else:
        raise AssertionError("stale MIS date must be rejected")



class TestFetchTwseIntradayFlow(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 11, 13, 10, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        self.landing = MagicMock()
        self.landing.raise_for_status.return_value = None

    def response(self, payload=None, text="", content=b""):
        response = MagicMock()
        response.status_code = 200
        response.headers = {"Content-Type": "application/json"}
        response.content = content or (text.encode("utf-8") if text else b'{"msgArray":[]}')
        response.encoding = "utf-8"
        response.text = text
        response.json.return_value = payload if payload is not None else {}
        response.raise_for_status.return_value = None
        return response

    def test_mis_success_does_not_call_csv(self):
        mis = self.response({
            "rtcode": "0000",
            "msgArray": [{"c": "t00", "d": "20261113", "t": "10:30:05", "z": "42711.92"}],
        })
        session = MagicMock()
        session.get.side_effect = [self.landing, mis]
        with patch("fetch_data.requests.Session", return_value=session), \
             patch("fetch_data.retry", side_effect=lambda fn, *a, **kw: fn(*a, **kw)), \
             patch("fetch_data.requests.get") as csv_get:
            result = fetch_data.fetch_twse_intraday(self.now)
        self.assertEqual(result["source"], "證交所 MIS 即時指數")
        self.assertEqual(result["price"], 42711.92)
        csv_get.assert_not_called()
        session.close.assert_called_once()

    def test_mis_failure_csv_success_uses_fallback(self):
        mis = self.response({"rtcode": "0001", "msgArray": []})
        csv = self.response(text='時間,發行量加權股價指數\n=10:30:00,"42,711.92"\n')
        session = MagicMock()
        session.get.side_effect = [self.landing, mis]
        with patch("fetch_data.requests.Session", return_value=session), \
             patch("fetch_data.retry", side_effect=lambda fn, *a, **kw: fn(*a, **kw)), \
             patch("fetch_data.requests.get", return_value=csv):
            result = fetch_data.fetch_twse_intraday(self.now)
        self.assertEqual(result["source"], "證交所 MI_5MINS_INDEX（備援）")
        self.assertEqual(result["price"], 42711.92)

    def test_both_sources_fail_includes_both_errors(self):
        mis = self.response({"rtcode": "0001", "msgArray": []})
        csv = self.response(text="invalid csv")
        csv.raise_for_status.side_effect = None
        session = MagicMock()
        session.get.side_effect = [self.landing, mis]
        with patch("fetch_data.requests.Session", return_value=session), \
             patch("fetch_data.retry", side_effect=lambda fn, *a, **kw: fn(*a, **kw)), \
             patch("fetch_data.requests.get", return_value=csv):
            with self.assertRaises(RuntimeError) as caught:
                fetch_data.fetch_twse_intraday(self.now)
        self.assertIn("MIS：", str(caught.exception))
        self.assertIn("CSV：", str(caught.exception))

    def test_mis_dash_price_is_rejected_and_csv_fallback_used(self):
        mis = self.response({
            "rtcode": "0000",
            "msgArray": [{"c": "t00", "d": "20261113", "t": "10:30:05", "z": "-"}],
        })
        csv = self.response(text='時間,發行量加權股價指數\n=10:30:00,"42,711.92"\n')
        session = MagicMock()
        session.get.side_effect = [self.landing, mis]
        with patch("fetch_data.requests.Session", return_value=session), \
             patch("fetch_data.retry", side_effect=lambda fn, *a, **kw: fn(*a, **kw)), \
             patch("fetch_data.requests.get", return_value=csv):
            result = fetch_data.fetch_twse_intraday(self.now)
        self.assertEqual(result["source"], "證交所 MI_5MINS_INDEX（備援）")
