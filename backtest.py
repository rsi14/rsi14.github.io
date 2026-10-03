"""
과매도 신호 백테스트 (약 6년, 코스피·코스닥 현재 상장 종목)
- 매수: 신호 다음 거래일 시가 / 수익률: 매수가 대비 종가, 세금·수수료 0.3% 차감
- 결과: site/data/backtest.json (조건 조합별 집계 큐브)
"""
import json
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from rsi_report import get_with_retry, list_stocks, rsi_series

DAYS = int(os.getenv("BT_DAYS", "1600"))
COST = 0.3  # %
HORIZONS = (1, 5, 20, 60)
RULES = {"r14_10": (14, 10), "r14_20": (14, 20), "r14_30": (14, 30), "r2_5": (2, 5)}


def get_ohlcv(code):
    r = get_with_retry(
        "https://fchart.stock.naver.com/sise.nhn",
        params={"symbol": code, "timeframe": "day", "count": DAYS, "requestType": 0},
    )
    rows = []
    for m in re.finditer(r'data="([^"]+)"', r.text):
        p = m.group(1).split("|")
        if len(p) >= 6:
            rows.append((p[0], float(p[1]), float(p[4]), float(p[5])))  # date, open, close, vol
    return rows


def liq_bucket(tv):
    return "a<10" if tv < 10 else "b10-50" if tv < 50 else "c50-200" if tv < 200 else "d200+"


def breadth_bucket(x):
    return "a<3" if x < 3 else "b3-10" if x < 10 else "c10-25" if x < 25 else "d25+"


def load(u):
    market, code, name = u
    try:
        rows = get_ohlcv(code)
    except Exception:
        return None
    if len(rows) < 260:
        return None
    closes = [c for _, _, c, _ in rows]
    return {"code": code, "market": market, "rows": rows,
            "r14": rsi_series(closes, 14), "r2": rsi_series(closes, 2)}


def main():
    universe = [("KOSPI", c, n) for c, n in list_stocks("KOSPI")]
    universe += [("KOSDAQ", c, n) for c, n in list_stocks("KOSDAQ")]
    with ThreadPoolExecutor(max_workers=12) as ex:
        data = [d for d in ex.map(load, universe) if d]
    print(f"종목 {len(universe)} / 데이터 {len(data)}")

    # 시장 폭: 날짜별 RSI(14) 30 이하 종목 비율(%)
    cnt = defaultdict(lambda: [0, 0])
    for d in data:
        for (dt, *_), r in zip(d["rows"], d["r14"]):
            if r is not None:
                cnt[dt][1] += 1
                cnt[dt][0] += r <= 30
    breadth = {dt: a / b * 100 for dt, (a, b) in cnt.items() if b >= int(os.getenv("BT_MIN_N", "300"))}

    cube = defaultdict(lambda: {"n": 0, **{f"s{h}": 0.0 for h in HORIZONS}, **{f"w{h}": 0 for h in HORIZONS},
                                **{f"c{h}": 0 for h in HORIZONS}, "sx": 0.0, "wx": 0, "cx": 0, "dx": 0})
    skipped = 0
    for d in data:
        rows, n = d["rows"], len(d["rows"])
        closes = [c for _, _, c, _ in rows]
        tvs = [c * v / 1e8 for _, _, c, v in rows]
        for rule, (per, th) in RULES.items():
            rs = d["r14"] if per == 14 else d["r2"]
            for i in range(200, n - 1):
                r, prev = rs[i], rs[i - 1]
                if r is None or prev is None or not (r <= th < prev):
                    continue
                dt, _, c, _ = rows[i]
                if c < 1000 or closes[i] / closes[i - 1] - 1 <= -0.305 or dt not in breadth:
                    skipped += 1
                    continue
                entry = rows[i + 1][1]
                if entry <= 0:
                    continue
                ma200 = sum(closes[i - 199:i + 1]) / 200
                key = (rule, d["market"], liq_bucket(sum(tvs[i - 19:i + 1]) / 20),
                       "up" if c >= ma200 else "down", breadth_bucket(breadth[dt]), dt[:4])
                cell = cube[key]
                cell["n"] += 1
                for h in HORIZONS:
                    j = i + h
                    if j < n:
                        ret = (closes[j] / entry - 1) * 100 - COST
                        cell[f"s{h}"] += max(-60, min(60, ret))  # 극단값 ±60% 컷
                        cell[f"w{h}"] += ret > 0
                        cell[f"c{h}"] += 1
                # 규칙 청산: RSI(14) 50 회복 시 종가 매도, 최대 20거래일
                for k in range(1, 21):
                    j = i + k
                    if j >= n:
                        break
                    if (d["r14"][j] or 0) >= 50 or k == 20:
                        ret = (closes[j] / entry - 1) * 100 - COST
                        cell["sx"] += max(-60, min(60, ret))
                        cell["wx"] += ret > 0
                        cell["cx"] += 1
                        cell["dx"] += k
                        break

    out = {
        "stocks": len(data), "skipped": skipped, "cost": COST,
        "from": min(r[0][0] for r in (d["rows"] for d in data)),
        "to": max(r[-1][0] for r in (d["rows"] for d in data)),
        "dims": ["rule", "market", "liq", "trend", "breadth", "year"],
        "cells": [{"k": list(k), **v} for k, v in cube.items()],
    }
    os.makedirs("site/data", exist_ok=True)
    with open("site/data/backtest.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    tot = sum(c["n"] for c in out["cells"])
    print(f"신호 {tot}건, 셀 {len(out['cells'])}개, 제외 {skipped}건, 기간 {out['from']}~{out['to']}")


if __name__ == "__main__":
    main()
