import datetime as dt

from fetch_data import fut_records, parse_twse_intraday_csv


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
=09:00:00,42,500.10,1,200.00
=09:00:05,42,501.20,1,201.00
=09:00:10,42,502.30,1,202.00
"""
    parsed = parse_twse_intraday_csv(raw, "2026-11-10")
    assert parsed == {"date": "2026-11-10", "time": "09:00:10", "price": 42502.3}
