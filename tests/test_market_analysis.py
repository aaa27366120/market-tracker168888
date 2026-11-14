"""Tests for the phase-3 market analysis: only TWII, SOX and GSPC are analysed and scored."""
import random
import unittest

import fetch_data
from fetch_data import MARKET_ANALYSIS_SYMBOLS, build_market_analysis


def make_item(sym, name, close, ma20, ma60, rsi14, hist, mom, chg=0.0, **extra):
    """Minimal item shaped like one entry of data.json['items'] (values from 2026-11-13)."""
    item = {
        "sym": sym, "name": name, "date": "2026-11-13", "close": close, "chg": chg,
        "stale": False, "source": "Yahoo Finance", "captured_open": False,
        "technical": {
            "status": "success",
            "daily": {"trend": "空頭"}, "weekly": {"trend": "轉弱觀察"}, "monthly": {"trend": "轉弱觀察"},
            "rsi14": rsi14, "ma20": ma20, "ma60": ma60, "momentum20": mom,
            "macd": {"histogram": hist,
                     "direction": "偏多" if hist > 0 else "偏空" if hist < 0 else "中性"},
        },
    }
    item.update(extra)
    return item


def real_items():
    """All eight symbols exactly as they appear in data.json on 2026-11-13."""
    return [
        make_item("^TWII", "台灣加權指數", 43675.12, 44634.4, 46215.19, 45.0, -64.814, -10.18, 1.35,
                  source="證交所官方收盤（FMTQIK）"),
        make_item("^GSPC", "標普 500", 7561.67, 7613.26, 7670.46, 46.8, -0.826, -1.28, -0.4),
        make_item("^IXIC", "那斯達克指數", 25136.27, 25689.82, 26336.03, 42.4, -15.201, -3.84, -0.91),
        make_item("^DJI", "道瓊工業指數", 52347.55, 52270.83, 52204.32, 51.9, 15.156, 0.66, 0.1),
        make_item("^SOX", "費城半導體", 10146.32, 10646.11, 11570.46, 42.2, 16.051, -8.9, -3.43),
        make_item("GC=F", "黃金期貨", 3887.6, 4035.27, 4247.16, 36.1, -5.216, -7.02, 0.06),
        make_item("CL=F", "WTI 原油期貨", 88.92, 95.75, 93.84, 42.3, -1.266, -12.32, 0.2),
        make_item("TWD=X", "美元兌台幣", 32.88, 32.65, 32.08, 62.4, 0.004, 1.7, 0.04),
    ]


def scores_of(result):
    return {x["sym"]: x["score"] for x in result["scored_indices"]}


class MarketAnalysisScopeTests(unittest.TestCase):
    def test_only_three_symbols_are_analysed_in_fixed_order(self):
        result = build_market_analysis(real_items())
        self.assertEqual([a["sym"] for a in result["assets"]], ["^TWII", "^SOX", "^GSPC"])
        self.assertEqual([x["sym"] for x in result["scored_indices"]], ["^TWII", "^SOX", "^GSPC"])
        self.assertEqual(MARKET_ANALYSIS_SYMBOLS, ("^TWII", "^SOX", "^GSPC"))

    def test_real_data_scores(self):
        result = build_market_analysis(real_items())
        # TWII: 25+20+0(RSI 45)+20+15 = 80；SOX: 25+20+0+0(MACD 偏多)+15 = 60；GSPC: 25+20+0+20+15 = 80
        self.assertEqual(scores_of(result), {"^TWII": 80, "^SOX": 60, "^GSPC": 80})
        self.assertEqual(result["risk_score"], 73)  # (80+60+80)/3 = 73.3
        self.assertEqual(result["risk_level"], "警戒")
        self.assertEqual(result["coverage"], 3)
        self.assertEqual(result["coverage_total"], 3)
        self.assertIn("三個指數", result["method"])
        self.assertNotIn("四個", result["method"])

    def test_item_order_does_not_change_output_order(self):
        items = real_items()
        random.Random(7).shuffle(items)
        result = build_market_analysis(items)
        self.assertEqual([a["sym"] for a in result["assets"]], ["^TWII", "^SOX", "^GSPC"])

    def test_dropped_symbols_still_exist_for_top_cards(self):
        # 本次只縮減市場分析；上方行情卡片仍抓取原本的八個標的。
        fetched = {s for s, _, _ in fetch_data.SYMBOLS}
        self.assertTrue({"^IXIC", "^DJI", "GC=F", "CL=F", "TWD=X"} <= fetched)
        analysed = {a["sym"] for a in build_market_analysis(real_items())["assets"]}
        self.assertTrue(analysed.isdisjoint({"^IXIC", "^DJI", "GC=F", "CL=F", "TWD=X"}))


class MarketAnalysisValidityTests(unittest.TestCase):
    def test_stale_index_is_excluded_from_score_but_still_listed(self):
        items = real_items()
        sox = next(x for x in items if x["sym"] == "^SOX")
        sox["stale"] = True
        result = build_market_analysis(items)
        self.assertEqual(scores_of(result), {"^TWII": 80, "^GSPC": 80})
        self.assertEqual(result["risk_score"], 80)
        self.assertEqual(result["coverage"], 2)
        self.assertEqual(result["coverage_total"], 3)
        listed = {a["sym"]: a for a in result["assets"]}
        self.assertTrue(listed["^SOX"]["stale"])

    def test_technical_stale_index_is_excluded(self):
        items = real_items()
        next(x for x in items if x["sym"] == "^TWII")["technical_stale"] = True
        result = build_market_analysis(items)
        self.assertNotIn("^TWII", scores_of(result))
        self.assertEqual(result["coverage"], 2)

    def test_only_one_valid_index_gives_no_score(self):
        items = real_items()
        for x in items:
            if x["sym"] in ("^SOX", "^GSPC"):
                x["stale"] = True
        result = build_market_analysis(items)
        self.assertIsNone(result["risk_score"])
        self.assertEqual(result["risk_level"], "資料不足")
        self.assertEqual(result["coverage"], 1)

    def test_missing_symbol_does_not_raise(self):
        items = [x for x in real_items() if x["sym"] != "^SOX"]
        result = build_market_analysis(items)
        self.assertEqual([a["sym"] for a in result["assets"]], ["^TWII", "^GSPC"])
        self.assertEqual(result["coverage"], 2)
        self.assertEqual(result["risk_score"], 80)

    def test_failed_item_without_technical_is_skipped(self):
        items = real_items()
        items[0] = {"sym": "^TWII", "name": "台灣加權指數", "error": "network down", "stale": True}
        result = build_market_analysis(items)
        self.assertNotIn("^TWII", [a["sym"] for a in result["assets"]])
        self.assertEqual(result["coverage"], 2)

    def test_empty_items(self):
        result = build_market_analysis([])
        self.assertIsNone(result["risk_score"])
        self.assertEqual(result["assets"], [])
        self.assertEqual(result["coverage"], 0)
        self.assertEqual(result["coverage_total"], 3)


class MarketAnalysisRuleBoundaryTests(unittest.TestCase):
    def test_boundary_values_do_not_light_any_rule(self):
        # close == MA60、MA20 == MA60、RSI == 40、MACD 柱狀體 == 0、動能 == 0 → 都是「未亮」
        items = [make_item(s, s, 100.0, 100.0, 100.0, 40.0, 0.0, 0.0) for s in MARKET_ANALYSIS_SYMBOLS]
        result = build_market_analysis(items)
        self.assertEqual(set(scores_of(result).values()), {0})
        self.assertEqual(result["risk_score"], 0)
        self.assertEqual(result["risk_level"], "一般")

    def test_each_rule_adds_its_own_points(self):
        base = dict(close=100.0, ma20=100.0, ma60=100.0, rsi14=50.0, hist=1.0, mom=1.0)

        def score(**override):
            args = {**base, **override}
            items = [make_item(s, s, args["close"], args["ma20"], args["ma60"],
                               args["rsi14"], args["hist"], args["mom"]) for s in MARKET_ANALYSIS_SYMBOLS]
            return build_market_analysis(items)["risk_score"]

        self.assertEqual(score(), 0)
        self.assertEqual(score(close=99.0), 25)          # 收盤低於 MA60
        self.assertEqual(score(ma20=99.0), 20)           # MA20 低於 MA60
        self.assertEqual(score(rsi14=39.9), 20)          # RSI < 40
        self.assertEqual(score(hist=-0.1), 20)           # MACD 柱狀體 < 0
        self.assertEqual(score(mom=-0.1), 15)            # 20 日動能 < 0
        self.assertEqual(score(close=99.0, ma20=99.0, rsi14=30.0, hist=-1.0, mom=-1.0), 100)

    def test_missing_indicator_values_do_not_add_points(self):
        item = make_item("^TWII", "台灣加權指數", 100.0, None, None, None, 1.0, None)
        item["technical"]["macd"] = {"histogram": None, "direction": None}
        others = [make_item(s, s, 100.0, 100.0, 100.0, 50.0, 1.0, 1.0) for s in ("^SOX", "^GSPC")]
        result = build_market_analysis([item] + others)
        self.assertEqual(scores_of(result)["^TWII"], 0)


if __name__ == "__main__":
    unittest.main()
