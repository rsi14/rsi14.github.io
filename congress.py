"""미 하원의원 주식거래 공시(PTR) 수집 → site/data/congress/
- 목록: 하원 서기국 연도별 공시 XML / 원문: 전자 제출 PTR PDF (스캔본은 건너뜀)
- 매일 새 공시만 읽고, 최근 거래의 거래일·공시일 이후 수익률 계산
"""
import io
import json
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pdfplumber
import requests

BASE = "https://disclosures-clerk.house.gov/public_disc"
OUT = "site/data/congress"
S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (rsi14.github.io; personal research)"
ROW = re.compile(
    r"(?:\b(?P<own>SP|JT|DC)\s+)?(?P<name>[^\[\]$]{0,120}?)\((?P<tk>[A-Z][A-Z0-9.\-]{0,6})\)\s*\[(?P<at>[A-Z]{2})\]\s*"
    r"(?P<tt>P|S \(partial\)|S|E)\s+(?P<d1>\d{2}/\d{2}/\d{4})\s+(?P<d2>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amt>\$[\d,]+\s*-\s*\$[\d,]+|Over \$[\d,]+|\$[\d,]+)")


def filings(year):
    z = zipfile.ZipFile(io.BytesIO(S.get(f"{BASE}/financial-pdfs/{year}FD.zip", timeout=60).content))
    root = ET.fromstring(z.read(f"{year}FD.xml"))
    out = []
    for m in root.iter("Member"):
        g = lambda k: (m.findtext(k) or "").strip()
        if g("FilingType") == "P":
            out.append({"doc": g("DocID"), "y": year, "m": f"{g('First')} {g('Last')}".strip(),
                        "st": g("StateDst"), "fd": datetime.strptime(g("FilingDate"), "%m/%d/%Y").strftime("%Y%m%d")})
    return out


def parse(f):
    if not f["doc"].startswith("2"):  # 8·9로 시작 = 종이 제출 스캔본
        return f["doc"], {"scan": 1}
    try:
        pdf = pdfplumber.open(io.BytesIO(S.get(f"{BASE}/ptr-pdfs/{f['y']}/{f['doc']}.pdf", timeout=60).content))
        text = re.sub(r"\s+", " ", " ".join((p.extract_text() or "") for p in pdf.pages).replace("\x00", ""))
    except Exception as e:
        return f["doc"], {"err": repr(e)[:80]}
    rows = []
    for r in ROW.finditer(text):
        own, name = r["own"] or "", r["name"]
        m = re.search(r".*\b(SP|JT|DC)\s+(.*)$", name)
        if m:
            own, name = m.group(1), m.group(2)
        name = re.sub(r"^.*(?:\$200\?|F S: \w+|D: [^.]*\.)\s*", "", name).strip(" -")
        rows.append({"own": own, "name": name[-60:], "tk": r["tk"], "at": r["at"],
                     "tt": r["tt"][0], "td": datetime.strptime(r["d1"], "%m/%d/%Y").strftime("%Y%m%d"),
                     "amt": re.sub(r"\s*-\s*", "~", r["amt"])})
    return f["doc"], {"rows": rows}


def main():
    os.makedirs(OUT, exist_ok=True)
    cache_p = f"{OUT}/cache.json"
    cache = json.load(open(cache_p)) if os.path.exists(cache_p) else {}
    y = datetime.now().year
    fl = []
    for yr in (y - 1, y):
        try:
            fl += filings(yr)
        except Exception as e:
            print("목록 실패", yr, e)
    todo = [f for f in fl if f["doc"] not in cache]
    print(f"PTR {len(fl)}건, 새로 읽을 공시 {len(todo)}건")
    with ThreadPoolExecutor(max_workers=8) as ex:
        for doc, res in ex.map(parse, todo):
            cache[doc] = res
    json.dump(cache, open(cache_p, "w"), separators=(",", ":"))

    trades = []
    for f in fl:
        for r in cache.get(f["doc"], {}).get("rows", []):
            trades.append({**r, "m": f["m"], "st": f["st"], "fd": f["fd"], "doc": f["doc"], "y": f["y"]})
    trades.sort(key=lambda t: (t["fd"], t["td"]), reverse=True)
    json.dump(trades, open(f"{OUT}/trades.json", "w"), ensure_ascii=False, separators=(",", ":"))

    # 피드: 최근 공시 300건 + 수익률
    feed = [t for t in trades if t["at"] in ("ST", "OP")][:300]
    try:
        import yfinance as yf
        tks = sorted({t["tk"].replace(".", "-") for t in feed})
        start = (datetime.strptime(min(t["td"] for t in feed), "%Y%m%d") - timedelta(days=7)).strftime("%Y-%m-%d")
        px = yf.download(tks, start=start, auto_adjust=True, progress=False, threads=True)["Close"]
        for t in feed:
            s = px.get(t["tk"].replace(".", "-"))
            if s is None or s.dropna().empty:
                continue
            s = s.dropna()
            at = lambda d: s[s.index >= datetime.strptime(d, "%Y%m%d")]
            a, b = at(t["td"]), at(t["fd"])
            last = float(s.iloc[-1])
            if len(a):
                t["rt"] = round((last / float(a.iloc[0]) - 1) * 100, 1)
            if len(b):
                t["rf"] = round((last / float(b.iloc[0]) - 1) * 100, 1)
    except Exception as e:
        print("수익률 계산 실패", e)
    json.dump({"generated": datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M"), "n_trades": len(trades),
               "n_scan": sum(1 for v in cache.values() if v.get("scan")), "items": feed},
              open(f"{OUT}/feed.json", "w"), ensure_ascii=False, separators=(",", ":"))
    print(f"거래 {len(trades)}건, 피드 {len(feed)}건")


if __name__ == "__main__":
    main()
