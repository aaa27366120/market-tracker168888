"""每天抓行情 → 算支撐/量比/回落幅度 → 套規則輸出中性結論 → 寫入 docs/data.json
只做資訊整理,不是投資建議。"""
import io, json, os, datetime as dt
import pandas as pd, requests

OUT = os.path.join(os.path.dirname(__file__), "docs", "data.json")
# 標的設定:代碼、顯示名稱、是否有成交量
SYMBOLS = [
    ("^TWII", "台灣加權指數", True),
    ("^GSPC", "標普 500", True),
    ("^SOX", "費城半導體", True),
    ("GC=F", "黃金期貨", True),
    ("CL=F", "WTI 原油期貨", True),
    ("TWD=X", "美元兌台幣", False),
]
STOOQ = {"^GSPC": "^spx", "^SOX": "^sox", "GC=F": "gc.f", "CL=F": "cl.f", "TWD=X": "usdtwd"}
TRIGGERS = [(25, "已達第二批條件"), (15, "已達第一批條件")]  # 自行調整:自一年高點回落 %
SUPPORT_DAYS, NEAR, FAR = 60, 2.0, 5.0


def from_yahoo(sym):
    import yfinance as yf
    df = yf.Ticker(sym).history(period="1y", auto_adjust=False)
    return df[["Close", "Low", "Volume"]].dropna(subset=["Close"])


def from_stooq(sym):
    r = requests.get(f"https://stooq.com/q/d/l/?s={STOOQ[sym]}&i=d", timeout=20)
    df = pd.read_csv(io.StringIO(r.text), parse_dates=["Date"], index_col="Date").tail(260)
    if "Volume" not in df:
        df["Volume"] = 0
    return df[["Close", "Low", "Volume"]].dropna(subset=["Close"])


def analyse(sym, name, has_vol):
    df = None
    for src in (from_yahoo, from_stooq):
        try:
            if src is from_stooq and sym not in STOOQ:
                continue
            df = src(sym)
            if len(df) > 70:
                break
        except Exception as e:
            print(f"[{sym}] {src.__name__} 失敗: {e}")
            df = None
    if df is None or len(df) < 70:
        return {"sym": sym, "name": name, "error": "今天抓不到資料"}
    last, prev = df.iloc[-1], df.iloc[-2]
    hist = df.iloc[-1 - SUPPORT_DAYS:-1]
    support = float(hist["Low"].min())
    ma60 = float(df["Close"].tail(60).mean())
    close = float(last["Close"])
    dist = (close / support - 1) * 100
    dd = (close / float(df["Close"].max()) - 1) * -100
    vr = None
    if has_vol and df["Volume"].tail(21).iloc[:-1].mean() > 0:
        vr = float(last["Volume"] / df["Volume"].tail(21).iloc[:-1].mean())
    # 規則式結論(中性用語)
    if close < support:
        text, level = "收盤低於近 60 日低點,等待後續確認", "break"
    elif dist <= NEAR:
        extra = "量縮" if vr and vr < 0.8 else "放量" if vr and vr > 1.5 else ""
        text, level = "接近近 60 日低點" + (f",{extra}" if extra else ""), "near"
    elif dist > FAR:
        text, level = "距支撐尚遠", "ok"
    else:
        text, level = "與支撐有一段距離", "ok"
    for pct, label in TRIGGERS:
        if dd >= pct:
            text += f";{label}(回落 {dd:.1f}%)"
            level = "trigger" if level != "break" else level
            break
    return {
        "sym": sym, "name": name, "date": df.index[-1].strftime("%Y-%m-%d"),
        "close": round(close, 2), "chg": round((close / float(prev["Close"]) - 1) * 100, 2),
        "support": round(support, 2), "dist": round(dist, 1), "ma60": round(ma60, 2),
        "vs_ma60": round((close / ma60 - 1) * 100, 1), "vol_ratio": None if vr is None else round(vr, 2),
        "drawdown": round(dd, 1), "text": text, "level": level,
        "spark": [round(float(x), 2) for x in df["Close"].tail(60)],
    }


def foreign_flow(prev_list):
    """證交所三大法人買賣金額統計(外資及陸資,不含外資自營商),單位:億元。失敗就沿用舊資料。"""
    try:
        j = requests.get("https://www.twse.com.tw/rwd/zh/fund/BFI82U?type=day&response=json", timeout=20).json()
        row = next(r for r in j["data"] if r[0].startswith("外資及陸資(不含外資自營商)"))
        net = round(int(row[3].replace(",", "")) / 1e8, 1)
        d = j.get("date", "")
        d = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d
        if d and all(x["date"] != d for x in prev_list):
            prev_list.append({"date": d, "net_yi": net})
    except Exception as e:
        print("外資資料失敗:", e)
    return prev_list[-20:]


def main():
    prev = {}
    if os.path.exists(OUT):
        prev = json.load(open(OUT, encoding="utf-8"))
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    data = {
        "updated": now.strftime("%Y-%m-%d %H:%M"),
        "items": [analyse(*s) for s in SYMBOLS],
        "foreign": foreign_flow(prev.get("foreign", [])),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(data, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print("完成", data["updated"])


if __name__ == "__main__":
    main()
