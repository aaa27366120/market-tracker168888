"""Collect official daily listed/OTC Taiwan stock snapshots.

This collector is intentionally separate from fetch_data.py and docs/data.json.
The exchange feeds are latest-session snapshots; this project accumulates them
by replacing each market's current snapshot while preserving the last good
snapshot when a source fails.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
from typing import Any

import requests

TZ_TAIPEI = dt.timezone(dt.timedelta(hours=8))
TIMEOUT = 30
HEADERS = {"User-Agent": "MarketTracker/1.0 (public market data dashboard)"}

SOURCES = {
    "listed": {
        "name": "TWSE",
        "master_url": "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
        "quotes_url": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
    },
    "otc": {
        "name": "TPEx",
        "master_url": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
        "quotes_url": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
    },
}
OUT = os.path.join(os.path.dirname(__file__), "docs", "stocks.json")


class StockDataError(RuntimeError):
    pass


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _pick(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _text(row.get(key))
        if value:
            return value
    return ""


def _number(value: Any, *, integer: bool = False) -> int | float | None:
    raw = _text(value).replace(",", "").replace("%", "")
    if raw in ("", "-", "--", "－", "—", "N/A"):
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    if integer:
        return int(number)
    return number


def roc_date(value: Any) -> str:
    """Convert ROC dates such as 1151102 or 115/11/02 to ISO dates."""
    raw = re.sub(r"\D", "", _text(value))
    if len(raw) != 7:
        raise StockDataError(f"無效的民國日期：{value!r}")
    year = int(raw[:3]) + 1911
    try:
        return dt.date(year, int(raw[3:5]), int(raw[5:7])).isoformat()
    except ValueError as exc:
        raise StockDataError(f"無效的民國日期：{value!r}") from exc


def parse_company_codes(rows: Any) -> set[str]:
    if not isinstance(rows, list) or not rows:
        raise StockDataError("官方公司基本資料不是非空 JSON 陣列")
    codes = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = _pick(row, "公司代號", "CompanyCode", "SecuritiesCompanyCode", "Code")
        name = _pick(row, "公司簡稱", "公司名稱", "CompanyName", "CompanyShortName", "Name")
        if code and name:
            codes.add(code)
    if len(codes) < 100:
        raise StockDataError(f"公司名單筆數異常：{len(codes)}")
    return codes


def _parse_quote(row: dict[str, Any], market: str, stock_codes: set[str]) -> dict[str, Any] | None:
    if market == "listed":
        symbol = _pick(row, "Code")
        name = _pick(row, "Name")
        date_value = _pick(row, "Date")
        keys = ("OpeningPrice", "HighestPrice", "LowestPrice", "ClosingPrice")
        volume_key, value_key, trades_key = "TradeVolume", "TradeValue", "Transaction"
    else:
        symbol = _pick(row, "SecuritiesCompanyCode", "Code")
        name = _pick(row, "CompanyName", "Name")
        date_value = _pick(row, "Date")
        keys = ("Open", "High", "Low", "Close")
        volume_key, value_key, trades_key = "TradingShares", "TransactionAmount", "TransactionNumber"

    if not symbol or symbol not in stock_codes:
        return None
    prices = [_number(row.get(key)) for key in keys]
    if any(value is None for value in prices):
        return None
    opening, high, low, close = prices
    if min(opening, high, low, close) <= 0 or high < max(opening, close, low) or low > min(opening, close, high):
        raise StockDataError(f"{market} {symbol} OHLC 數值關係異常")
    volume = _number(row.get(volume_key), integer=True)
    turnover = _number(row.get(value_key))
    transactions = _number(row.get(trades_key), integer=True)
    if volume is None or volume < 0:
        return None

    change_raw = _number(row.get("Change"))
    return {
        "symbol": symbol,
        "name": name or symbol,
        "market": "上市" if market == "listed" else "上櫃",
        "date": roc_date(date_value),
        "open": opening,
        "high": high,
        "low": low,
        "close": close,
        "change": change_raw,
        "volume_shares": volume,
        "turnover": turnover,
        "transactions": transactions,
        "source": "TWSE OpenAPI" if market == "listed" else "TPEx OpenAPI",
    }


def parse_market(market: str, master_rows: Any, quote_rows: Any) -> tuple[str, list[dict[str, Any]]]:
    if market not in SOURCES:
        raise StockDataError(f"未知市場：{market}")
    if not isinstance(quote_rows, list) or not quote_rows:
        raise StockDataError(f"{market} 行情不是非空 JSON 陣列")
    stock_codes = parse_company_codes(master_rows)
    parsed = []
    dates = set()
    for row in quote_rows:
        if not isinstance(row, dict):
            continue
        item = _parse_quote(row, market, stock_codes)
        if item:
            parsed.append(item)
            dates.add(item["date"])
    if len(dates) != 1:
        raise StockDataError(f"{market} 行情日期不一致或無有效日期：{sorted(dates)}")
    # Conservative completeness checks: prevent a WAF/error page or partial
    # response from replacing a previously good market snapshot.
    minimum = 500 if market == "listed" else 300
    if len(parsed) < minimum:
        raise StockDataError(f"{market} 有效股票筆數過少：{len(parsed)}（至少 {minimum}）")
    symbols = [item["symbol"] for item in parsed]
    if len(symbols) != len(set(symbols)):
        raise StockDataError(f"{market} 出現重複股票代號")
    return next(iter(dates)), sorted(parsed, key=lambda item: item["symbol"])


def fetch_json(url: str) -> Any:
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise StockDataError(f"來源未回傳有效 JSON：{url}") from exc
    if not isinstance(payload, list):
        raise StockDataError(f"來源回傳格式不是 JSON 陣列：{url}")
    return payload


def load_existing(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
        if isinstance(data, dict) and isinstance(data.get("markets"), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"schema_version": 1, "markets": {}}


def write_atomic(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="stocks-", suffix=".json", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def update(path: str = OUT, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now(TZ_TAIPEI)
    data = load_existing(path)
    markets = data.setdefault("markets", {})
    errors = []
    successes = 0

    for market, source in SOURCES.items():
        try:
            master_rows = fetch_json(source["master_url"])
            quote_rows = fetch_json(source["quotes_url"])
            trading_date, items = parse_market(market, master_rows, quote_rows)
            previous = markets.get(market) or {}
            previous_date = previous.get("trading_date")
            if previous_date and trading_date < previous_date:
                raise StockDataError(
                    f"{source['name']} 回傳日期倒退：{trading_date} < 已保存日期 {previous_date}"
                )
            markets[market] = {
                "source": source["name"],
                "source_url": source["quotes_url"],
                "trading_date": trading_date,
                "checked_at": now.isoformat(timespec="minutes"),
                "status": "success",
                "count": len(items),
                "items": items,
                "error": None,
            }
            successes += 1
        except Exception as exc:
            previous = markets.get(market) or {}
            markets[market] = {
                **previous,
                "source": source["name"],
                "source_url": source["quotes_url"],
                "status": "failed",
                "checked_at": now.isoformat(timespec="minutes"),
                "error": str(exc)[:500],
                "last_success_date": previous.get("trading_date"),
            }
            errors.append(f"{market}: {exc}")

    data.update({
        "schema_version": 1,
        "updated_at": now.isoformat(timespec="minutes"),
        "update_status": "success" if successes == len(SOURCES) else ("partial" if successes else "failed"),
        "successful_markets": successes,
        "total_markets": len(SOURCES),
        "errors": errors,
    })
    write_atomic(path, data)
    return data


if __name__ == "__main__":
    result = update()
    print(
        "個股資料更新："
        f"{result['successful_markets']}/{result['total_markets']} 市場成功；"
        f"狀態={result['update_status']}"
    )
    for error in result["errors"]:
        print("ERROR:", error)
    if result["successful_markets"] == 0:
        raise SystemExit("上市與上櫃行情均更新失敗")
