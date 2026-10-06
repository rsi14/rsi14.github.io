"""미장(S&P500) RSI 과매도 스캔 → site/data/us/ (국장 로직 재사용)"""
import csv
import io
import os

import requests
import yfinance as yf

import rsi_report as K

os.environ.setdefault("BREADTH_MIN_N", "200")
K.MIN_PRICE, K.MIN_TV = 5, 0.1  # $5 미만, 20일 평균 거래대금 $1천만 미만 제외
OUT = "site/data/us"
SRC = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"


def main():
    uni = {r["Symbol"].replace(".", "-"): (r["Security"], r.get("GICS Sector", ""))
           for r in csv.DictReader(io.StringIO(requests.get(SRC, timeout=30).text))}
    df = yf.download(list(uni), period="2y", interval="1d", auto_adjust=False,
                     group_by="ticker", threads=True, progress=False)
    rows_map = {}
    for s in uni:
        try:
            d = df[s].dropna(subset=["Close"])
        except KeyError:
            continue
        rows_map[s] = [(i.strftime("%Y%m%d"), float(c), float(v)) for i, c, v in zip(d.index, d["Close"], d["Volume"])]
    K.get_daily = lambda code, count=None: rows_map[code]
    results = [r for r in (K.scan_one(uni[s][1], s, uni[s][0]) for s in rows_map if len(rows_map[s]) > 60) if r]
    print(f"미장 종목 {len(uni)} / 데이터 {len(results)}")

    base = max(r["date"] for r in results)
    cur = [r for r in results if r["date"] == base]
    breadth = K.market_breadth(results)
    tb = breadth.get(base)
    hits = [r for r in cur if r["rsi"] <= K.RSI_THRESHOLD and not r["halted"]]
    tracking = K.update_tracking(results, breadth, site_dir=OUT)
    try:  # 원/달러 환율 (시총 원화 환산)
        fx = float(yf.Ticker("KRW=X").history(period="5d")["Close"].dropna().iloc[-1])
    except Exception:
        fx = None
    for h in hits:
        h["tv20"] = round(h["tv20"] * 100)  # $M
        h["grade"], h["flags"], h["lg"] = ("양호" if h["tv20"] >= 300 else "보통"), [], False
        try:  # 시총(억원)·PER·PBR
            info = yf.Ticker(h["code"]).info
            h["mcap"] = round(info["marketCap"] * fx / 1e8) if info.get("marketCap") and fx else None
            h["per"] = round(info["trailingPE"], 1) if info.get("trailingPE") else None
            h["pbr"] = round(info["priceToBook"], 2) if info.get("priceToBook") else None
        except Exception:
            pass
        cb = breadth.get(h.get("cross"))
        h["cb"] = round(cb, 1) if cb is not None else None
        h["type"] = K.drop_type(cb)
        past = [s for s in tracking if s["code"] == h["code"] and len(s["p"]) >= 5]
        if past:
            l20 = [s["p"][19] for s in past if len(s["p"]) >= 20]
            h["hist"] = {"n": len(past), "d5": round(sum(s["p"][4] for s in past) / len(past), 1),
                         "d20": round(sum(l20) / len(l20), 1) if l20 else None}
    dates = sorted(breadth)[-120:]
    K.save_site_data(hits, base, len(cur), len(uni) - len(results), extra={
        "breadth": round(tb, 1) if tb is not None else None,
        "bhist": [[d, round(breadth[d], 1)] for d in dates],
        "panic": K.PANIC, "partial": K.PARTIAL, "brsi": K.BREADTH_RSI, "raw": len(hits), "bt": None,
    }, site_dir=OUT)
    print(f"미장 기준일 {base}, 과매도 비율 {tb}, RSI {K.RSI_THRESHOLD:g} 이하 {len(hits)}종목")


if __name__ == "__main__":
    main()
