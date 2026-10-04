"""링크 미리보기용 썸네일(site/og.png) — 매일 그날 결과로 생성"""
import json
import os

from PIL import Image, ImageDraw, ImageFont

FB = "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"
FR = "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"


def font(size, bold=True):
    path = FB if bold else FR
    return ImageFont.truetype(path if os.path.exists(path) else "DejaVuSans.ttf", size)


def main():
    idx = json.load(open("site/data/index.json", encoding="utf-8"))
    d = json.load(open(f"site/data/{idx[0]['date']}.json", encoding="utf-8"))
    items, b = d.get("items", []), d.get("breadth")
    ok = lambda x: x.get("grade") != "주의" and not x.get("flags")
    picks = [x["name"] for x in items if ok(x) and (x.get("type") == "동반" or x.get("lg"))]

    W, H = 1200, 630
    im = Image.new("RGB", (W, H), "#f3f4f6")
    dr = ImageDraw.Draw(im)
    dr.rounded_rectangle((40, 40, W - 40, H - 40), 32, fill="#ffffff")
    dr.text((96, 96), "RSI 과매도 알리미", font=font(60), fill="#16181d")
    dr.text((96, 180), f"{d['date']} 종가 기준 · 국장 · 미장", font=font(30, False), fill="#6f7480")
    dr.text((96, 270), "시장 과매도 비율", font=font(34, False), fill="#3b404a")
    dr.text((96, 318), f"{b:.1f}%" if b is not None else "–", font=font(120), fill="#2a62d4")
    if picks:
        line, color = "매수 후보: " + ", ".join(picks[:3]) + (f" 외 {len(picks) - 3}" if len(picks) > 3 else ""), "#1f8a5b"
    else:
        line, color = "오늘은 쉬어가는 날이에요", "#3b404a"
    dr.text((96, 482), line, font=font(42), fill=color)
    im.save("site/og.png", optimize=True)
    print("og.png:", line)


if __name__ == "__main__":
    main()
