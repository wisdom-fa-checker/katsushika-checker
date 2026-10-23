#!/usr/bin/env python3
"""
葛飾区公共施設 空き状況チェッカー
対象：屋外スポーツ（フットサル・少年サッカー・サッカー）の土日祝日空き確認
出力：空き日 + 空き時間帯（コマ）
通知：メール + LINE Messaging API
"""

import os
import re
import sys
import time
import html
import json
import signal
import smtplib
import subprocess
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import jpholiday

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

GMAIL_USER     = os.getenv("GMAIL_USER")
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD")
NOTIFY_EMAIL   = os.getenv("NOTIFY_EMAIL")
LINE_TOKEN     = os.getenv("LINE_TOKEN")
LINE_USER_ID   = os.getenv("LINE_USER_ID")

BASE_URL = "https://rsv.shisetsu.city.katsushika.lg.jp/katsushika/web"

# 目的コード一覧（selectPpsdCd, selectPpsCd）
TARGET_PURPOSES = {
    "フットサル":   ("200", "201100"),
    "少年サッカー": ("200", "201000"),
    "サッカー":     ("200", "200300"),
}

AVAILABLE_STATUS = {"全て空き", "一部空き"}

# 一時的に対象外にする施設（名前の部分一致）。
# 陸上競技場 = 改修工事中で予約画面が特殊（2026-06）。工事完了後はこの行を削除して復活。
EXCLUDE_FACILITIES = ["陸上競技場"]

# --- メール赤文字ルール（HTMLメール表示用。名前の部分一致で判定）---
# 下記施設は「時間帯を問わず」赤文字にする
ALWAYS_RED_FACILITIES = ["東金町運動場", "水元運動場", "にいじゅくみらい公園"]
# 下記施設は「6:00-8:00以外は赤文字」ルールから除外（＝赤文字にしない）
RED_EXEMPT_FACILITIES = ["堀切橋"]
# 通常施設で赤文字にしない基準コマ（これ以外の時間帯は赤文字）
BASE_SLOT = "6:00-8:00"

WEEKDAY_CLASS = {
    "m_akitablelist_sun": "日",
    "m_akitablelist_sat": "土",
    "m_akitablelist_mon": "月",
    "m_akitablelist_tue": "火",
    "m_akitablelist_wed": "水",
    "m_akitablelist_thu": "木",
    "m_akitablelist_fri": "金",
}

# 面（コート）行の名前にだいたい含まれる文字
AREA_RE = re.compile(r"(面|コート|広場|フィールド|グラウンド|球場|場|室)")


def is_weekend_or_holiday(year: int, month: int, day: int) -> bool:
    d = date(year, month, day)
    return d.weekday() in (5, 6) or bool(jpholiday.is_holiday(d))


def fmt_time(raw: str) -> str:
    """'600' -> '6:00', '1130' -> '11:30'"""
    raw = raw.strip()
    if len(raw) < 3:
        return raw
    mm = raw[-2:]
    hh = raw[:-2]
    return f"{int(hh)}:{mm}"


def wait_frame(page, timeout=10000):
    page.wait_for_load_state("networkidle")
    time.sleep(0.8)
    return page.frames[0]


def read_ym(frame):
    """表示中カレンダーの (年, 月) を返す。読めなければ None"""
    try:
        m = re.search(r"(\d{4})年\s*\n?\s*(\d{1,2})月", frame.inner_text("body"))
    except Exception:
        return None
    return (int(m.group(1)), int(m.group(2))) if m else None


def wait_until_ym(page, prev_ym, expect_change=True, retries=24):
    """ページ遷移後、年月が確定（必要なら prev_ym から変化）するまでリトライ待ち"""
    frame = page.frames[0]
    for _ in range(retries):
        page.wait_for_load_state("networkidle")
        time.sleep(0.5)
        frame = page.frames[0]
        ym = read_ym(frame)
        if ym is not None and (not expect_change or ym != prev_ym):
            return frame, ym
    return frame, read_ym(frame)


def _wait_click_img(page, alt, retries=15):
    """指定 alt の画像（リンク）が描画されるまで待ってクリック"""
    frame = wait_frame(page)
    for _ in range(retries):
        img = frame.query_selector(f"img[alt='{alt}']")
        if img:
            img.query_selector("xpath=..").click()
            return wait_frame(page)
        time.sleep(0.6)
        frame = page.frames[0]
    raise RuntimeError(f"img[alt='{alt}'] が見つかりません")


def _wait_click_link(page, keyword, retries=15):
    """テキストに keyword を含むリンクが描画されるまで待ってクリック"""
    frame = page.frames[0]
    for _ in range(retries):
        link = next((l for l in frame.query_selector_all("a")
                     if keyword in (l.inner_text() or "")), None)
        if link:
            link.click()
            return wait_frame(page)
        time.sleep(0.6)
        frame = page.frames[0]
    raise RuntimeError(f"リンク「{keyword}」が見つかりません")


def navigate_to_entry(page):
    """メニュー →「施設の空き状況」→「利用目的から」→「屋外スポーツ」"""
    page.goto(f"{BASE_URL}/menu.jsp", wait_until="networkidle")
    time.sleep(1)
    page.click("a[href='JavaScript:startMode(1);']")

    _wait_click_img(page, "施設の空き状況")
    _wait_click_img(page, "利用目的から")
    return _wait_click_link(page, "屋外スポーツ")


def _facility_links(frame):
    return [l for l in frame.query_selector_all("a")
            if l.inner_text().strip() not in ["小", "中", "大", ""]]


def get_facilities_for_purpose(page, purpose_name: str) -> list:
    """指定目的の施設一覧 [(施設名, コード), ...] を返す"""
    frame = navigate_to_entry(page)

    # 目的リンクが描画されるまでリトライ（描画前に探すと取りこぼす）
    target = None
    for _ in range(15):
        try:
            target = next((l for l in frame.query_selector_all("a")
                           if (l.inner_text() or "").strip() == purpose_name), None)
        except Exception:
            target = None
        if target:
            break
        time.sleep(0.6)
        frame = page.frames[0]
    if not target:
        print(f"  目的「{purpose_name}」が見つかりません")
        return []
    target.click()

    frame = wait_frame(page)

    # 施設リンクが描画されるまでリトライ
    fac_links = []
    for _ in range(15):
        fac_links = _facility_links(frame)
        if fac_links:
            break
        time.sleep(0.6)
        frame = page.frames[0]
    if not fac_links:
        return []

    first_name = fac_links[0].inner_text().strip()
    fac_links[0].click()

    try:
        page.wait_for_selector("select[name='selectBldCd']", timeout=10000)
    except Exception:
        return [(first_name, "")]

    time.sleep(0.5)
    frame = page.frames[0]
    sel = frame.query_selector("select[name='selectBldCd']")
    if not sel:
        return [(first_name, "")]

    options = sel.query_selector_all("option")
    return [(opt.inner_text().strip(), opt.get_attribute("value") or "") for opt in options]


def navigate_to_facility_calendar(page, purpose_name, facility_name, facility_code):
    """指定施設の月カレンダーまで移動して frame を返す（当月表示）"""
    frame = navigate_to_entry(page)

    target = next((l for l in frame.query_selector_all("a")
                   if l.inner_text().strip() == purpose_name), None)
    if not target:
        return None
    target.click()

    frame = wait_frame(page)
    fac_link = None
    for _ in range(15):
        fac_link = next((l for l in frame.query_selector_all("a")
                         if facility_name in l.inner_text()), None)
        if fac_link:
            break
        time.sleep(0.6)
        frame = page.frames[0]
    if not fac_link:
        return None
    fac_link.click()

    try:
        page.wait_for_selector("select[name='selectBldCd']", timeout=10000)
    except Exception:
        pass

    time.sleep(0.5)
    frame = page.frames[0]
    sel = frame.query_selector("select[name='selectBldCd']")
    if sel and facility_code:
        sel.select_option(value=facility_code)
        redisp = frame.query_selector("img[src*='bw_redisp']")
        if redisp:
            redisp.query_selector("xpath=..").click()
        frame, _ = wait_until_ym(page, None, expect_change=False)

    return frame


def switch_facility(page, facility_code):
    """月カレンダー上でセレクトボックスで施設を切り替える（当月にリセットされる想定）"""
    frame = page.frames[0]
    sel = frame.query_selector("select[name='selectBldCd']")
    if not sel:
        return frame
    sel.select_option(value=facility_code)
    redisp = frame.query_selector("img[src*='bw_redisp']")
    if redisp:
        redisp.query_selector("xpath=..").click()
    frame, _ = wait_until_ym(page, None, expect_change=False)
    return frame


def parse_calendar_page(frame, facility_name, facility_code, purpose):
    """1ページ分のカレンダーを解析して土日祝の空き情報を返す"""
    results = []
    ym = read_ym(frame)
    if not ym:
        return results
    year, month = ym

    cells = frame.query_selector_all("td[class*='m_akitablelist']")
    for cell in cells:
        cell_class = cell.get_attribute("class") or ""
        weekday = next((wd for k, wd in WEEKDAY_CLASS.items() if k in cell_class), None)
        if weekday is None:
            continue

        strong = cell.query_selector("strong")
        if not strong:
            continue
        day_text = strong.inner_text().replace("本日", "").strip()
        if not day_text:
            continue
        dm = re.search(r"(\d+)", day_text)
        if not dm:
            continue
        day_num = int(dm.group(1))

        if not is_weekend_or_holiday(year, month, day_num):
            continue

        img = cell.query_selector("img[alt]")
        if not img:
            continue
        status = img.get_attribute("alt") or ""
        if status not in AVAILABLE_STATUS:
            continue

        try:
            holiday_name = jpholiday.is_holiday_name(date(year, month, day_num)) or ""
        except Exception:
            holiday_name = ""

        results.append({
            "purpose": purpose,
            "facility": facility_name,
            "code": facility_code,
            "year": year, "month": month, "day": day_num,
            "date": f"{year}/{month:02d}/{day_num:02d}({weekday})",
            "date_obj": date(year, month, day_num),
            "status": status,
            "holiday": holiday_name,
            "slots": [],
        })

    return results


def reset_to_month(page, target_y, target_m, max_steps=10):
    """表示カレンダーを target_y/target_m に合わせる（前の月/次の月で調整）"""
    frame, ym = wait_until_ym(page, None, expect_change=False)
    for _ in range(max_steps):
        if not ym:
            break
        if (ym[0], ym[1]) == (target_y, target_m):
            return frame
        btn_alt = "前の月" if (ym[0], ym[1]) > (target_y, target_m) else "次の月"
        btn = frame.query_selector(f"img[alt='{btn_alt}']")
        if not btn:
            break
        prev = ym
        btn.query_selector("xpath=..").click()
        frame, ym = wait_until_ym(page, prev, expect_change=True)
    return page.frames[0]


def check_facility_two_months(page, facility_name, facility_code, purpose):
    """当月・翌月の2ヶ月分をチェック（堅牢な月送り）"""
    results = []
    now = datetime.now()
    frame = reset_to_month(page, now.year, now.month)

    results.extend(parse_calendar_page(frame, facility_name, facility_code, purpose))

    prev_ym = read_ym(frame)
    next_btn = frame.query_selector("img[alt='次の月']")
    if next_btn:
        next_btn.query_selector("xpath=..").click()
        frame, _ = wait_until_ym(page, prev_ym, expect_change=True)
        results.extend(parse_calendar_page(frame, facility_name, facility_code, purpose))

    return results


def find_day_link(frame, day):
    """月カレンダーから指定日のクリック用 <a> を返す"""
    for cell in frame.query_selector_all("td[class*='m_akitablelist']"):
        strong = cell.query_selector("strong")
        if not strong:
            continue
        m = re.search(r"(\d+)", strong.inner_text())
        if m and int(m.group(1)) == day:
            return cell.query_selector("a")
    return None


def parse_timeslot_grid(frame):
    """日別グリッドを解析。戻り値 (grid_found, slots)
    grid_found: 時間帯ヘッダを認識できたか（False=パース失敗/未ロード）
    slots: 実際に「空き」コマの時間帯リスト ['6:00-8:00', ...]
    """
    trs = frame.query_selector_all("tr")

    # ヘッダ行 = 時間帯セルを2個以上含み、最もセル数が少ない行（入れ子の最内側=綺麗な行）
    ranges = None
    header_idx = None
    best_cells = 10 ** 9
    for tr in trs:
        cells = tr.query_selector_all("td, th")
        texts = [(c.inner_text() or "").replace(" ", "").replace("\n", "") for c in cells]
        tidx = [i for i, t in enumerate(texts) if re.search(r"\d{3,4}-\d{3,4}", t)]
        if len(tidx) >= 2 and len(cells) < best_cells:
            best_cells = len(cells)
            header_idx = tidx
            ranges = [re.search(r"(\d{3,4})-(\d{3,4})", texts[i]).groups() for i in tidx]

    if not ranges:
        return (False, [])

    # 面（コート）行を、ヘッダと同じ列インデックスで読む（列整列）
    avail = [False] * len(ranges)
    for tr in trs:
        cells = tr.query_selector_all("td, th")
        if len(cells) <= max(header_idx):
            continue
        first = (cells[0].inner_text() or "").strip()
        if not first or not AREA_RE.search(first):
            continue
        for k, ci in enumerate(header_idx):
            im = cells[ci].query_selector("img[alt]")
            if im and im.get_attribute("alt") == "空き":
                avail[k] = True

    slots = [f"{fmt_time(s)}-{fmt_time(e)}" for k, (s, e) in enumerate(ranges) if avail[k]]
    return (True, slots)


def fetch_timeslots(page, purpose, facility_name, facility_code, year, month, day):
    """空き日の日別画面を開き、(status, slots) を返す
    status: 'ok'=空きコマあり / 'none'=グリッド確認済だが空きコマ無し / 'unknown'=取得失敗
    """
    try:
        frame = navigate_to_facility_calendar(page, purpose, facility_name, facility_code)
        if not frame:
            return ("unknown", [])

        # 対象月まで送る
        cur = read_ym(frame)
        guard = 0
        while cur and (cur[0], cur[1]) != (year, month) and guard < 6:
            nb = frame.query_selector("img[alt='次の月']")
            if not nb:
                break
            nb.query_selector("xpath=..").click()
            frame, cur = wait_until_ym(page, cur, expect_change=True)
            guard += 1

        link = find_day_link(frame, day)
        if not link:
            return ("unknown", [])
        link.click()
        page.wait_for_load_state("networkidle")
        time.sleep(1.5)
        frame = page.frames[0]

        # 日別画面で対象施設を選び直し「再表示」（最大2回試行）
        found, slots = False, []
        for attempt in range(2):
            sel = frame.query_selector("select[name='selectBldCd']")
            if sel and facility_code:
                sel.select_option(value=facility_code)
                time.sleep(0.6)
                rd = frame.query_selector("img[src*='bw_redisp']")
                if rd:
                    rd.query_selector("xpath=..").click()
                    page.wait_for_load_state("networkidle")
                    time.sleep(1.5 + attempt)
                    frame = page.frames[0]
            found, slots = parse_timeslot_grid(frame)
            if found:
                break
            time.sleep(0.5)
            frame = page.frames[0]

        if not found:
            return ("unknown", [])
        return ("ok", slots) if slots else ("none", [])
    except Exception as e:
        print(f"    [時間帯取得エラー] {facility_name} {year}/{month}/{day}: {e}")
        return ("unknown", [])


def send_email(subject, body, html_body=None):
    """送信できたら True、できなかったら False を返す（2026-10-23）。
    呼び出し側はこれを見て「通知済み」として状態を保存してよいかを決める。"""
    if not all([GMAIL_USER, GMAIL_PASSWORD, NOTIFY_EMAIL]):
        print("[EMAIL] 設定なし（スキップ）")
        return False
    # html_body があれば multipart/alternative（プレーン+HTML）で送る。
    # 対応クライアントはHTML（赤文字）を、非対応はプレーンを表示する。
    msg = MIMEMultipart("alternative") if html_body else MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = NOTIFY_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    # DNS瞬断・一時的なネットワーク不調に備えて最大3回リトライ
    last_err = None
    for attempt in range(1, 4):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
                server.login(GMAIL_USER, GMAIL_PASSWORD)
                server.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())
            print(f"[EMAIL] 送信完了（{attempt}回目）")
            return True
        except Exception as e:
            last_err = e
            print(f"[EMAIL] 送信失敗({attempt}/3): {e}")
            if attempt < 3:
                time.sleep(15)
    print(f"[EMAIL] 3回とも失敗: {last_err}")
    return False


def send_line(message):
    if not all([LINE_TOKEN, LINE_USER_ID]):
        print("[LINE] 設定なし（スキップ）")
        return
    try:
        res = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
            json={"to": LINE_USER_ID, "messages": [{"type": "text", "text": message}]},
            timeout=10,
        )
        print("[LINE] 送信完了" if res.status_code == 200
              else f"[LINE] 送信失敗: {res.status_code} {res.text[:100]}")
    except Exception as e:
        print(f"[LINE] 送信失敗: {e}")


def format_results(all_results):
    if not all_results:
        return "空き情報なし"
    all_results.sort(key=lambda x: (x["purpose"], x["date_obj"], x["facility"]))
    lines = []
    current = None
    for item in all_results:
        if item["purpose"] != current:
            if current:
                lines.append("")
            lines.append(f"【{item['purpose']}】")
            current = item["purpose"]
        holiday = f" ({item['holiday']})" if item["holiday"] else ""
        lines.append(f"  {item['date']}{holiday}  {item['facility']}")
        if item.get("slots"):
            lines.append(f"      空き時間: {' / '.join(item['slots'])}")
        elif item.get("slot_status") == "unknown":
            lines.append(f"      空き時間: 予約画面でご確認ください")
    return "\n".join(lines)


def slot_is_red(facility: str, slot: str) -> bool:
    """この施設・この時間帯コマを赤文字にすべきか判定。
    優先順位：
      1) ALWAYS_RED_FACILITIES → 時間帯問わず赤
      2) RED_EXEMPT_FACILITIES（堀切橋）→ 赤にしない
      3) それ以外 → BASE_SLOT(6:00-8:00) 以外は赤
    """
    if any(name in facility for name in ALWAYS_RED_FACILITIES):
        return True
    if any(name in facility for name in RED_EXEMPT_FACILITIES):
        return False
    return slot.strip() != BASE_SLOT


def _slot_html(facility: str, slots: list) -> str:
    out = []
    for s in slots:
        s_esc = html.escape(s)
        if slot_is_red(facility, s):
            out.append(f'<span style="color:#d00000;font-weight:bold">{s_esc}</span>')
        else:
            out.append(s_esc)
    return " / ".join(out)


def format_results_html(all_results):
    """メール用HTML。format_results と同じ並びで、該当コマを赤文字にする。"""
    if not all_results:
        return "<p>空き情報なし</p>"
    all_results.sort(key=lambda x: (x["purpose"], x["date_obj"], x["facility"]))
    lines = []
    current = None
    for item in all_results:
        if item["purpose"] != current:
            if current:
                lines.append("")
            lines.append(f'<b>【{html.escape(item["purpose"])}】</b>')
            current = item["purpose"]
        holiday = f" ({html.escape(item['holiday'])})" if item["holiday"] else ""
        lines.append(f'&nbsp;&nbsp;{html.escape(item["date"])}{holiday}&nbsp;&nbsp;'
                     f'{html.escape(item["facility"])}')
        if item.get("slots"):
            lines.append(f'&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;空き時間: '
                         f'{_slot_html(item["facility"], item["slots"])}')
        elif item.get("slot_status") == "unknown":
            lines.append("&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;空き時間: 予約画面でご確認ください")
    body = "<br>\n".join(lines)
    return (f'<div style="font-family:monospace,monospace;font-size:14px;'
            f'line-height:1.6">{body}</div>')


# --- 取得失敗の記録・整形（2026-08-04 追加）---
# 目的：取得に失敗した種目・施設を「黙って0件扱い」にせず、必ずメールに出す。
# kind='purpose' … 施設一覧の取得に失敗＝その種目を丸ごとチェックできていない（重大）
# kind='slot'    … 施設×日の時間帯取得に失敗＝その1枠だけ不明

def _group_failures(failures):
    """failures を種目単位にまとめる。戻り値: (種目丸ごと失敗のlist, {種目: [表記, ...]})"""
    purpose_failed = [f["purpose"] for f in failures if f.get("kind") == "purpose"]
    slot_failed = {}
    for f in failures:
        if f.get("kind") != "slot":
            continue
        slot_failed.setdefault(f["purpose"], []).append(
            f'{f["facility"]} {f["date"]}')
    return purpose_failed, slot_failed


def format_failures(failures):
    """取得失敗の一覧（プレーンテキスト）。失敗が無ければ空文字。"""
    if not failures:
        return ""
    purpose_failed, slot_failed = _group_failures(failures)
    lines = ["", "⚠ 今回チェックできなかったもの（空き有無は不明）"]
    for p in purpose_failed:
        lines.append(f"  【{p}】施設一覧の取得に失敗 → この種目は今回チェックできていません")
    for p, items in slot_failed.items():
        lines.append(f"  【{p}】時間帯を取得できなかった枠:")
        for it in items:
            lines.append(f"      - {it}")
    lines.append("  ※ 上記は予約画面で直接ご確認ください。")
    return "\n".join(lines)


def format_failures_html(failures):
    """取得失敗の一覧（HTML）。失敗が無ければ空文字。"""
    if not failures:
        return ""
    purpose_failed, slot_failed = _group_failures(failures)
    lines = ['<b style="color:#d00000">⚠ 今回チェックできなかったもの（空き有無は不明）</b>']
    for p in purpose_failed:
        lines.append(f'&nbsp;&nbsp;<b style="color:#d00000">【{html.escape(p)}】'
                     f'施設一覧の取得に失敗 → この種目は今回チェックできていません</b>')
    for p, items in slot_failed.items():
        lines.append(f'&nbsp;&nbsp;【{html.escape(p)}】時間帯を取得できなかった枠:')
        for it in items:
            lines.append(f'&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;- {html.escape(it)}')
    lines.append('&nbsp;&nbsp;※ 上記は予約画面で直接ご確認ください。')
    body = "<br>\n".join(lines)
    return (f'<div style="font-family:monospace,monospace;font-size:14px;'
            f'line-height:1.6;margin-top:12px">{body}</div>')


def parse_target_dates(args):
    """'2026-06-21' '6/21' '06/21' などを date に変換。
    年省略時は「今日以降で最も近い年」を採用（過去日なら翌年扱い）。"""
    today = date.today()
    out = []
    for a in args:
        a = a.strip()
        m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$", a)
        if m:
            y, mo, d = (int(x) for x in m.groups())
        else:
            m = re.match(r"^(\d{1,2})[-/](\d{1,2})$", a)
            if not m:
                print(f"  日付として認識できません: {a}")
                continue
            mo, d = (int(x) for x in m.groups())
            y = today.year
            try:
                if date(y, mo, d) < today:
                    y += 1
            except ValueError:
                print(f"  無効な日付: {a}")
                continue
        try:
            out.append(date(y, mo, d))
        except ValueError:
            print(f"  無効な日付: {a}")
    return sorted(set(out))


# 予約可能な窓（本日から何日先まで）。区のルール：本日から1ヶ月先まで。
BOOKING_WINDOW_DAYS = 30


# ============================================================
# 対象日と絞り込みのルール（2026-09-20 代表指示で全面整理）
#
#   土日祝        … 全施設・8時以降（早朝6-8時は代表が使わないため除外）
#   毎週木曜      … 下記4施設・17時以降のみ
#   単発の指定日  … EXTRA_DATES。木曜と同じ絞り込みを適用する
#
# これにより、別タスクだった小菅西の木曜監視（kosuge-thursday-watch）を
# 本タスクに統合した。同タスクは削除済み。
# ============================================================
# 土日祝でなくても必ず見る単発の日付。'YYYY-MM-DD' 形式。
# 過去になった日・窓の外の日は自動で無視されるため、消し忘れても害はない。
# 9/28(月) は学校が休みで、17時以降の会場を押さえたい（2026-09-20 代表指示）。
EXTRA_DATES = ["2026-09-28"]

# 毎週見る平日の曜日（0=月〜6=日）。土日祝は別枠で必ず入る。
WEEKLY_WEEKDAYS = [3]           # 木曜

# 「限定監視の日」に見る施設（部分一致）と、拾う枠の下限時刻。
# 枠の開始がこの時刻以降のものだけを対象にする（上限は設けない）。
# 上千葉は 2026-09-25 代表指示で追加（フットサル種目の少年球技場）。
LIMITED_FACILITIES = ["東金町", "にいじゅく", "水元", "小菅西", "上千葉"]
LIMITED_FROM_HOUR = 17.0

# 土日祝（全施設を見る日）で拾う枠の下限時刻。
# 早朝 6:00-8:00 は「100%予約することがない」ため通知から外す（2026-09-20 代表指示）。
# 直近の実績ではこの枠が全通知件数の46%を占めていた＝ノイズ削減が目的。
# 巡回の速度には影響しない（ページ遷移の回数が変わらないため）。
# 8時より前に始まる枠がすべて対象外になる（現状は 6:00-8:00 のみ該当）。
FULL_SCOPE_FROM_HOUR = 8.0


def is_full_scope_date(d):
    """その日を「全施設・全時間帯」で見るか。土日祝＝True、それ以外＝False。"""
    try:
        holiday = bool(jpholiday.is_holiday(d))
    except Exception:
        holiday = False
    return d.weekday() in (5, 6) or holiday


def scope_for_date(d):
    """その日に適用する絞り込みを返す。
    戻り値 (施設キーワードのリスト or None, 下限時刻 or None)。None は「絞らない」。"""
    if is_full_scope_date(d):
        return (None, FULL_SCOPE_FROM_HOUR)
    return (LIMITED_FACILITIES, LIMITED_FROM_HOUR)


def slot_start_hour(slot):
    """'18:30-20:30' → 18.5。読めない場合は None。"""
    try:
        h, m = slot.split("-")[0].split(":")
        return int(h) + int(m) / 60
    except Exception:
        return None


def filter_slots(slots, from_hour):
    """開始が from_hour 以降の枠だけ残す。from_hour が None なら素通し。
    開始時刻を読めない枠は落とさず残す（区の表記が変わった時に黙って消えないように）。"""
    if from_hour is None:
        return list(slots)
    out = []
    for x in slots:
        h = slot_start_hour(x)
        if h is None or h >= from_hour:
            out.append(x)
    return out


def filter_facilities(facs, keywords):
    """(name, code) のリストをキーワード部分一致で絞る。keywords が None なら素通し。"""
    if not keywords:
        return list(facs)
    return [(n, c) for (n, c) in facs if any(k in n for k in keywords)]


def parse_extra_dates(raw_list):
    """'YYYY-MM-DD' の文字列リスト → date のリスト。読めない値は警告して捨てる。"""
    out = []
    for raw in raw_list or []:
        raw = str(raw).strip()
        if not raw:
            continue
        try:
            d = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            print(f"[警告] 単発の日付を読めませんでした（無視します）: {raw}")
            continue
        if d not in out:
            out.append(d)
    return sorted(out)


def auto_target_dates(window_days: int = BOOKING_WINDOW_DAYS, extra_dates=None):
    """本日〜window_days日後までの 土日祝＋WEEKLY_WEEKDAYS＋EXTRA_DATES を返す。

    区の予約は「本日から1ヶ月先まで」しか取れないため、窓は毎日スライドする。
    固定の日付リストだと対象日がどんどん過去になり手動更新が必要になるので、
    実行のたびに窓内の土日祝を計算し直す。新しく予約可能になった日は
    自動的に対象へ入り、差分通知の仕組みで「新着」として1回だけ通知される。
    """
    today = date.today()
    last = today + timedelta(days=window_days)
    picked = {today + timedelta(days=i)
              for i in range(window_days + 1)
              if (today + timedelta(days=i)).weekday() in (5, 6)
              or jpholiday.is_holiday(today + timedelta(days=i))}
    # 毎週見る平日（既定＝木曜）を足す
    picked |= {today + timedelta(days=i)
               for i in range(window_days + 1)
               if (today + timedelta(days=i)).weekday() in WEEKLY_WEEKDAYS}
    # 土日祝でも木曜でもない単発の指定日を足す（窓の中のものだけ）
    for d in parse_extra_dates(EXTRA_DATES if extra_dates is None else extra_dates):
        if today <= d <= last:
            picked.add(d)
    return sorted(picked)


# ============================================================
# 高速版：指定日モード（2026-09-07 実装）
#
# 旧実装は「施設 × 日付」の組み合わせごとにトップメニューから全遷移し直しており、
# 1件あたり9〜11秒かかっていた。日別画面には施設切替ドロップダウン（selectBldCd）
# ＋再表示ボタンがあり、これで施設を切り替えても日付は維持される（実測1.0〜1.5秒）。
# そこで「日付ごとに1回だけ日別画面を開き、あとはドロップダウンで全施設を回す」
# 方式に変更した。あわせて
#   ・画像/CSS/フォントの読み込みをブロック（旧型JSPサイトで効果大）
#   ・固定 time.sleep を「条件が満たされるまでのポーリング」に置換
#   ・グリッド解析を1回のJS実行に集約（要素ハンドル数百往復を1往復に）
# を実施。取得に失敗した施設は旧方式（フル遷移）でフォールバック再取得する。
# ============================================================

# 文書以外のリソースは読み込まない（<img> タグ自体はDOMに残るので alt 判定に影響なし）
BLOCK_RESOURCE_TYPES = {"image", "stylesheet", "font", "media"}

# 日別グリッドを1回のJS実行で解析する（Python版 parse_timeslot_grid と同じロジック）
JS_PARSE_GRID = r"""
() => {
  const AREA = /(面|コート|広場|フィールド|グラウンド|球場|場|室)/;
  const RANGE = /(\d{3,4})-(\d{3,4})/;
  const trs = [...document.querySelectorAll('tr')];
  let ranges = null, headerIdx = null, best = 1e9;
  for (const tr of trs) {
    const cells = [...tr.querySelectorAll('td,th')];
    const texts = cells.map(c => (c.innerText || '').replace(/\s/g, ''));
    const tidx = texts.map((t, i) => RANGE.test(t) ? i : -1).filter(i => i >= 0);
    if (tidx.length >= 2 && cells.length < best) {
      best = cells.length; headerIdx = tidx;
      ranges = tidx.map(i => { const m = texts[i].match(RANGE); return [m[1], m[2]]; });
    }
  }
  if (!ranges) return {found: false};
  const avail = ranges.map(() => false);
  const maxIdx = Math.max(...headerIdx);
  for (const tr of trs) {
    const cells = [...tr.querySelectorAll('td,th')];
    if (cells.length <= maxIdx) continue;
    const first = (cells[0].innerText || '').trim();
    if (!first || !AREA.test(first)) continue;
    headerIdx.forEach((ci, k) => {
      const im = cells[ci].querySelector('img[alt]');
      if (im && im.getAttribute('alt') === '空き') avail[k] = true;
    });
  }
  return {found: true, ranges: ranges, avail: avail};
}
"""

JS_OPTIONS = "s => [...s.options].map(o => [o.text.trim(), o.value])"


def new_fast_page(browser):
    """画像等をブロックしたページを作る"""
    page = browser.new_page()
    page.route("**/*", lambda route: (
        route.abort() if route.request.resource_type in BLOCK_RESOURCE_TYPES
        else route.continue_()))
    return page


def _poll(page, fn, timeout=20.0, interval=0.15):
    """frameを毎回取り直しながら fn(frame) が真を返すまで待つ。
    フレーム遷移中の 'Execution context was destroyed' を握りつぶして再試行する。"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            frame = page.frames[0]
            val = fn(frame)
            if val:
                return frame, val
        except Exception:
            pass
        time.sleep(interval)
    return page.frames[0], None


def _mark_doc(page):
    """今の文書に目印を付ける（遷移検知用）"""
    try:
        page.frames[0].evaluate("() => { window.__kc_mark = 1; }")
    except Exception:
        pass


def _wait_new_doc(page, timeout=20.0):
    """_mark_doc を付けた文書が置き換わる（＝遷移完了）まで待つ"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if page.frames[0].evaluate("() => window.__kc_mark === undefined"):
                return True
        except Exception:
            pass
        time.sleep(0.12)
    return False


def _click_img_fast(page, alt, timeout=20.0):
    def go(frame):
        img = frame.query_selector(f"img[alt='{alt}']")
        if not img:
            return None
        img.query_selector("xpath=..").click()
        return True
    _, ok = _poll(page, go, timeout)
    if not ok:
        raise RuntimeError(f"img[alt='{alt}'] が見つかりません")


def _click_link_fast(page, eq=None, contains=None, timeout=20.0):
    def go(frame):
        for a in frame.query_selector_all("a"):
            t = (a.inner_text() or "").strip()
            if (eq and t == eq) or (contains and contains in t):
                a.click()
                return True
        return None
    _, ok = _poll(page, go, timeout)
    if not ok:
        raise RuntimeError(f"リンク「{eq or contains}」が見つかりません")


def _parse_grid_js(frame):
    """(found, slots) を返す。JS1回で解析する高速版"""
    r = frame.evaluate(JS_PARSE_GRID)
    if not r.get("found"):
        return (False, [])
    slots = [f"{fmt_time(s)}-{fmt_time(e)}"
             for k, (s, e) in enumerate(r["ranges"]) if r["avail"][k]]
    return (True, slots)


def open_purpose_calendar(page, purpose_name):
    """メニュー → 屋外スポーツ → 目的 → 先頭施設のカレンダー。
    戻り値: 施設一覧 [(名前, コード), ...]（ドロップダウンから取得）"""
    page.goto(f"{BASE_URL}/menu.jsp", wait_until="domcontentloaded")
    page.click("a[href='JavaScript:startMode(1);']")
    _click_img_fast(page, "施設の空き状況")
    _click_img_fast(page, "利用目的から")
    _click_link_fast(page, contains="屋外スポーツ")
    _click_link_fast(page, eq=purpose_name)

    def click_first_facility(frame):
        links = [l for l in frame.query_selector_all("a")
                 if l.inner_text().strip() not in ["小", "中", "大", ""]]
        if not links:
            return None
        links[0].click()
        return True
    _poll(page, click_first_facility)

    _, opts = _poll(page, lambda fr: fr.eval_on_selector(
        "select[name='selectBldCd']", JS_OPTIONS))
    return [(n, v) for n, v in (opts or [])]


def _calendar_goto_month(page, year, month, max_steps=6):
    """カレンダーを対象年月まで送る"""
    frame, ym = _poll(page, lambda fr: read_ym(fr))
    steps = 0
    while ym and (ym[0], ym[1]) != (year, month) and steps < max_steps:
        prev = ym
        _mark_doc(page)
        _poll(page, lambda fr: (lambda nb: nb and (
            nb.query_selector("xpath=..").click() or True))(
            fr.query_selector("img[alt='次の月']")))
        _wait_new_doc(page)
        frame, ym = _poll(page, lambda fr: (lambda y: y if y and y != prev else None)(read_ym(fr)))
        steps += 1
    return frame, ym


def _calendar_switch_facility(page, code):
    """カレンダー画面でドロップダウン＋再表示により施設を切り替える"""
    _mark_doc(page)

    def go(frame):
        frame.eval_on_selector("select[name='selectBldCd']", "(s, v) => { s.value = v; }", code)
        rd = frame.query_selector("img[src*='bw_redisp']")
        if not rd:
            return None
        rd.query_selector("xpath=..").click()
        return True
    _, ok = _poll(page, go)
    if ok:
        _wait_new_doc(page)
    return _poll(page, lambda fr: read_ym(fr))[0]


def enter_day_screen(page, purpose_name, facilities, td, max_try=8):
    """指定日の日別画面まで入る。日リンクが立っている施設を順に試す。
    戻り値: True/False"""
    open_purpose_calendar(page, purpose_name)
    _calendar_goto_month(page, td.year, td.month)

    for i, (name, code) in enumerate(facilities[:max_try]):
        if i > 0:
            _calendar_switch_facility(page, code)
            _calendar_goto_month(page, td.year, td.month)
        frame, link = _poll(page, lambda fr: find_day_link(fr, td.day), timeout=6.0)
        if not link:
            continue
        _mark_doc(page)
        try:
            link.click()
        except Exception:
            _poll(page, lambda fr: (lambda a: a and (a.click() or True))(find_day_link(fr, td.day)),
                  timeout=6.0)
        _wait_new_doc(page)
        _, grid = _poll(page, lambda fr: (lambda g: g if g[0] else None)(_parse_grid_js(fr)),
                        timeout=25.0)
        if grid:
            return True
    return False


def day_screen_facilities(page):
    """日別画面の施設ドロップダウンの一覧を返す"""
    _, opts = _poll(page, lambda fr: fr.eval_on_selector(
        "select[name='selectBldCd']", JS_OPTIONS))
    return [(n, v) for n, v in (opts or [])]


def day_screen_select(page, code, timeout=25.0):
    """日別画面で施設を切り替えてグリッドを読む。(status, slots) を返す"""
    _mark_doc(page)

    def go(frame):
        frame.eval_on_selector("select[name='selectBldCd']", "(s, v) => { s.value = v; }", code)
        rd = frame.query_selector("img[src*='bw_redisp']")
        if not rd:
            return None
        rd.query_selector("xpath=..").click()
        return True
    _, ok = _poll(page, go, timeout=10.0)
    if not ok:
        return ("unknown", [])
    _wait_new_doc(page, timeout=timeout)

    def read(frame):
        # 切替後の施設が実際に表示されているかを確認してから解析する
        cur = frame.eval_on_selector("select[name='selectBldCd']", "s => s.value")
        if cur != code:
            return None
        found, slots = _parse_grid_js(frame)
        return (found, slots) if found else None
    _, res = _poll(page, read, timeout=timeout)
    if not res:
        return ("unknown", [])
    _, slots = res
    return ("ok", slots) if slots else ("none", [])


def run_for_dates(target_dates):
    """指定日だけ、全目的・全施設の時間帯を直接取得する（指定日モード・高速版）。
    戻り値: (results, failures) — failures は取得できなかった種目・枠の記録。"""
    results = []
    failures = []
    wd = "月火水木金土日"
    t_start = time.time()

    with sync_playwright() as p:
        browser = launch_browser(p)
        page = new_fast_page(browser)

        for purpose_name in TARGET_PURPOSES:
            print(f"[{purpose_name}] 施設一覧を取得中...")
            facilities = []
            for attempt in range(3):
                try:
                    facilities = open_purpose_calendar(page, purpose_name)
                    if facilities:
                        break
                except Exception as e:
                    print(f"  → 取得リトライ({attempt+1}/3): {e}")
                    time.sleep(1)
            facilities = [(n, c) for (n, c) in facilities
                          if not any(x in n for x in EXCLUDE_FACILITIES)]
            print(f"  → {len(facilities)}施設")
            if not facilities:
                print(f"  → [種目取得失敗] {purpose_name} はチェックできませんでした")
                failures.append({"kind": "purpose", "purpose": purpose_name})
                continue

            for td in target_dates:
                date_label = (f"{td.year}/{td.month:02d}/{td.day:02d}"
                              f"({wd[td.weekday()]})")
                fac_keywords, from_hour = scope_for_date(td)
                # 限定監視の日は、その施設が種目一覧に無ければ丸ごと飛ばす
                if fac_keywords and not filter_facilities(facilities, fac_keywords):
                    print(f"  → [対象施設なし] {purpose_name} {date_label}")
                    continue
                entered = False
                for attempt in range(2):
                    try:
                        entered = enter_day_screen(page, purpose_name, facilities, td)
                    except Exception as e:
                        print(f"  → 日別画面へ入れずリトライ({attempt+1}/2) {date_label}: {e}")
                        entered = False
                    if entered:
                        break
                if not entered:
                    # その日は日別画面に入れなかった＝対象施設をチェック不可
                    print(f"  → [日別画面失敗] {purpose_name} {date_label}")
                    for name, _ in filter_facilities(facilities, fac_keywords):
                        failures.append({"kind": "slot", "purpose": purpose_name,
                                         "facility": name, "date": date_label})
                    continue

                # 日別画面のドロップダウンで全施設を回す
                day_facs = day_screen_facilities(page)
                day_facs = [(n, c) for (n, c) in day_facs
                            if not any(x in n for x in EXCLUDE_FACILITIES)]
                if not day_facs:
                    day_facs = facilities
                # 限定監視の日は指定4施設だけを回す（負荷を抑えるため）
                day_facs = filter_facilities(day_facs, fac_keywords)
                if fac_keywords:
                    print(f"    （{date_label} は限定監視: "
                          f"{len(day_facs)}施設・{int(from_hour)}時以降）")

                for fac_name, fac_code in day_facs:
                    status, slots = day_screen_select(page, fac_code)
                    if status == "unknown":
                        # 1回だけ切替をやり直す
                        status, slots = day_screen_select(page, fac_code)
                    if status == "unknown":
                        # それでも駄目なら旧方式（フル遷移）でフォールバック
                        status, slots = fetch_timeslots(
                            page, purpose_name, fac_name, fac_code,
                            td.year, td.month, td.day)
                        if status != "unknown":
                            print(f"    [フォールバック成功] {fac_name} {date_label}")
                        # フル遷移で画面が変わったので日別画面に戻す
                        if not enter_day_screen(page, purpose_name, facilities, td):
                            print(f"  → [日別画面復帰失敗] {purpose_name} {date_label}")
                            failures.append({"kind": "slot", "purpose": purpose_name,
                                             "facility": fac_name, "date": date_label})
                            break

                    if status == "unknown":
                        print(f"    [取得失敗] {fac_name} {td}")
                        failures.append({"kind": "slot", "purpose": purpose_name,
                                         "facility": fac_name, "date": date_label})
                        continue
                    if status == "none":
                        # 予約枠なしは出さない（指定日モードはノイズを排除）
                        continue

                    # 限定監視の日は下限時刻より前の枠を落とす
                    slots = filter_slots(slots, from_hour)
                    if not slots:
                        continue

                    try:
                        holiday = jpholiday.is_holiday_name(td) or ""
                    except Exception:
                        holiday = ""
                    results.append({
                        "purpose": purpose_name, "facility": fac_name, "code": fac_code,
                        "year": td.year, "month": td.month, "day": td.day,
                        "date": date_label,
                        "date_obj": td, "status": "一部空き", "holiday": holiday,
                        "slots": slots, "slot_status": status,
                    })
                    print(f"    [空き] {fac_name} {td}: {', '.join(slots)}")

        browser.close()

    print(f"\n[所要時間] {time.time() - t_start:.0f}秒")
    return results, failures


# ============================================================
# 差分通知（2026-09-07 実装）
#
# 1日5回（7・11・15・19・23時）に増やしたため、「空きがあれば毎回メール」だと
# 同じ内容が1日5通届いて通知が機能しなくなる。そこで前回結果を last_state.json に
# 保存し、**前回から新しく出た枠がある時だけ**メールする方式に変更した。
# ・初回（状態ファイルなし）は全件を通知
# ・枠が埋まった（減った）だけの変化では通知しない
# ・種目まるごと取得失敗は異常なので通知するが、前回と同じ失敗内容なら再通知しない
# ============================================================

STATE_FILE = os.path.join(os.path.dirname(__file__), "last_state.json")


def _slot_key(r):
    return f"{r['purpose']}|{r['facility']}|{r['date']}"


def load_state():
    """前回の状態を返す。無ければ None（＝初回扱い）"""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_state(all_results, failures, prev=None, keep_prev=False, meta=None):
    """今回の状態を保存する。
    keep_prev=True のときは空き枠・失敗内容を前回のまま残す（2026-10-23）。
    メール送信に失敗した回に今回の枠を保存すると、次回「前回もあった」と判定され
    その枠は二度と通知されない。前回のままにしておけば次回もう一度通知される。
    meta は完走回数・生存確認の送信日など、枠とは別に毎回更新する情報。"""
    if keep_prev:
        prev = prev or {}
        state = {
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "slots": prev.get("slots", {}),
            "failure_sig": prev.get("failure_sig", []),
        }
    else:
        state = {
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "slots": {_slot_key(r): sorted(r["slots"]) for r in all_results},
            "failure_sig": sorted(f"{f.get('purpose')}|{f.get('facility', '')}|{f.get('date', '')}"
                                  for f in failures),
        }
    state.update(meta or {})
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"[状態保存エラー] {e}")


def diff_new_slots(all_results, prev):
    """前回から新しく出た枠だけを抜き出す（results と同じ形で返す）"""
    if prev is None:
        return list(all_results)
    prev_slots = prev.get("slots", {})
    new_items = []
    for r in all_results:
        before = set(prev_slots.get(_slot_key(r), []))
        added = [s for s in r["slots"] if s not in before]
        if added:
            item = dict(r)
            item["slots"] = added
            new_items.append(item)
    return new_items

def run_specified_dates(target_dates):
    """指定日モードの本体：取得→整形→通知。"""
    now = datetime.now().strftime("%Y/%m/%d %H:%M")
    print(f"=== 葛飾区施設チェック（指定日モード）{now} ===")
    print("対象日:", ", ".join(d.strftime("%Y/%m/%d(") + "月火水木金土日"[d.weekday()] + ")"
                              for d in target_dates), "\n")

    all_results, failures = run_for_dates(target_dates)
    result_text = format_results(all_results)
    failure_text = format_failures(failures)

    print("\n=== チェック完了 ===")
    print(result_text)
    if failure_text:
        print(failure_text)

    # --- 差分判定（1日5回実行のため、新しく出た枠がある時だけ通知する）---
    prev = load_state()
    first_run = prev is None
    new_items = diff_new_slots(all_results, prev)

    # 種目まるごと取得失敗は、空きが1件も無くても必ず知らせる（沈黙＝正常と誤認させないため）。
    # ただし前回とまったく同じ失敗内容なら、毎回同じ警告を送らない。
    purpose_failed = [f for f in failures if f.get("kind") == "purpose"]
    failure_sig = sorted(f"{f.get('purpose')}|{f.get('facility', '')}|{f.get('date', '')}"
                         for f in failures)
    failure_is_new = bool(purpose_failed) and (
        first_run or failure_sig != prev.get("failure_sig", []))

    if first_run and all_results:
        subject = f"【葛飾区施設】指定日の空き（{now}）"
        lead = "指定日の空き状況です（初回のため全件）。"
        main_text = result_text
        main_html = format_results_html(all_results)
        line_text = result_text
    elif new_items:
        subject = f"【葛飾区施設】新着の空き（{now}）"
        lead = "前回チェックから新しく出た空きです。"
        new_text = format_results(new_items)
        main_text = f"{new_text}\n--- 現在の全空き ---\n{result_text}"
        main_html = (f"{format_results_html(new_items)}"
                     f"<hr><p><b>現在の全空き</b></p>{format_results_html(all_results)}")
        line_text = new_text
    elif failure_is_new:
        subject = f"【葛飾区施設】⚠ チェック失敗あり（{now}）"
        lead = "一部の種目がチェックできていません。"
        main_text = result_text
        main_html = format_results_html(all_results)
        line_text = result_text
    else:
        subject = None
        if all_results:
            print("\n新着なし（前回と同じ空き枠のみ）→ 通知なし")
        else:
            print("\n指定日に空きなし → 通知なし")

    if subject:
        body = (
            f"{lead}\n\n"
            f"{main_text}\n"
            f"{failure_text}\n\n"
            f"予約はこちら:\n{BASE_URL}/menu.jsp"
        )
        html_body = (
            f'<p>{html.escape(lead)}</p>'
            f'{main_html}'
            f'{format_failures_html(failures)}'
            f'<p>予約はこちら:<br><a href="{BASE_URL}/menu.jsp">{BASE_URL}/menu.jsp</a></p>'
        )
    mail_ok = True
    if subject:
        mail_ok = send_email(subject, body, html_body)
        send_line(f"🏟 葛飾区・指定日\n\n{line_text}\n{failure_text}\n\n"
                  f"▶ 予約: {BASE_URL}/menu.jsp")

    # 本日の完走回数（生存確認メールに載せる）
    today = datetime.now().strftime("%Y-%m-%d")
    runs = (prev or {}).get("runs", {})
    runs = {"date": today, "count": (runs.get("count", 0) if runs.get("date") == today else 0) + 1}
    meta = {"runs": runs, "heartbeat_sent": (prev or {}).get("heartbeat_sent", "")}

    # 生存確認：23時の回だけ、1日1通「本日の巡回完了」を送る（2026-10-23）。
    # このメールが来ない日＝どこかで止まっている、と判断できるようにするため。
    heartbeat_ok = True
    if datetime.now().hour == HEARTBEAT_HOUR and meta["heartbeat_sent"] != today:
        heartbeat_ok = send_heartbeat(target_dates, all_results, failures, runs["count"])
        if heartbeat_ok:
            meta["heartbeat_sent"] = today

    if mail_ok:
        save_state(all_results, failures, meta=meta)
    else:
        # 通知できなかった枠を「通知済み」にしない。次回もう一度通知させる
        print("[状態] メール送信に失敗したため、空き枠は前回の状態のまま保存（次回に再通知）")
        save_state(all_results, failures, prev=prev, keep_prev=True, meta=meta)

    # GitHub Actions で失敗が赤く表示されるよう、メール送信の失敗は終了コード1にする
    if not (mail_ok and heartbeat_ok):
        sys.exit(1)


HEARTBEAT_HOUR = 23
RUNS_PER_DAY = 17


def send_heartbeat(target_dates, all_results, failures, run_count):
    """23時の生存確認メール。空きの有無に関係なく送る。"""
    now = datetime.now().strftime("%Y/%m/%d %H:%M")
    n_slots = sum(len(r["slots"]) for r in all_results)
    subject = f"【葛飾区施設】本日の巡回完了（{now}）"
    body = (
        "本日の巡回が完了しました。\n\n"
        f"本日完走した回数：{run_count}回（予定 {RUNS_PER_DAY}回）\n"
        f"今回の対象日：{len(target_dates)}日\n"
        f"現在の空き枠：{n_slots}件\n"
        f"チェックできなかったもの：{len(failures)}件\n\n"
        "このメールが届かない日は、どこかで巡回が止まっています。"
    )
    return send_email(subject, body)


# --- 前回の異常終了で取り残されたブラウザの掃除（2026-09-08 新設）-------------
# 2026-09-08、前回実行の chrome-headless-shell が孤児として4つ残っており、
# 次の実行の chromium.launch() が 180 秒でタイムアウトして丸ごと失敗した。
# 差分通知は「新着が無ければ無音」が正常なので、この失敗は気づかれにくい。
# そこで毎回の起動時に、前の実行が残した孤児だけを掃除する。
_PW_CACHE_MARK  = "ms-playwright"
_PW_DRIVER_MARK = "playwright/driver"
_OWNER_SCRIPTS  = ("checker.py", "kosuge_watch.py")


def _ps_snapshot():
    """pid -> (ppid, command) のスナップショットを返す。"""
    out = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,command="],
        capture_output=True, text=True, timeout=20,
    ).stdout
    table = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            table[int(parts[0])] = (int(parts[1]), parts[2])
        except ValueError:
            continue
    return table


def _is_playwright_proc(cmd):
    """Playwright が起動したブラウザ／ドライバか（他アプリの Chrome は対象外）。"""
    if _PW_DRIVER_MARK in cmd:
        return True
    return _PW_CACHE_MARK in cmd and "chrome-headless-shell" in cmd


def _is_owner_proc(cmd):
    """生きている checker.py / kosuge_watch.py の本体プロセスか。"""
    return "python" in cmd.lower() and any(s in cmd for s in _OWNER_SCRIPTS)


def cleanup_orphan_browsers(verbose=True):
    """
    前回の異常終了で取り残された Playwright のブラウザ／ドライバを終了させる。

    終了させるのは「先祖をたどっても誰にも所有されていない（pid 1 にぶら下がった）
    Playwright のプロセス」だけに限定する:
      - 実行中の checker.py / kosuge_watch.py の配下 → 残す
        （もう一方のタスクが同時に走っていても巻き込まないため）
      - 他アプリ配下の Chrome → そもそも判定対象にしない
    """
    try:
        table = _ps_snapshot()
    except Exception as e:
        print(f"  [掃除] プロセス一覧を取得できませんでした（掃除をスキップ）: {e}")
        return 0

    me = os.getpid()
    orphans = []
    for pid, (ppid, cmd) in table.items():
        if pid == me or not _is_playwright_proc(cmd):
            continue
        cur, hops, owned = ppid, 0, False
        while cur > 1 and hops < 30:
            parent = table.get(cur)
            if parent is None:
                break
            if _is_owner_proc(parent[1]) or not _is_playwright_proc(parent[1]):
                owned = True   # 実行中の本体、または他アプリの配下 → 触らない
                break
            cur, hops = parent[0], hops + 1
        if not owned:
            orphans.append(pid)

    if not orphans:
        return 0

    count = len(orphans)
    if verbose:
        print(f"  [掃除] 前回の残骸とみられるプロセスを {count} 件終了します: {sorted(orphans)}")

    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in orphans:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError:
                print(f"  [掃除] pid {pid} を終了できません（権限不足）")
        time.sleep(2)
        try:
            still = _ps_snapshot()
        except Exception:
            break
        orphans = [p for p in orphans if p in still]
        if not orphans:
            break

    if orphans and verbose:
        print(f"  [掃除] 終了できなかったプロセスが残っています: {sorted(orphans)}")
    elif verbose:
        print(f"  [掃除] 完了（{count} 件）")
    return count



def launch_browser(p, attempts=3, timeout_ms=120_000):
    """
    ブラウザを起動する。まれに起動そのものが固まることがある（2026-09-08 に
    180 秒でタイムアウトし、実行が丸ごと失敗した）。差分通知は「新着が無ければ
    無音」が正常なので黙って死ぬと気づけない。そこで数回リトライし、
    リトライ前に前回の残骸を掃除する。
    """
    last = None
    for i in range(1, attempts + 1):
        try:
            return p.chromium.launch(headless=True, timeout=timeout_ms)
        except Exception as e:
            last = e
            print(f"  [起動] ブラウザ起動に失敗（{i}/{attempts}）: {type(e).__name__}")
            if i < attempts:
                cleanup_orphan_browsers()
                time.sleep(5)
    raise last



def main():
    # 前回の異常終了で残ったブラウザを掃除する（放置すると launch が
    # タイムアウトし、無通知のまま失敗し続ける。2026-09-08）
    cleanup_orphan_browsers()

    # 引数の解釈:
    #   auto / auto:N … 本日〜N日後(既定30日)の土日祝を全部チェック（指定日モード）
    #   日付を列挙    … その日だけチェック（指定日モード）
    #   引数なし      … 従来の「全土日モード」（月カレンダーのみ・時間帯は出ない）
    args = sys.argv[1:]
    if args and args[0].lower().startswith("auto"):
        window = BOOKING_WINDOW_DAYS
        if ":" in args[0]:
            try:
                window = int(args[0].split(":", 1)[1])
            except ValueError:
                print(f"  窓日数として認識できません: {args[0]} → 既定{BOOKING_WINDOW_DAYS}日を使用")
        target_dates = auto_target_dates(window)
        if not target_dates:
            print(f"本日から{window}日以内に対象日（土日祝＋単発の指定日）がありません。")
            return
        run_specified_dates(target_dates)
        return
    if args:
        target_dates = parse_target_dates(args)
        if not target_dates:
            print("有効な日付がありません。例: python3 checker.py 2026-06-21 7/19")
            return
        run_specified_dates(target_dates)
        return

    now = datetime.now().strftime("%Y/%m/%d %H:%M")
    print(f"=== 葛飾区施設チェック開始 {now} ===\n")

    all_results = []
    failures = []

    with sync_playwright() as p:
        browser = launch_browser(p)
        page = new_fast_page(browser)  # 画像等をブロックして高速化（2026-09-07）

        # ---- フェーズ1: 月カレンダーで空き日を収集 ----
        for purpose_name in TARGET_PURPOSES:
            print(f"[{purpose_name}] 施設一覧を取得中...")
            facilities = []
            for attempt in range(3):
                try:
                    facilities = get_facilities_for_purpose(page, purpose_name)
                    if facilities:
                        break
                except Exception as e:
                    print(f"  → 取得リトライ({attempt+1}/3): {e}")
                    time.sleep(1)
            facilities = [(n, c) for (n, c) in facilities
                          if not any(x in n for x in EXCLUDE_FACILITIES)]
            print(f"  → {len(facilities)}施設")
            if not facilities:
                print(f"  → [種目取得失敗] {purpose_name} はチェックできませんでした")
                failures.append({"kind": "purpose", "purpose": purpose_name})
                continue

            frame = None
            for attempt in range(3):
                try:
                    frame = navigate_to_facility_calendar(page, purpose_name, facilities[0][0], facilities[0][1])
                    if frame is not None:
                        break
                except Exception as e:
                    print(f"  → ナビリトライ({attempt+1}/3): {e}")
                    time.sleep(1)
            if frame is None:
                print("  → ナビゲーション失敗")
                failures.append({"kind": "purpose", "purpose": purpose_name})
                continue

            for i, (fac_name, fac_code) in enumerate(facilities):
                print(f"  {fac_name} チェック中...")
                try:
                    if i > 0:
                        switch_facility(page, fac_code)
                    results = check_facility_two_months(page, fac_name, fac_code, purpose_name)
                    if results:
                        dates = ", ".join(sorted({r["date"] for r in results}))
                        print(f"    → 空きあり {len(results)}件 ({dates})")
                        all_results.extend(results)
                    else:
                        print("    → 空きなし")
                except Exception as e:
                    print(f"    → エラー: {e}")
                    failures.append({"kind": "slot", "purpose": purpose_name,
                                     "facility": fac_name, "date": "月カレンダー取得失敗"})

        # ---- フェーズ2: 空き日ごとに時間帯を取得し、本当に予約可能な枠だけ残す ----
        if all_results:
            print(f"\n[時間帯取得] {len(all_results)}件の日別空き枠を確認中...")
            for r in all_results:
                status, slots = fetch_timeslots(page, r["purpose"], r["facility"], r["code"],
                                                r["year"], r["month"], r["day"])
                r["slot_status"] = status
                r["slots"] = slots
                label = (", ".join(slots) if slots else
                         "予約可能枠なし（月表示のみ空き）" if status == "none" else "時間帯不明")
                print(f"  [{status}] {r['date']} {r['facility']}: {label}")

        browser.close()

    # status='none'（実際は予約不可）は除外。'ok'と'unknown'のみ通知
    dropped = [r for r in all_results if r.get("slot_status") == "none"]
    reportable = [r for r in all_results if r.get("slot_status") != "none"]

    print("\n=== チェック完了 ===")
    if dropped:
        print(f"※ 月表示は空きだが実際の予約可能枠なし（除外）: "
              + ", ".join(sorted({r['facility'] for r in dropped})))
    result_text = format_results(reportable)
    failure_text = format_failures(failures)
    print(result_text)
    if failure_text:
        print(failure_text)

    purpose_failed = [f for f in failures if f.get("kind") == "purpose"]

    all_results = reportable
    if all_results or purpose_failed:
        if all_results:
            subject = f"【葛飾区施設】土日祝に空きあり（{now}）"
            lead = "以下の施設に空きがあります。"
        else:
            subject = f"【葛飾区施設】⚠ チェック失敗あり・空きなし（{now}）"
            lead = "空きはありませんでしたが、一部チェックできていません。"
        body = (
            f"{lead}\n\n"
            f"{result_text}\n"
            f"{failure_text}\n\n"
            f"予約はこちら:\n{BASE_URL}/menu.jsp"
        )
        html_body = (
            f'<p>{html.escape(lead)}</p>'
            f'{format_results_html(all_results)}'
            f'{format_failures_html(failures)}'
            f'<p>予約はこちら:<br><a href="{BASE_URL}/menu.jsp">{BASE_URL}/menu.jsp</a></p>'
        )
        send_email(subject, body, html_body)
        send_line(
            f"🏟 葛飾区施設\n\n{result_text}\n{failure_text}\n\n▶ 予約: {BASE_URL}/menu.jsp"
        )
    else:
        print("\n空き情報なし → 通知なし")


if __name__ == "__main__":
    main()
