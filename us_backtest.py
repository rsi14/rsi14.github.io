"""
미장(S&P500) RSI 계산식 비교 백테스트 → site/data/us/bt_compare.json
- 비교: 커틀러(단순평균) RSI 10/15/20, 와일더 RSI 20/25
- 신호: RSI(14)가 기준값을 위에서 아래로 뚫은 날
- 매수: 다음 거래일 시가 / 비용 0.1% 차감 / 수익률 ±60% 컷
- 매도: 5/20/60거래일 보유, RSI 50 회복 시(같은 계산식, 최대 20거래일)
- 시장 과매도 비율: 와일더 RSI(14) 30 이하 종목 비율 (두 계산식 공통 기준)
- 한계: 현재 S&P500 구성 종목만 사용 (생존 편향)
"""
import csv
import io
import json
import os
from collections import defaultdict

import requests
import yfinance as yf

SRC = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
COST = 0.1
RULES = {"C10": ("c", 10), "C15": ("c", 15), "C20": ("c", 20), "W20": ("w", 20), "W25": ("w", 25)}
HOLD = (5, 20, 60)
MAXX = 20


def wilder(closes, p=14):
    out = [None] * len(closes)
    if len(closes) <= p:
        return out
    g = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    l = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(g[:p]) / p, sum(l[:p]) / p
    v = lambda a, b: 100.0 if b == 0 else 100 - 100 / (1 + a / b)
    out[p] = v(ag, al)
    for i in range(p, len(g)):
        ag = (ag * (p - 1) + g[i]) / p
        al = (al * (p - 1) + l[i]) / p
        out[i + 1] = v(ag, al)
    return out


def cutler(closes, p=14):
    out = [None] * len(closes)
    d = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    for i in range(p, len(closes)):
        w = d[i - p:i]
        gs = sum(x for x in w if x > 0)
        ls = sum(-x for x in w if x < 0)
        out[i] = 50.0 if gs + ls == 0 else 100 * gs / (gs + ls)
    return out


def bucket(b):
    return "동반" if b >= 25 else "부분" if b >= 10 else "단독"


def main():
    syms = [r["Symbol"].replace(".", "-") for r in csv.DictReader(io.StringIO(requests.get(SRC, timeout=30).text))]
    df = yf.download(syms, period=os.getenv("US_BT_PERIOD", "10y"), interval="1d", auto_adjust=True,
                     group_by="ticker", threads=True, progress=False)
    data = {}
    for s in syms:
        try:
            d = df[s].dropna(subset=["Close", "Open"])
        except KeyError:
            continue
        if len(d) < 300:
            continue
        dates = [i.strftime("%Y%m%d") for i in d.index]
        o, c, v = list(map(float, d["Open"])), list(map(float, d["Close"])), list(map(float, d["Volume"]))
        data[s] = {"d": dates, "o": o, "c": c, "v": v, "w": wilder(c), "cu": cutler(c)}
    print("종목", len(data))

    cnt = defaultdict(lambda: [0, 0])
    for x in data.values():
        for dt, r in zip(x["d"], x["w"]):
            if r is not None:
                cnt[dt][1] += 1
                cnt[dt][0] += r <= 30
    breadth = {dt: a / b * 100 for dt, (a, b) in cnt.items() if b >= 200}

    agg = defaultdict(lambda: {"n": 0, **{f"s{k}": 0.0 for k in (*HOLD, "x")},
                               **{f"w{k}": 0 for k in (*HOLD, "x")}, **{f"c{k}": 0 for k in (*HOLD, "x")}, "dx": 0})
    months = sorted({dt[:6] for dt in breadth})

    for x in data.values():
        d, o, c, v = x["d"], x["o"], x["c"], x["v"]
        for name, (kind, th) in RULES.items():
            rs = x["cu"] if kind == "c" else x["w"]
            for i in range(15, len(c) - 1):
                r, pr = rs[i], rs[i - 1]
                if r is None or pr is None or not (r <= th < pr):
                    continue
                if c[i] < 5 or d[i] not in breadth:
                    continue
                tv = sum(c[k] * v[k] for k in range(max(0, i - 19), i + 1)) / min(20, i + 1)
                if tv < 1e7:
                    continue
                entry = o[i + 1]
                if entry <= 0:
                    continue
                ret = lambda j: max(-60.0, min(60.0, (c[j] / entry - 1) * 100 - COST))
                keys = [(name, "all", "all"), (name, "y", d[i][:4]), (name, "b", bucket(breadth[d[i]]))]
                res = {}
                for k in HOLD:
                    if i + k < len(c):
                        res[k] = ret(i + k)
                if i + MAXX < len(c):
                    j = next((i + k for k in range(1, MAXX + 1) if (rs[i + k] or 0) >= 50), i + MAXX)
                    res["x"] = ret(j)
                    res["dx"] = j - i
                for key in keys:
                    a = agg[key]
                    a["n"] += 1
                    for k in (*HOLD, "x"):
                        if k in res:
                            a[f"s{k}"] += res[k]
                            a[f"w{k}"] += res[k] > 0
                            a[f"c{k}"] += 1
                    if "dx" in res:
                        a["dx"] += res["dx"]

    out = {"from": min(breadth), "to": max(breadth), "months": len(months), "stocks": len(data), "cost": COST, "rows": []}
    for (rule, dim, val), a in sorted(agg.items()):
        row = {"rule": rule, "dim": dim, "val": val, "n": a["n"], "per_month": round(a["n"] / len(months), 1)}
        for k in (*HOLD, "x"):
            if a[f"c{k}"]:
                row[f"a{k}"] = round(a[f"s{k}"] / a[f"c{k}"], 2)
                row[f"w{k}"] = round(a[f"w{k}"] / a[f"c{k}"] * 100)
        if a["cx"]:
            row["dx"] = round(a["dx"] / a["cx"], 1)
        out["rows"].append(row)
    os.makedirs("site/data/us", exist_ok=True)
    with open("site/data/us/bt_compare.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=0)
    for row in out["rows"]:
        if row["dim"] == "all":
            print(row)


if __name__ == "__main__":
    main()
