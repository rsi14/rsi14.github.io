"""
백테스트 v2: 과매도 신호를 건별로 저장 (잡주 제외)
- 신호: RSI(14)가 15/20/25/30 중 하나를 위에서 아래로 뚫은 날
- 매수: 다음 거래일 시가 / 수익률은 비용 0.3% 차감, ±60% 컷
- 결과: site/data/bt2.json
  cols: d 날짜, m 시장(1=코스피), r RSI, pr 전날 RSI, tv 20일 평균 거래대금(억), b 시장 과매도 비율(%), up 200일선 위(1)
        h1 h3 h5 h10 h20 h60 보유 N거래일 뒤 종가 수익률
        x50 RSI 50 회복 시 매도(최대 20일), x50d 보유일
        s7 -7% 손절(종가 기준) 아니면 10일 뒤 매도
        x50s7 RSI 50 회복 or -7% 손절, 최대 20일
"""
import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from backtest import get_ohlcv
from rsi_report import list_stocks, rsi_series

COST = 0.3
TH = (15, 20, 25, 30)
HOLD = (1, 3, 5, 10, 20, 60)
MIN_PRICE, MIN_TV = 1000, 10


def load(u):
    market, code, name = u
    try:
        rows = get_ohlcv(code)
    except Exception:
        return None
    if len(rows) < 260:
        return None
    closes = [c for _, _, c, _ in rows]
    return {"m": market, "rows": rows, "r14": rsi_series(closes, 14)}


def clip(x):
    return round(max(-60.0, min(60.0, x)), 2)


def main():
    universe = [("KOSPI", c, n) for c, n in list_stocks("KOSPI")]
    universe += [("KOSDAQ", c, n) for c, n in list_stocks("KOSDAQ")]
    with ThreadPoolExecutor(max_workers=12) as ex:
        data = [d for d in ex.map(load, universe) if d]
    print(f"종목 {len(universe)} / 데이터 {len(data)}")

    cnt = defaultdict(lambda: [0, 0])
    for d in data:
        for (dt, *_), r in zip(d["rows"], d["r14"]):
            if r is not None:
                cnt[dt][1] += 1
                cnt[dt][0] += r <= 30
    breadth = {dt: a / b * 100 for dt, (a, b) in cnt.items() if b >= int(os.getenv("BT_MIN_N", "300"))}

    out = []
    for d in data:
        rows, rs, n = d["rows"], d["r14"], len(d["rows"])
        closes = [c for _, _, c, _ in rows]
        tvs = [c * v / 1e8 for _, _, c, v in rows]
        for i in range(200, n - 1):
            r, prev = rs[i], rs[i - 1]
            if r is None or prev is None or not any(r <= th < prev for th in TH):
                continue
            dt, _, c, _ = rows[i]
            tv = sum(tvs[i - 19:i + 1]) / 20
            if c < MIN_PRICE or tv < MIN_TV or closes[i] / closes[i - 1] - 1 <= -0.305 or dt not in breadth:
                continue
            entry = rows[i + 1][1]
            if entry <= 0:
                continue
            ret = lambda j: (closes[j] / entry - 1) * 100 - COST
            row = {"d": dt, "m": 1 if d["m"] == "KOSPI" else 0, "r": round(r, 1), "pr": round(prev, 1),
                   "tv": round(tv), "b": round(breadth[dt], 1),
                   "up": 1 if c >= sum(closes[i - 199:i + 1]) / 200 else 0}
            for h in HOLD:
                row[f"h{h}"] = clip(ret(i + h)) if i + h < n else None
            # RSI 50 회복 (최대 20일)
            row["x50"] = row["x50d"] = None
            for k in range(1, 21):
                j = i + k
                if j >= n:
                    break
                if (rs[j] or 0) >= 50 or k == 20:
                    row["x50"], row["x50d"] = clip(ret(j)), k
                    break
            # -7% 손절 or 10일 보유
            row["s7"] = None
            for k in range(1, 11):
                j = i + k
                if j >= n:
                    break
                if ret(j) <= -7 or k == 10:
                    row["s7"] = clip(ret(j))
                    break
            # RSI 50 회복 or -7% 손절 (최대 20일)
            row["x50s7"] = None
            for k in range(1, 21):
                j = i + k
                if j >= n:
                    break
                if (rs[j] or 0) >= 50 or ret(j) <= -7 or k == 20:
                    row["x50s7"] = clip(ret(j))
                    break
            out.append(row)

    cols = ["d", "m", "r", "pr", "tv", "b", "up"] + [f"h{h}" for h in HOLD] + ["x50", "x50d", "s7", "x50s7"]
    os.makedirs("site/data", exist_ok=True)
    with open("site/data/bt2.json", "w", encoding="utf-8") as f:
        json.dump({"cols": cols, "rows": [[x[c] for c in cols] for x in out],
                   "days": sorted(breadth), "cost": COST}, f, separators=(",", ":"))
    print(f"신호 {len(out)}건, 거래일 {len(breadth)}일")


if __name__ == "__main__":
    main()
