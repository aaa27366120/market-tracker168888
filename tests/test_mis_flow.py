"""MIS 盤中指數：首頁（取得 cookie）失敗時仍要呼叫 API，並使用 MIS 自帶的昨收。

背景：2026-11-16 的資料檔顯示 https://mis.twse.com.tw/stock/index.jsp 回 404，
舊流程因此放棄整個 MIS 路徑；但同一時間 API 本身在不帶 cookie 時可正常回傳。
"""
import datetime as dt
import json
import unittest
from unittest.mock import MagicMock, patch

import requests

import fetch_data
from fetch_data import parse_twse_mis_intraday, parse_twse_mis_previous_close, resolve_previous_close

# 2026-11-16 10:26 從 getStockInfo.jsp?ex_ch=tse_t00.tw 取得的實際回應（原樣保留）。
REAL_PAYLOAD = json.loads(
    '{"msgArray":[{"@":"t00.tw","pid":"2.tse.tw|1033","key":"tse_t00.tw_20261116","^":"20261116",'
    '"c":"t00","#":"2.tse.tw|1033","d":"20261116","%":"10:26:00","ch":"t00.tw","tlong":"1794795960000",'
    '"h":"43512.42","i":"tidx.tw","it":"t","l":"42159.98","m":"6730180","n":"發行量加權股價指數",'
    '"o":"43512.42","r":"1614272","ex":"tse","t":"10:26:00","y":"43675.12","z":"42344.03"}],'
    '"referer":"","userDelay":5000,"rtcode":"0000","queryTime":{"sysDate":"20261116","stockInfoItem":19,'
    '"stockInfo":110868,"sessionStr":"UserSession","sysTime":"10:26:01","showChart":false,'
    '"sessionFromTime":-1,"sessionLatestTime":-1},"rtmessage":"OK","exKey":"if_tse_t00.tw_zh-tw.null",'
    '"cachedAlive":110533}'
)
TPE = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 11, 16, 10, 26, 30, tzinfo=TPE)


def payload(**row):
    base = {"c": "t00", "d": "20261116", "t": "10:26:00", "z": "42344.03", "y": "43675.12"}
    base.update(row)
    return {"rtcode": "0000", "msgArray": [base]}


class PreviousCloseParserTests(unittest.TestCase):
    def test_real_payload_price_and_previous_close(self):
        quote = parse_twse_mis_intraday(REAL_PAYLOAD, "2026-11-16")
        self.assertEqual(quote, {"date": "2026-11-16", "time": "10:26:00", "price": 42344.03})
        self.assertEqual(parse_twse_mis_previous_close(REAL_PAYLOAD, quote["price"]), 43675.12)

    def test_previous_close_is_none_when_missing_or_invalid(self):
        for bad in (None, "", "-", "0", "abc"):
            row = {"y": bad} if bad is not None else {}
            p = payload(**row)
            if bad is None:
                del p["msgArray"][0]["y"]
            self.assertIsNone(parse_twse_mis_previous_close(p, 42344.03), f"y={bad!r}")

    def test_absurd_previous_close_is_ignored(self):
        # 與現價相差超過 20% 視為不可信（台股單日漲跌幅上限為 10%）
        self.assertIsNone(parse_twse_mis_previous_close(payload(y="10000"), 42344.03))
        self.assertIsNone(parse_twse_mis_previous_close(payload(y="90000"), 42344.03))
        self.assertEqual(parse_twse_mis_previous_close(payload(y="38500"), 42344.03), 38500.0)

    def test_empty_msgarray_is_none(self):
        self.assertIsNone(parse_twse_mis_previous_close({"msgArray": []}, 42344.03))


class FetchFlowTests(unittest.TestCase):
    def response(self, body=None, text="", status=200):
        r = MagicMock()
        r.status_code = status
        r.headers = {"Content-Type": "application/json"}
        r.content = text.encode("utf-8") if text else json.dumps(body or {}).encode("utf-8")
        r.encoding = "utf-8"
        r.text = text
        r.json.return_value = body if body is not None else {}
        r.raise_for_status.return_value = None
        return r

    def landing(self, status=200):
        r = MagicMock()
        r.status_code = status
        r.headers = {"Content-Type": "text/html"}
        r.content = b"<html></html>"
        r.encoding = "utf-8"
        if status >= 400:
            r.raise_for_status.side_effect = requests.HTTPError(
                f"{status} Client Error:  for url: https://mis.twse.com.tw/stock/index.jsp")
        else:
            r.raise_for_status.return_value = None
        return r

    def run_fetch(self, session, csv=None, retry_stub=None):
        retry_patch = patch("fetch_data.retry", side_effect=retry_stub or (lambda fn, *a, **kw: fn(*a, **kw)))
        csv_patch = patch("fetch_data.requests.get", return_value=csv) if csv is not None \
            else patch("fetch_data.requests.get")
        with patch("fetch_data.requests.Session", return_value=session), retry_patch, csv_patch as csv_get:
            return fetch_data.fetch_twse_intraday(NOW), csv_get

    def test_landing_404_still_uses_mis_api(self):
        session = MagicMock()
        session.get.side_effect = [self.landing(404), self.response(REAL_PAYLOAD)]
        result, csv_get = self.run_fetch(session)
        self.assertEqual(result["source"], "證交所 MIS 即時指數")
        self.assertEqual(result["price"], 42344.03)
        self.assertEqual(result["time"], "10:26:00")
        self.assertEqual(result["previous_close"], 43675.12)
        csv_get.assert_not_called()
        self.assertEqual(session.get.call_count, 2)  # 首頁 + API，兩個都有呼叫
        session.close.assert_called_once()

    def test_landing_connection_error_still_uses_mis_api(self):
        calls = []

        def retry_stub(fn, *args, **kwargs):
            calls.append(args[0])
            if len(calls) == 1:  # 第一個呼叫是首頁：連線失敗
                raise requests.ConnectionError("landing unreachable")
            return fn(*args, **kwargs)

        session = MagicMock()
        session.get.side_effect = [self.response(REAL_PAYLOAD)]
        result, _ = self.run_fetch(session, retry_stub=retry_stub)
        self.assertEqual(result["source"], "證交所 MIS 即時指數")
        self.assertEqual(result["price"], 42344.03)
        self.assertEqual(len(calls), 2)

    def test_landing_ok_still_works_as_before(self):
        session = MagicMock()
        session.get.side_effect = [self.landing(200), self.response(REAL_PAYLOAD)]
        result, csv_get = self.run_fetch(session)
        self.assertEqual(result["source"], "證交所 MIS 即時指數")
        csv_get.assert_not_called()

    def test_result_has_no_previous_close_when_y_missing(self):
        p = payload()
        del p["msgArray"][0]["y"]
        session = MagicMock()
        session.get.side_effect = [self.landing(200), self.response(p)]
        result, _ = self.run_fetch(session)
        self.assertEqual(result["price"], 42344.03)
        self.assertNotIn("previous_close", result)

    def test_landing_404_and_api_empty_falls_back_to_csv(self):
        session = MagicMock()
        session.get.side_effect = [self.landing(404), self.response({"rtcode": "0001", "msgArray": []})]
        csv = self.response(text='時間,發行量加權股價指數\n=10:26:00,"42,344.03"\n')
        result, _ = self.run_fetch(session, csv=csv)
        self.assertEqual(result["source"], "證交所 MI_5MINS_INDEX（備援）")
        self.assertNotIn("previous_close", result)

    def test_all_failures_report_landing_note_and_both_sources(self):
        session = MagicMock()
        session.get.side_effect = [self.landing(404), self.response({"rtcode": "0001", "msgArray": []})]
        csv = self.response(text="invalid csv")
        with self.assertRaises(RuntimeError) as caught:
            self.run_fetch(session, csv=csv)
        message = str(caught.exception)
        self.assertIn("MIS：", message)
        self.assertIn("MIS 首頁失敗", message)  # 診斷要看得到首頁出了什麼事
        self.assertIn("404", message)
        self.assertIn("CSV：", message)

    def test_stale_date_message_survives_landing_note(self):
        # main() 以「MIS 指數日期不符」判斷休市日，訊息加上首頁備註後仍必須包含它
        session = MagicMock()
        session.get.side_effect = [self.landing(404), self.response(payload(d="20261113"))]
        csv = self.response(text="invalid csv")
        with self.assertRaises(RuntimeError) as caught:
            self.run_fetch(session, csv=csv)
        self.assertIn("MIS 指數日期不符", str(caught.exception))


class ResolvePreviousCloseTests(unittest.TestCase):
    TODAY = "2026-11-16"

    def test_mis_previous_close_has_priority(self):
        live = {"price": 42344.03, "previous_close": 43675.12}
        turnover = {"date": self.TODAY, "previous_index_close": 1.0}
        old = {"date": "2026-11-13", "close": 2.0}
        item = {"date": "2026-11-13", "close": 3.0}
        self.assertEqual(resolve_previous_close(live, turnover, old, item, self.TODAY), 43675.12)

    def test_monday_morning_without_mis_previous_close_is_none(self):
        # 這就是修正前的實際情況：Yahoo 盤中日線已帶今天的日期，證交所成交資料還是上週五，
        # 所有舊來源都不適用 → 漲跌幅算不出來。
        live = {"price": 42344.03}
        turnover = {"date": "2026-11-13", "previous_index_close": 43091.87}
        old = {"date": self.TODAY, "close": 42270.36}
        item = {"date": self.TODAY, "close": 42270.36}
        self.assertIsNone(resolve_previous_close(live, turnover, old, item, self.TODAY))

    def test_monday_morning_with_mis_previous_close_gives_change(self):
        live = {"price": 42344.03, "previous_close": 43675.12}
        value = resolve_previous_close(live, None, None, {"date": self.TODAY, "close": 42270.36}, self.TODAY)
        self.assertEqual(value, 43675.12)
        self.assertAlmostEqual((live["price"] / value - 1) * 100, -3.05, places=2)

    def test_falls_back_to_turnover_then_old_then_item(self):
        live = {"price": 1.0}
        turnover = {"date": self.TODAY, "previous_index_close": 100.0}
        self.assertEqual(resolve_previous_close(live, turnover, None, {"date": self.TODAY}, self.TODAY), 100.0)
        old = {"date": "2026-11-13", "close": 200.0}
        self.assertEqual(resolve_previous_close(live, None, old, {"date": self.TODAY}, self.TODAY), 200.0)
        item = {"date": "2026-11-13", "close": 300.0}
        self.assertEqual(resolve_previous_close(live, None, None, item, self.TODAY), 300.0)


if __name__ == "__main__":
    unittest.main()
