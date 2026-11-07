"""Fetch market data, calculate neutral indicators, and write docs/data.json.

This project is an information dashboard, not investment advice.  A successful
workflow run is not treated as proof that every source returned fresh data.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
from typing import Any

import pandas as pd
import requests

OUT = os.path.join(os.path.dirname(__file__), "docs", "data.json")
TZ_TAIPEI = dt.timezone(dt.timedelta(hours=8))

# Symbol, display name, whether the provider's volume is meaningful for a ratio.
# Broad index symbols and FX do not have a comparable consolidated traded volume.
SYMBOLS = [
    ("^TWII", "台灣加權指數", False),
    ("^GSPC", "標普 500", False),
    ("^IXIC", "那斯達克指數", False),
    ("^DJI", "道瓊工業指數", False),
    ("^SOX", "費城半導體", False),
    ("GC=F", "黃金期貨", True),
    ("CL=F", "WTI 原油期貨", True),
    ("TWD=X", "美元兌台幣", False),
]
STOOQ = {
    "^GSPC": "^spx", "^IXIC": "^ndq", "^DJI": "^dji",
    "^SOX": "^sox", "GC=F": "gc.f", "CL=F": "cl.f",
    "TWD=X": "usdtwd",
}
SUPPORT_DAYS, NEAR, FAR = 60, 2.0, 5.0
FUT_URL = "https://openapi.taifex.com.tw/v1/DailyMarketReportFut"  # 期交所開放資料：期貨每日交易行情（只提供最新一日）
TWSE_TURNOVER_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK"  # 證交所：每日市場成交資訊


def from_yahoo(sym: str) -> pd.DataFrame:
    import yfinance as yf
    df = yf.Ticker(sym).history(period="1y", auto_adjust=False)
    if df is None or df.empty or "Close" not in df:
        raise ValueError("Yahoo Finance 沒有回傳有效行情")
    if "Low" not in df:
        df["Low"] = df["Close"]
    if "Volume" not in df:
        df["Volume"] = float("nan")
    return df[["Close", "Low", "Volume"]].dropna(subset=["Close"])


def from_stooq(sym: str) -> pd.DataFrame:
    if sym not in STOOQ:
        raise ValueError("此標的沒有 Stooq 備援代碼")
    response = requests.get(
        f"https://stooq.com/q/d/l/?s={STOOQ[sym]}&i=d", timeout=20
    )
    response.raise_for_status()
    if not response.text.strip() or "Date" not in response.text.splitlines()[0]:
        raise ValueError("Stooq 回傳內容不是有效 CSV")
    df = pd.read_csv(io.StringIO(response.text), parse_dates=["Date"], index_col="Date").tail(260)
    if "Volume" not in df:
        df["Volume"] = float("nan")
    if "Low" not in df:
        df["Low"] = df["Close"]
    return df[["Close", "Low", "Volume"]].dropna(subset=["Close"])


def judge(close: float, support: float, dist: float, vr: float | None) -> tuple[str, str]:
    if close < support:
        return "收盤低於近 60 日支撐，留意後續是否站回", "break"
    if dist <= NEAR:
        extra = "量縮" if vr is not None and vr < 0.8 else "放量" if vr is not None and vr > 1.5 else ""
        return "接近近 60 日支撐" + (f"，{extra}" if extra else ""), "near"
    if dist > FAR:
        return "距近 60 日支撐尚遠", "ok"
    return "與近 60 日支撐有一段距離", "ok"


def twse_turnover() -> dict[str, Any] | None:
    """加權指數用「成交金額」算量比（Yahoo 的指數成交量不可靠）。失敗回傳 None。"""
    try:
        first = dt.datetime.now(TZ_TAIPEI).replace(day=1)
        months = [(first - dt.timedelta(days=1)).replace(day=1), first]
        rows = []
        for m in months:
            r = requests.get(TWSE_TURNOVER_URL, params={"date": m.strftime("%Y%m01"), "response": "json"}, timeout=20)
            r.raise_for_status()
            rows += r.json().get("data") or []
        series = []
        for row in rows:
            y, mo, d = str(row[0]).split("/")
            series.append((f"{int(y) + 1911}-{mo}-{d}", int(str(row[2]).replace(",", "")) / 1e8))
        series.sort()
        if len(series) < 21:
            raise ValueError("成交金額歷史不足 21 日")
        prior = [v for _, v in series[-21:-1] if v > 0]
        if not prior or series[-1][1] <= 0:
            raise ValueError("成交金額數值異常")
        return {"date": series[-1][0], "amount_yi": round(series[-1][1], 1),
                "ratio": series[-1][1] / (sum(prior) / len(prior))}
    except Exception as exc:
        print(f"加權指數成交金額抓取失敗：{exc}")
        return None


# ---------- 台指期（期交所開放資料；欄位名稱未實測，已做容錯） ----------
def num(x: Any) -> float | None:
    try:
        return float(re.sub(r"[,%▼▲\s]", "", str(x)))
    except ValueError:
        return None


def pick(r: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if r.get(k) not in (None, "", "-"):
            return r[k]
    return None


def tx_rows() -> dict[str, dict[str, dict[str, Any]]]:
    """{YYYYMMDD: {"day": 近月一般盤, "night": 近月盤後}}"""
    resp = requests.get(FUT_URL, timeout=30)
    resp.raise_for_status()
    rows = resp.json()
    by: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        if pick(r, "Contract", "契約") not in ("TX", "臺股期貨"):
            continue
        m = str(pick(r, "ContractMonth(Week)", "到期月份(週別)") or "")
        d = re.sub(r"\D", "", str(pick(r, "Date", "日期") or ""))
        if not (m.isdigit() and len(m) == 6 and len(d) == 8):
            continue  # 排除週契約與格式不明的列
        sess = str(pick(r, "TradingSession", "交易時段") or "")
        k = "night" if ("盤後" in sess or "after" in sess.lower()) else "day"
        row = {"m": m, "high": num(pick(r, "High", "最高價")), "low": num(pick(r, "Low", "最低價")),
               "last": num(pick(r, "Last", "最後成交價")), "chg_pct": num(pick(r, "%", "漲跌%")),
               "vol": num(pick(r, "Volume", "成交量")), "oi": num(pick(r, "OpenInterest", "未沖銷契約量"))}
        cur = by.setdefault(d, {}).get(k)
        if cur is None or m < cur["m"]:
            by[d][k] = row
    if not by:
        print("台指期：沒解析到資料，第一列欄位為", list(rows[0].keys()) if rows else "空")
    return by


def fut_records(by: dict) -> dict[str, list[dict[str, Any]]]:
    """台指期=一般盤；台指期（全）=盤後+一般合併（高低取極值、量相加，收盤取一般盤收盤）。"""
    out: dict[str, list[dict[str, Any]]] = {"TX": [], "TXALL": []}
    for d, s_ in sorted(by.items()):
        day, night = s_.get("day"), s_.get("night")
        if not day or day["last"] is None:
            continue
        base = {"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "m": day["m"], "close": day["last"],
                "oi": day["oi"], "chg_pct": day["chg_pct"]}
        out["TX"].append({**base, "high": day["high"], "low": day["low"], "vol": day["vol"]})
        parts = [x for x in (night, day) if x]
        hs = [x["high"] for x in parts if x["high"] is not None]
        ls = [x["low"] for x in parts if x["low"] is not None]
        out["TXALL"].append({**base, "high": max(hs) if hs else None, "low": min(ls) if ls else None,
                             "vol": sum(x["vol"] or 0 for x in parts)})
    return out


def fut_item(sym: str, name: str, hist: list[dict[str, Any]], live: tuple | None, ok: bool) -> dict[str, Any]:
    if not hist:
        return {"sym": sym, "name": name, "error": "歷史資料累積中，之後每天會自動增加", "stale": True}
    last, prior = hist[-1], hist[:-1][-SUPPORT_DAYS:]
    lows = [x["low"] for x in prior if x.get("low") is not None]
    vols = [x["vol"] for x in prior[-20:] if x.get("vol")]
    it: dict[str, Any] = {
        "sym": sym, "name": name, "date": last["date"], "close": last["close"], "chg": last["chg_pct"],
        "oi": last["oi"], "volume": last["vol"], "volume_unit": "口", "support": None, "dist": None,
        "vol_ratio": None, "volume_status": "累積中", "level": "ok", "stale": not ok,
        "spark": [x["close"] for x in hist[-SUPPORT_DAYS:]], "history_days": len(hist),
    }
    if len(lows) >= 5:
        it["support"] = min(lows)
        it["dist"] = round((last["close"] / it["support"] - 1) * 100, 1)
        if vols and last["vol"]:
            it["vol_ratio"] = round(last["vol"] / (sum(vols) / len(vols)), 2)
            it["volume_status"] = "有效"
        it["text"], it["level"] = judge(last["close"], it["support"], it["dist"], it["vol_ratio"])
    else:
        it["text"] = f"歷史資料累積中（{len(hist)}/60 天），滿 5 天後開始顯示支撐"
    if live:  # 夜盤進行中或剛結束：顯示盤後最新價
        it["close"], it["chg"], it["date"] = live[1]["last"], live[1]["chg_pct"], live[0]
        it["text"] = "盤後最新價；" + it["text"]
    if not ok:
        it["text"] = f"期交所更新失敗，顯示舊資料（{last['date']}）"
    return it


def update_futures(hist: dict[str, Any]) -> tuple[dict[str, list], list[dict[str, Any]]]:
    hist = {k: list(v) for k, v in (hist or {}).items()}
    ok, live = True, None
    try:
        by = tx_rows()
        ok = bool(by)
        for k, recs in fut_records(by).items():  # 以日期合併累積（API 只給最新一日）
            merged = {x["date"]: x for x in hist.get(k, [])}
            merged.update({x["date"]: x for x in recs})
            hist[k] = [merged[d] for d in sorted(merged)][-130:]
        if by:
            ld = max(by)
            if "night" in by[ld] and "day" not in by[ld] and by[ld]["night"]["last"]:
                live = (f"{ld[:4]}-{ld[4:6]}-{ld[6:]}", by[ld]["night"])
    except Exception as exc:
        ok = False
        print(f"台指期更新失敗：{exc}")
    items = [fut_item("TX", "台指期（近月）", hist.get("TX", []), None, ok),
             fut_item("TXALL", "台指期（全）", hist.get("TXALL", []), live, ok)]
    return hist, items


def analyse(sym: str, name: str, has_vol: bool, override: dict[str, Any] | None = None) -> dict[str, Any]:
    errors = []
    df = None
    for source in (from_yahoo, from_stooq):
        if source is from_stooq and sym not in STOOQ:
            continue
        try:
            candidate = source(sym)
            if candidate is not None and len(candidate) >= SUPPORT_DAYS + 2:
                df = candidate
                break
            errors.append(f"{source.__name__}: 歷史資料不足")
        except Exception as exc:  # keep trying the fallback source
            errors.append(f"{source.__name__}: {exc}")

    if df is None:
        raise RuntimeError("；".join(errors) or "沒有可用行情來源")

    df = df.sort_index()
    last, prev = df.iloc[-1], df.iloc[-2]
    hist = df.iloc[-1 - SUPPORT_DAYS:-1]
    support = float(hist["Low"].min())
    ma60 = float(df["Close"].tail(60).mean())
    close, previous_close = float(last["Close"]), float(prev["Close"])
    if support <= 0 or ma60 <= 0 or previous_close <= 0:
        raise ValueError("行情數值異常，無法計算指標")

    dist = (close / support - 1) * 100
    one_year_high = float(df["Close"].max())
    drawdown = max(0.0, (one_year_high - close) / one_year_high * 100)

    # Treat zero/missing provider volume as unavailable, never as a genuine 0x ratio.
    vol_ratio, volume, unit = None, None, ""
    if has_vol and "Volume" in df:
        volumes = pd.to_numeric(df["Volume"], errors="coerce")
        current_volume = volumes.iloc[-1]
        prior_volumes = volumes.iloc[-21:-1].where(volumes.iloc[-21:-1] > 0).dropna()
        if pd.notna(current_volume) and current_volume > 0 and not prior_volumes.empty:
            avg_volume = float(prior_volumes.mean())
            if avg_volume > 0:
                vol_ratio = float(current_volume / avg_volume)
                volume, unit = float(current_volume), "口"

    last_date = pd.Timestamp(df.index[-1]).strftime("%Y-%m-%d")
    if override and override.get("date") == last_date:  # 加權指數：改用證交所成交金額
        vol_ratio, volume, unit = override["ratio"], override["amount_yi"], "億元"

    if close < support:
        text, level = "收盤低於近 60 日支撐，留意後續是否站回", "break"
    elif dist <= NEAR:
        extra = "量縮" if vol_ratio is not None and vol_ratio < 0.8 else "放量" if vol_ratio is not None and vol_ratio > 1.5 else ""
        text, level = "接近近 60 日支撐" + (f"，{extra}" if extra else ""), "near"
    elif dist > FAR:
        text, level = "距近 60 日支撐尚遠", "ok"
    else:
        text, level = "與近 60 日支撐有一段距離", "ok"

    # Drawdown is a descriptive risk statistic, not an entry/buy signal.
    if drawdown >= 25:
        drawdown_status = "回落超過 25%（高回落觀察）"
    elif drawdown >= 15:
        drawdown_status = "回落超過 15%（回落觀察）"
    else:
        drawdown_status = "未達 15% 回落門檻"

    return {
        "sym": sym, "name": name,
        "date": pd.Timestamp(df.index[-1]).strftime("%Y-%m-%d"),
        "close": round(close, 2),
        "chg": round((close / previous_close - 1) * 100, 2),
        "support": round(support, 2), "dist": round(dist, 1),
        "ma60": round(ma60, 2), "vs_ma60": round((close / ma60 - 1) * 100, 1),
        "vol_ratio": None if vol_ratio is None else round(vol_ratio, 2),
        "volume": volume, "volume_unit": unit,
        "volume_status": "有效" if vol_ratio is not None else "不適用" if not has_vol else "無有效成交量資料",
        "drawdown": round(drawdown, 1), "drawdown_status": drawdown_status,
        "text": text, "level": level,
        "spark": [round(float(x), 2) for x in df["Close"].tail(60)],
        "stale": False,
    }


def foreign_flow(previous: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return foreign-investor net flow history and explicit source freshness."""
    fetched_at = dt.datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M")
    try:
        response = requests.get(
            "https://www.twse.com.tw/rwd/zh/fund/BFI82U?type=day&response=json",
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("stat") not in (None, "OK"):
            raise ValueError(f"證交所回應狀態：{payload.get('stat')}")
        rows = payload.get("data") or []
        row = next(r for r in rows if str(r[0]).startswith("外資及陸資(不含外資自營商)"))
        net = round(int(str(row[3]).replace(",", "")) / 1e8, 1)
        raw_date = str(payload.get("date", ""))
        source_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if len(raw_date) == 8 else raw_date
        if not source_date:
            raise ValueError("證交所資料缺少日期")
        history = [x for x in previous if x.get("date") != source_date]
        history.append({"date": source_date, "net_yi": net})
        history = sorted(history, key=lambda x: x.get("date", ""))[-20:]
        return history, {"status": "success", "message": "最新抓取成功", "source_date": source_date, "checked_at": fetched_at}
    except Exception as exc:
        if previous:
            source_date = previous[-1].get("date")
            return previous[-20:], {
                "status": "stale", "message": f"本次抓取失敗，沿用舊資料：{exc}",
                "source_date": source_date, "checked_at": fetched_at,
            }
        return [], {
            "status": "unavailable", "message": f"目前無可用外資資料：{exc}",
            "source_date": None, "checked_at": fetched_at,
        }


def load_previous() -> dict[str, Any]:
    try:
        with open(OUT, encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> None:
    previous = load_previous()
    old_by_symbol = {x.get("sym"): x for x in previous.get("items", []) if isinstance(x, dict)}
    turnover = twse_turnover()
    fut_hist, fut_items = update_futures(previous.get("fut_hist", {}))
    items = []
    success_count = sum(1 for x in fut_items if not x.get("error") and not x.get("stale"))
    total = len(SYMBOLS) + len(fut_items)
    for symbol in SYMBOLS:
        sym, name, _ = symbol
        try:
            item = analyse(*symbol, override=turnover if sym == "^TWII" else None)
            success_count += 1
        except Exception as exc:
            old = old_by_symbol.get(sym)
            if old and old.get("date") and old.get("close") is not None:
                item = dict(old)
                item["stale"] = True
                item["fetch_error"] = str(exc)
                item["text"] = f"行情更新失敗，顯示舊資料（{item['date']}）"
            else:
                item = {"sym": sym, "name": name, "error": str(exc), "stale": True}
            print(f"[{sym}] 更新失敗：{exc}")
        items.append(item)
        if sym == "^TWII":  # 台指期放在加權指數後面
            items.extend(fut_items)

    foreign, foreign_meta = foreign_flow(previous.get("foreign", []))
    now = dt.datetime.now(TZ_TAIPEI)
    data = {
        "updated": now.strftime("%Y-%m-%d %H:%M"),
        "update_status": "success" if success_count == total else "partial" if success_count else "failed",
        "successful_items": success_count,
        "total_items": total,
        "items": items,
        "foreign": foreign,
        "foreign_meta": foreign_meta,
        "fut_hist": fut_hist,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    temp_out = OUT + ".tmp"
    with open(temp_out, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temp_out, OUT)
    print(f"完成 {data['updated']}；行情成功 {success_count}/{total}；外資狀態 {foreign_meta['status']}")


if __name__ == "__main__":
    main()
