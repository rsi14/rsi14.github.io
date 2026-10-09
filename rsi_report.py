"""
국장(코스피·코스닥) 전 종목 RSI(14) 스캔 → RSI 임계값 이하 종목 리포트 전송
- 데이터: 네이버 금융 (종목 목록 + 일봉)
- 전송: 텔레그램 / 이메일(Gmail) 중 설정된 쪽으로 전송
"""
import os
import re
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

import requests

RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_THRESHOLD = float(os.getenv("RSI_THRESHOLD", "10"))
BREADTH_RSI = 30   # 시장 과매도 비율: RSI(14)가 이 값 이하인 종목 비율
PANIC, PARTIAL = 25, 10  # 비율(%) 기준: 25↑ 시장 동반, 10~25 부분 동반, 10↓ 단독 하락
KST = timezone(timedelta(hours=9))
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

session = requests.Session()
session.headers.update(HEADERS)


def get_with_retry(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = session.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def list_stocks(market):
    """market: KOSPI / KOSDAQ → [(code, name)]"""
    out, page = [], 1
    while True:
        r = get_with_retry(
            f"https://m.stock.naver.com/api/stocks/marketValue/{market}",
            params={"page": page, "pageSize": 100},
        )
        data = r.json()
        stocks = data.get("stocks", [])
        if not stocks:
            break
        for s in stocks:
            code, name = s.get("itemCode"), s.get("stockName")
            if s.get("stockEndType") not in (None, "stock"):  # ETF·ETN 등 제외
                continue
            if not code or code[-1] != "0" or "스팩" in (name or ""):  # 우선주·스팩 제외
                continue
            if code and name:
                out.append((code, name))
        total = data.get("totalCount", 0)
        if page * 100 >= total:
            break
        page += 1
    return out


def get_daily(code, count=320):
    """[(date, close, volume)] 오래된 순"""
    r = get_with_retry(
        "https://fchart.stock.naver.com/sise.nhn",
        params={"symbol": code, "timeframe": "day", "count": count, "requestType": 0},
    )
    rows = []
    for m in re.finditer(r'data="([^"]+)"', r.text):
        p = m.group(1).split("|")
        if len(p) >= 6:
            rows.append((p[0], float(p[4]), float(p[5])))
    return rows


def rsi_series(closes, period=14):
    """종가 리스트와 같은 길이의 RSI 리스트 (계산 불가 구간은 None)"""
    out = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    # 국내 증권사 앱과 같은 단순평균 RSI: 최근 period일 상승폭 합 / (상승폭 합 + 하락폭 합)
    for i in range(period, len(closes)):
        g = sum(gains[i - period:i])
        l = sum(losses[i - period:i])
        out[i] = 50.0 if g + l == 0 else 100 * g / (g + l)
    return out


def wilder_rsi(closes, period=14):
    return rsi_series(closes, period)[-1]


TRACK_DAYS = int(os.getenv("TRACK_DAYS", "60"))      # 신호 후 추적 거래일 수
BACKFILL_DAYS = int(os.getenv("BACKFILL_DAYS", "120"))  # 과거 신호 자동 탐색 범위(거래일)
WARMUP = 60  # RSI 안정화용 최소 데이터 길이
LIMIT_DOWN = -0.305  # 하루 -30% 초과 하락 = 가격제한폭 밖(정리매매 등)
MIN_PRICE, MIN_TV, MIN_MCAP = 1000, 10, 1000  # 잡주 제외: 주가(원), 20일 평균 거래대금(억), 시총(억)


def tv20_at(rows, i):
    """i일 기준 20일 평균 거래대금(억원)"""
    w = rows[max(0, i - 19):i + 1]
    return sum(c * v for _, c, v in w) / len(w) / 1e8


def find_signals(rows, rsis):
    """RSI가 임계값 위 → 이하로 '떨어진 날'을 신호로 보고, 다음날부터 종가 추이를 붙임"""
    sigs = []
    n = len(rows)
    for i in range(max(WARMUP, n - BACKFILL_DAYS), n):
        r, prev = rsis[i], rsis[i - 1]
        if r is None or prev is None or not (r <= RSI_THRESHOLD < prev):
            continue
        if rows[i - 1][1] and rows[i][1] / rows[i - 1][1] - 1 <= LIMIT_DOWN:  # 정리매매 등
            continue
        if rows[i][1] < MIN_PRICE or tv20_at(rows, i) < MIN_TV:  # 잡주 제외
            continue
        entry = rows[i][1]
        js = range(i + 1, min(n, i + 1 + TRACK_DAYS))
        sig = {"date": rows[i][0], "entry": entry, "rsi": round(r, 1),
               "p": [round((rows[j][1] / entry - 1) * 100, 2) for j in js]}
        if sig["p"]:
            sig["ld"], sig["lc"] = rows[js[-1]][0], rows[js[-1]][1]
        sigs.append(sig)
    return sigs


def scan_one(market, code, name):
    rows = get_daily(code)
    if len(rows) < RSI_PERIOD + 1:
        return None
    last_date, last_close, last_vol = rows[-1]
    rsis = rsi_series([c for _, c, _ in rows], RSI_PERIOD)
    signals = find_signals(rows, rsis)
    rsi = rsis[-1]
    halted = last_vol == 0  # 거래정지 등
    if rsi is None:
        return None
    prev_close = rows[-2][1]
    chg = (last_close / prev_close - 1) * 100 if prev_close else 0
    out = {
        "market": market, "code": code, "name": name, "date": last_date,
        "close": last_close, "chg": chg, "rsi": rsi, "halted": halted, "signals": signals,
        "since": rows[max(WARMUP, len(rows) - BACKFILL_DAYS)][0] if len(rows) > WARMUP else last_date,
        "tail": [(rows[k][0], rsis[k]) for k in range(max(0, len(rows) - 200), len(rows))],
    }
    out["tv20"] = round(tv20_at(rows, len(rows) - 1), 1)  # 20일 평균 거래대금(억원)
    out["lg"] = out["tv20"] >= 200 and any(  # 대형주 눌림: 최근 2거래일 내 RSI 기준값 아래로 진입
        rsis[k] is not None and rsis[k - 1] is not None and rsis[k] <= RSI_THRESHOLD < rsis[k - 1] for k in (len(rows) - 2, len(rows) - 1))
    if rsi <= RSI_THRESHOLD or out["lg"]:
        for k in range(len(rows) - 1, max(WARMUP, len(rows) - 60), -1):  # RSI 기준 아래로 들어온 날
            if rsis[k] is not None and rsis[k - 1] is not None and rsis[k] <= RSI_THRESHOLD < rsis[k - 1]:
                out["cross"] = rows[k][0]
                break
        tail = rows[-90:]
        rt = rsis[-90:]
        out["spark"] = [round(c) for _, c, _ in tail]
        out["low"] = [i for i, v in enumerate(rt) if v is not None and v <= RSI_THRESHOLD]
        hi52 = max(c for _, c, _ in rows[-250:])
        out["from_high"] = round((last_close / hi52 - 1) * 100, 1)
    return out


def parse_krw_eok(text):
    """'412조 3,213억' / '3,213억원' → 억원 단위 숫자"""
    if not text:
        return None
    t = str(text).replace(",", "").replace(" ", "")
    jo = re.search(r"([\d.]+)조", t)
    eok = re.search(r"([\d.]+)억", t)
    if not (jo or eok):
        return None
    return (float(jo.group(1)) * 10000 if jo else 0) + (float(eok.group(1)) if eok else 0)


def safe_json(url, params=None):
    try:
        return get_with_retry(url, params=params, tries=2).json()
    except Exception:
        return None


def enrich(hit):
    """해당 종목만 추가 조회: 시총·PER·PBR·위험 표시·최근 뉴스 (실패해도 리포트는 진행)"""
    code = hit["code"]
    info = safe_json(f"https://m.stock.naver.com/api/stock/{code}/integration") or {}
    for t in info.get("totalInfos", []) or []:
        key, val = str(t.get("code", "")), t.get("value")
        if key == "marketValue":
            hit["mcap_text"] = val
            hit["mcap"] = parse_krw_eok(val)
        elif key in ("per", "pbr"):
            try:
                hit[key] = float(str(val).replace(",", "").replace("배", ""))
            except Exception:
                pass
    basic = safe_json(f"https://m.stock.naver.com/api/stock/{code}/basic") or {}
    flags = []
    blob = str(basic)
    for word in ("관리종목", "투자경고", "투자위험", "투자주의", "거래정지", "정리매매", "불성실공시"):
        if word in blob:
            flags.append(word)
    if hit.get("halted") and "거래정지" not in flags:
        flags.append("거래정지")
    if hit["chg"] / 100 <= LIMIT_DOWN and "정리매매" not in flags:
        flags.append("정리매매 의심(하루 -30% 초과)")
    hit["flags"] = flags
    news = safe_json(f"https://m.stock.naver.com/api/news/stock/{code}", {"pageSize": 10, "page": 1})
    try:
        raw = news if isinstance(news, list) else (news or {}).get("items", [])
        cands = []
        for it in raw:
            cands.extend(it.get("items") or [] if isinstance(it, dict) and "items" in it else [it])
        titles = [re.sub(r"<[^>]+>", "", c.get("title") or c.get("titleFull") or "").strip()
                  for c in cands if isinstance(c, dict)]
        related = [t for t in titles if hit["name"] in t]  # 종목명이 들어간 기사만
        if related:
            hit["news"] = related[0]
    except Exception:
        pass
    hit["grade"] = grade(hit)
    return hit


def grade(h):
    """양호: 시총 1조↑ & 거래대금 50억↑ / 주의: 위험표시·시총 1천억↓·거래대금 10억↓ / 나머지 보통"""
    mcap, tv = h.get("mcap"), h.get("tv20") or 0
    if h.get("flags") or (mcap is not None and mcap < 1000) or tv < 10:
        return "주의"
    if mcap is not None and mcap >= 10000 and tv >= 50:
        return "양호"
    return "보통"


def build_report(hits, base_date, scanned, failed):
    d = datetime.strptime(base_date, "%Y%m%d").strftime("%Y-%m-%d")
    lines = [f"📉 국장 RSI({RSI_PERIOD}) {RSI_THRESHOLD:g} 이하 종목", f"기준일: {d} 종가 | 스캔 {scanned}종목", ""]
    if not hits:
        lines.append("해당 종목 없음")
    for mk in ("KOSPI", "KOSDAQ"):
        group = sorted([h for h in hits if h["market"] == mk], key=lambda x: x["rsi"])
        if not group:
            continue
        lines.append(f"[{mk}] {len(group)}종목")
        for h in group:
            extra = f" | 시총 {h['mcap_text']}" if h.get("mcap_text") else ""
            lines.append(
                f"· [{h.get('grade', '-')}] {h['name']}({h['code']}) RSI {h['rsi']:.1f} | "
                f"{h['close']:,.0f}원 ({h['chg']:+.1f}%){extra}"
            )
            if h.get("flags"):
                lines.append(f"   ⚠ {', '.join(h['flags'])}")
            if h.get("news"):
                lines.append(f"   📰 {h['news']}")
        lines.append("")
    if failed:
        lines.append(f"※ 데이터 조회 실패 {failed}종목 제외")
    return "\n".join(lines).strip()


def send_telegram(text):
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return False
    for i in range(0, len(text), 3900):  # 텔레그램 4096자 제한
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text[i:i + 3900]},
            timeout=15,
        ).raise_for_status()
    return True


def send_email(subject, text):
    user, pw, to = os.getenv("GMAIL_USER"), os.getenv("GMAIL_APP_PASSWORD"), os.getenv("MAIL_TO")
    if not (user and pw):
        return False
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to or user
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.send_message(msg)
    return True


def save_site_data(hits, base_date, scanned, failed, extra=None, site_dir="site/data"):
    """웹페이지용: 날짜별 JSON 저장 + 날짜 목록(index.json) 갱신"""
    import json
    os.makedirs(site_dir, exist_ok=True)
    d = datetime.strptime(base_date, "%Y%m%d").strftime("%Y-%m-%d")
    payload = {
        "date": d, "period": RSI_PERIOD, "threshold": RSI_THRESHOLD,
        "scanned": scanned, "failed": failed,
        "generated": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        **(extra or {}),
        "items": sorted(
            [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in h.items() if k not in ("date", "signals", "halted", "since", "tail")} for h in hits],
            key=lambda x: x["rsi"],
        ),
    }
    with open(f"{site_dir}/{d}.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    idx_path = f"{site_dir}/index.json"
    try:
        with open(idx_path, encoding="utf-8") as f:
            idx = json.load(f)
    except Exception:
        idx = []
    idx = [e for e in idx if e["date"] != d] + [{"date": d, "count": len(hits)}]
    idx.sort(key=lambda e: e["date"], reverse=True)
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False)


TRACK_TAG = f"sma{RSI_THRESHOLD:g}"  # 신호 기록 기준 (계산식·기준값)


def update_tracking(results, breadth, site_dir="site/data"):
    """신호별 이후 추이를 signals.json에 누적 (백필 구간 밖의 기존 기록은 유지)"""
    import json
    os.makedirs(site_dir, exist_ok=True)
    path = f"{site_dir}/signals.json"
    try:
        with open(path, encoding="utf-8") as f:
            book = {f"{s['code']}_{s['date']}": s for s in json.load(f) if s.get("t") == TRACK_TAG}  # 기준이 바뀌면 옛 기록 정리
    except Exception:
        book = {}
    for r in results:
        fresh = {f"{r['code']}_{s['date']}" for s in r["signals"]}
        for k in [k for k, v in book.items() if v["code"] == r["code"] and v["date"] >= r["since"] and k not in fresh]:
            del book[k]
        for s in r["signals"]:
            key = f"{r['code']}_{s['date']}"
            old = book.get(key)
            if old and len(old["p"]) > len(s["p"]):
                continue  # 이미 더 긴 기록이 있으면 유지
            b = breadth.get(s["date"], old.get("b") if old else None)
            book[key] = {"code": r["code"], "name": r["name"], "market": r["market"], **s,
                         "b": round(b, 1) if b is not None else None, "t": TRACK_TAG}
    items = sorted(book.values(), key=lambda s: (s["date"], s["code"]), reverse=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, separators=(",", ":"))
    return items


def horizon_stats(items, days=(1, 5, 20, 60)):
    out = []
    for k in days:
        rets = [s["p"][k - 1] for s in items if len(s["p"]) >= k]
        if rets:
            out.append((k, len(rets), sum(rets) / len(rets), sum(r > 0 for r in rets) / len(rets) * 100))
    return out


def build_tracking_summary(items):
    if not items:
        return ""
    lines = ["", "━━━━━━━━━━", f"📈 신호 이후 추이 (RSI {RSI_THRESHOLD:g} 이하 진입일 종가 대비)"]
    for k, n, avg, win in horizon_stats(items):
        lines.append(f"· D+{k}: 평균 {avg:+.1f}% | 상승 비율 {win:.0f}% (표본 {n})")
    active = [s for s in items if len(s["p"]) < TRACK_DAYS][:15]
    if active:
        lines += ["", f"추적 중 (최근 {len(active)}건)"]
        for s in active:
            d = datetime.strptime(s["date"], "%Y%m%d").strftime("%m/%d")
            if s["p"]:
                lines.append(f"· {s['name']} {d} 진입 → D+{len(s['p'])} {s['p'][-1]:+.1f}%")
            else:
                lines.append(f"· {s['name']} {d} 진입 → 오늘부터 추적")
    return "\n".join(lines)


def market_breadth(results):
    """날짜별 RSI(14) BREADTH_RSI 이하 종목 비율(%) — 종목 수가 충분한 날만"""
    from collections import defaultdict
    cnt = defaultdict(lambda: [0, 0])
    for r in results:
        for d, v in r["tail"]:
            if v is not None:
                cnt[d][1] += 1
                cnt[d][0] += v <= BREADTH_RSI
    need = int(os.getenv("BREADTH_MIN_N", "300"))
    return {d: a / b * 100 for d, (a, b) in cnt.items() if b >= need}


def drop_type(b):
    if b is None:
        return None
    return "동반" if b >= PANIC else "부분" if b >= PARTIAL else "단독"


def backtest_summary(path="site/data/backtest.json"):
    """백테스트 결과에서 현재 기준(RSI 14, 임계값)의 하락 유형별 성과 요약"""
    import json
    try:
        with open(path, encoding="utf-8") as f:
            bt = json.load(f)
    except Exception:
        return None
    rule = f"r14_{RSI_THRESHOLD:g}"
    groups = {"동반": ("d25+",), "부분": ("c10-25",), "단독": ("a<3", "b3-10")}
    years = [c["k"][5] for c in bt["cells"]]
    out = {"from": min(years) + "0101" if years else bt.get("from"), "to": bt.get("to"), "cost": bt.get("cost")}
    for name, keys in groups.items():
        cells = [c for c in bt["cells"] if c["k"][0] == rule and c["k"][4] in keys and c["k"][2] != "a<10"]
        g = {f: sum(c[f] for c in cells) for f in ("n", "s5", "c5", "w5", "s20", "c20", "w20")}
        if g["c20"]:
            out[name] = {"n": g["n"], "a5": round(g["s5"] / g["c5"], 1), "w5": round(g["w5"] / g["c5"] * 100),
                         "a20": round(g["s20"] / g["c20"], 1), "w20": round(g["w20"] / g["c20"] * 100)}
    return out


def main():
    universe = [("KOSPI", c, n) for c, n in list_stocks("KOSPI")]
    universe += [("KOSDAQ", c, n) for c, n in list_stocks("KOSDAQ")]
    if not universe:
        sys.exit("종목 목록을 가져오지 못했습니다.")

    print(f"종목 목록: {len(universe)}개")
    results, failed, first_err = [], 0, None
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = [ex.submit(scan_one, *u) for u in universe]
        for f in as_completed(futs):
            try:
                r = f.result()
                if r:
                    results.append(r)
            except Exception as e:
                failed += 1
                first_err = first_err or repr(e)
    print(f"시세 조회 성공 {len(results)} / 실패 {failed}" + (f" (첫 오류: {first_err})" if first_err else ""))

    if not results:
        sys.exit("시세 데이터를 가져오지 못했습니다.")

    base_date = max(r["date"] for r in results)
    current = [r for r in results if r["date"] == base_date]
    breadth = market_breadth(results)
    today_b = breadth.get(base_date)
    print(f"시장 과매도 비율(RSI {BREADTH_RSI}↓): {today_b}")

    hits = [r for r in current if (r["rsi"] <= RSI_THRESHOLD or r["lg"]) and not r["halted"]]
    raw_n = len(hits)
    hits = [h for h in hits if h["close"] >= MIN_PRICE and h["tv20"] >= MIN_TV]  # 1차: 주가·거래대금
    with ThreadPoolExecutor(max_workers=6) as ex:
        hits = list(ex.map(enrich, hits))
    hits = [h for h in hits if h["grade"] != "주의"]  # 2차: 시총·위험표시
    print(f"RSI {RSI_THRESHOLD:g} 이하 {raw_n}종목 중 잡주 제외 후 {len(hits)}종목")
    tracking = update_tracking(results, breadth)
    for h in hits:
        cb = breadth.get(h.get("cross"))
        h["cb"] = round(cb, 1) if cb is not None else None
        h["type"] = drop_type(cb)
        past = [s for s in tracking if s["code"] == h["code"] and len(s["p"]) >= 5]  # 이 종목의 과거 신호 성과
        if past:
            l20 = [s["p"][19] for s in past if len(s["p"]) >= 20]
            h["hist"] = {
                "n": len(past),
                "d5": round(sum(s["p"][4] for s in past) / len(past), 1),
                "d20": round(sum(l20) / len(l20), 1) if l20 else None,
            }

    report = build_report(hits, base_date, len(current), failed) + build_tracking_summary(tracking)
    print(report)
    dates = sorted(breadth)[-120:]
    save_site_data(hits, base_date, len(current), failed, extra={
        "breadth": round(today_b, 1) if today_b is not None else None,
        "bhist": [[d, round(breadth[d], 1)] for d in dates],
        "panic": PANIC, "partial": PARTIAL, "brsi": BREADTH_RSI, "raw": raw_n,
        "bt": backtest_summary(),
    })

    subject = f"[RSI 리포트] {datetime.now(KST):%Y-%m-%d} 국장 RSI {RSI_THRESHOLD:g} 이하 {len(hits)}종목"
    sent = False
    for fn, args in ((send_telegram, (report,)), (send_email, (subject, report))):
        try:
            sent = fn(*args) or sent
        except Exception as e:  # 전송 실패해도 웹페이지 갱신은 계속
            print(f"\n[전송 실패] {fn.__name__}: {e!r}")
    if not sent:
        print("\n(전송 설정 없음: 텔레그램 또는 Gmail 시크릿을 등록하세요)")


if __name__ == "__main__":
    main()
