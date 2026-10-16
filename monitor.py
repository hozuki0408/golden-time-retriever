"""Golden Time Retriever (cloud) - メルカリ売り切れ検知 → eBay在庫0

GitHub Actions から30分ごとに実行される。
商品リストは Google スプレッドシート（Apps Script 経由）から読み書きする。
秘密情報はすべて GitHub の Secrets から環境変数として受け取る。
"""
import base64
import json
import os
import random
import re
import statistics
import sys
import time
import unicodedata
import math
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

APP_ID = os.environ["EBAY_APP_ID"]
CERT_ID = os.environ["EBAY_CERT_ID"]
REFRESH_TOKEN = os.environ["EBAY_REFRESH_TOKEN"]
SHEET_API_URL = os.environ["SHEET_API_URL"]
SHEET_API_KEY = os.environ["SHEET_API_KEY"]
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

SCOPES = " ".join([
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
])
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
TRADING_URL = "https://api.ebay.com/ws/api.dll"
NS = {"e": "urn:ebay:apis:eBLBaseComponents"}
JST = ZoneInfo("Asia/Tokyo")


def now():
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M")


def notify(msg):
    # 公開リポジトリの実行記録は誰でも見られるため、通知の中身は記録に出さない
    if DISCORD_WEBHOOK:
        try:
            requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=15)
        except Exception:
            print("Discord通知に失敗しました")


# ---------- スプレッドシート ----------
def sheet(action, **payload):
    # Googleの応答がたまに壊れる（書き込み自体は成功していることが多い）ので、数回やり直す
    for attempt in range(3):
        try:
            r = requests.post(
                SHEET_API_URL,
                data=json.dumps({"key": SHEET_API_KEY, "action": action, **payload}),
                headers={"Content-Type": "text/plain"},
                timeout=60,
            )
            data = r.json()
            break
        except (ValueError, requests.RequestException):
            if action == "append":      # 追加は二重にならないよう、やり直さない
                print("スプレッドシートの応答が読めませんでした（追加）")
                return {}
            time.sleep(5 * (attempt + 1))
    else:
        if action in ("update", "log", "cleanup"):
            print(f"スプレッドシートの応答が読めませんでした（{action}）。続行します")
            return {}
        raise RuntimeError("スプレッドシートの応答が読めません（3回やり直しても失敗）")
    if data.get("error"):
        raise RuntimeError(f"スプレッドシートのエラー: {data['error']}")
    return data


# ---------- eBay ----------
def get_access_token():
    auth = "Basic " + base64.b64encode(f"{APP_ID}:{CERT_ID}".encode()).decode()
    r = requests.post(
        TOKEN_URL,
        headers={"Authorization": auth, "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN, "scope": SCOPES},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"eBayトークン更新に失敗 ({r.status_code}): {r.text[:300]}")
    return r.json()["access_token"]


def trading_call(call_name, body_xml, token):
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<{call_name}Request xmlns="urn:ebay:apis:eBLBaseComponents">{body_xml}</{call_name}Request>'
    )
    r = requests.post(
        TRADING_URL,
        headers={
            "X-EBAY-API-CALL-NAME": call_name,
            "X-EBAY-API-SITEID": "0",
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1209",
            "X-EBAY-API-IAF-TOKEN": token,
            "Content-Type": "text/xml",
        },
        data=xml.encode("utf-8"),
        timeout=30,
    )
    root = ET.fromstring(r.content)
    ack = root.findtext("e:Ack", default="", namespaces=NS)
    errors = [e.findtext("e:LongMessage", default="", namespaces=NS) for e in root.findall("e:Errors", NS)]
    return ack, errors


def stop_on_ebay(item_id, token):
    """在庫がなくなったらeBayの出品を終了する（終了できないときだけ在庫0にする）"""
    ack, errors = trading_call(
        "EndFixedPriceItem",
        f"<ItemID>{item_id}</ItemID><EndingReason>NotAvailable</EndingReason>",
        token,
    )
    if ack in ("Success", "Warning"):
        return True, "eBayの出品を終了しました"
    if any(re.search(r"already (been )?(ended|closed)|has ended|終了", e, re.I) for e in errors):
        return True, "eBayの出品はすでに終了しています"
    ack2, errors2 = trading_call(
        "ReviseInventoryStatus",
        f"<InventoryStatus><ItemID>{item_id}</ItemID><Quantity>0</Quantity></InventoryStatus>",
        token,
    )
    if ack2 in ("Success", "Warning"):
        return True, "出品を終了できず、eBay在庫を0にしました"
    return False, f"eBay停止に失敗: {' / '.join(errors + errors2)[:200]}"


# ---------- メルカリ ----------
JUDGE_JS = """() => {
    const text = document.body.innerText;
    if (/削除されています|見つかりません|公開停止/.test(text)) return 'deleted';
    // メルカリオークションは仕入れ値が確定しないので対象外
    if (document.querySelector('[data-testid="bid-button"], [data-testid="bid-button-container"]') ||
        text.includes('オークション商品')) return 'auction';
    // メルカリShops: 在庫0でも非表示の「購入手続きへ」が残るため、専用の目印で判定
    if (document.querySelector('[data-testid="out-of-stock"], [data-testid="disabled-purchase-button"]'))
        return 'sold';
    const live = [...document.querySelectorAll('button, a')].some(b =>
        (b.innerText || '').includes('購入手続きへ') && !b.disabled &&
        b.getAttribute('aria-disabled') !== 'true' && b.offsetParent !== null);
    if (live) return 'on_sale';
    if (text.includes('売り切れ')) return 'sold';
    return 'unknown';
}"""


def check_mercari(page, url):
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception:
        return "unknown", {}
    if resp is not None and resp.status == 404:
        return "deleted", {}
    if resp is not None and resp.status in (403, 429):
        return "blocked", {}
    try:
        page.wait_for_function(
            """() => { const t = document.body ? document.body.innerText : '';
                return /購入手続きへ|売り切れ|削除されています|見つかりません|公開停止/.test(t); }""",
            timeout=25000,
        )
    except Exception:
        return "unknown", {}
    page.wait_for_timeout(600)
    status = page.evaluate(JUDGE_JS)
    info = page.evaluate(INFO_JS) if status in ("on_sale", "sold") else {}
    return status, info


DETAIL_JS = """() => ({
    images: [...document.querySelectorAll('[data-testid^="image-"] img')].map(i => i.currentSrc || i.src)
              .filter((v, i, a) => v && a.indexOf(v) === i).slice(0, 12),
    description: (document.querySelector('[data-testid="description"]') || {}).innerText || '',
    condition: ((document.querySelector('[data-testid="商品の状態"]') || {}).innerText || '').split('\\n')[0],
    category: [...document.querySelectorAll('a[href*="category_id="]')].map(a => a.innerText.trim()).filter(Boolean).join(' > '),
})"""

INFO_JS = """() => {
    const title = (document.querySelector('h1')?.innerText || document.title.replace(/ - メルカリ.*$/, '')).trim();
    const main = (document.querySelector('main') || document.body).innerText;
    const head = main.split('商品の説明')[0];
    const m = head.match(/¥\\s*([\\d,]+)/);
    return { title, price: m ? parseInt(m[1].replace(/,/g, ''), 10) : null };
}"""

# ---------- 仕入れ候補の利益計算 ----------
_app_token = None


def app_token():
    """出品者の権限が要らない検索用のトークン"""
    global _app_token
    if _app_token:
        return _app_token
    auth = "Basic " + base64.b64encode(f"{APP_ID}:{CERT_ID}".encode()).decode()
    r = requests.post(
        TOKEN_URL,
        headers={"Authorization": auth, "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=30,
    )
    r.raise_for_status()
    _app_token = r.json()["access_token"]
    return _app_token


MODEL_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,4}-?\d{2,4}[A-Za-z]{0,2})(?![A-Za-z0-9])")


KNOWN_MODELS = []   # 設定シートの検索キーワード（型番）。商品名にあればそれを優先
DIGIT_MODEL_RE = re.compile(r"(?<![A-Za-z0-9])(A?\d{3}(?:KC|SH))(?![A-Za-z0-9])", re.I)   # 903KC・602SH・A203KC など


def _flat(s):
    return re.sub(r"[-\s‐－ー]", "", unicodedata.normalize("NFKC", s or "")).upper()


def has_model(text, model):
    """文章に型番が含まれるか（KY42C・KY-42C・NP902KC・V602SH は一致、3902KC・0805SHISEIDO は不一致）"""
    q = re.sub(r"[-\s‐－ー]", "", unicodedata.normalize("NFKC", model or "")).upper()
    if len(q) < 4:
        return False
    pat = r"[-\s‐－ー]?".join(re.escape(c) for c in q)
    t = unicodedata.normalize("NFKC", text or "").upper()
    return re.search(r"(?<![0-9])" + pat + r"(?![A-Z0-9])", t) is not None


def guess_query(title):
    """商品名から型番らしき文字列を拾う（例：KY-42C、SH-02L、903KC、SO903i）"""
    t = unicodedata.normalize("NFKC", title or "")
    for k in KNOWN_MODELS:
        if has_model(t, k):   # 「0805SHISEIDO」のような別商品の品番を型番と間違えない
            return k
    m = DIGIT_MODEL_RE.search(t)
    if m:
        return m.group(1).upper()
    for m in MODEL_RE.finditer(t):
        code = m.group(1)
        if re.search(r"[A-Za-z]", code) and len(re.findall(r"\d", code)) >= 2 and len(code.replace("-", "")) >= 4:
            return code
    return ""


_market_cache = {}


def ebay_market_usd(query):
    """eBay(US)で同じ型番の中古・即決出品の価格(送料込み)の中央値と件数（携帯電話カテゴリ）"""
    if query in _market_cache:
        return _market_cache[query]
    _market_cache[query] = _ebay_market_usd(query)
    return _market_cache[query]


MY_SELLER = "golden_time_retriever"

# ---------- 携帯ゲーム機（ガラケーと同じ仕組みで扱う） ----------
# mercari: メルカリの本体カテゴリ、ebay_cat: eBayのカテゴリ（Video Game Consoles）
GAMES = {
    "PSP-1000": {"brand": "Sony", "platform": "Sony PSP", "series": "Sony PSP-1000", "mercari": "7076"},
    "PSP-2000": {"brand": "Sony", "platform": "Sony PSP", "series": "Sony PSP-2000", "mercari": "7076"},
    "PSP-3000": {"brand": "Sony", "platform": "Sony PSP", "series": "Sony PSP-3000", "mercari": "7076"},
    "PCH-1000": {"brand": "Sony", "platform": "Sony PlayStation Vita", "series": "Sony PS Vita PCH-1000", "mercari": "7058"},
    "PCH-2000": {"brand": "Sony", "platform": "Sony PlayStation Vita", "series": "Sony PS Vita PCH-2000", "mercari": "7058"},
    # 任天堂の携帯機：メルカリは機種ごとの本体カテゴリ（exact）、Yahoo!フリマは呼び名（alias）で見分ける
    "DMG-01": {"brand": "Nintendo", "platform": "Nintendo Game Boy", "series": "Nintendo Game Boy DMG-01",
               "mercari": "7181", "exact": True, "yahoo_q": "ゲームボーイ 本体", "ebay_q": "Game Boy DMG-01",
               "alias": r"ゲームボーイ|GAME ?BOY|DMG", "neg": r"ポケット|カラー|アドバンス|ミクロ|ライト|POCKET|COLOR|ADVANCE|MICRO|GBA|GBC|SP"},
    "MGB-001": {"brand": "Nintendo", "platform": "Nintendo Game Boy Pocket", "series": "Nintendo Game Boy Pocket MGB-001",
                "mercari": "8907", "exact": True, "yahoo_q": "ゲームボーイポケット", "ebay_q": "Game Boy Pocket",
                "alias": r"ゲームボーイ\s*ポケット|GAME ?BOY ?POCKET|MGB", "neg": r"ライト|カラー|LIGHT|COLOR"},
    "CGB-001": {"brand": "Nintendo", "platform": "Nintendo Game Boy Color", "series": "Nintendo Game Boy Color CGB-001",
                "mercari": "8908", "exact": True, "yahoo_q": "ゲームボーイカラー", "ebay_q": "Game Boy Color CGB-001",
                "alias": r"ゲームボーイ\s*カラー|GAME ?BOY ?COLOU?R|GBC|CGB", "neg": r"アドバンス|ADVANCE"},
    "AGB-001": {"brand": "Nintendo", "platform": "Nintendo Game Boy Advance", "series": "Nintendo Game Boy Advance AGB-001",
                "mercari": "7174", "exact": True, "yahoo_q": "ゲームボーイアドバンス", "ebay_q": "Game Boy Advance AGB-001",
                "alias": r"アドバンス|ADVANCE|GBA|AGB", "neg": r"SP|ミクロ|MICRO|AGS|OXY"},
    "AGS-001": {"brand": "Nintendo", "platform": "Nintendo Game Boy Advance SP", "series": "Nintendo Game Boy Advance SP AGS-001",
                "mercari": "8904", "exact": True, "yahoo_q": "ゲームボーイアドバンスSP", "ebay_q": "Game Boy Advance SP",
                "alias": r"アドバンス\s*SP|ADVANCE ?SP|GBA ?SP|AGS", "neg": r"$^"},
    "OXY-001": {"brand": "Nintendo", "platform": "Nintendo Game Boy Micro", "series": "Nintendo Game Boy Micro OXY-001",
                "mercari": "7178", "exact": True, "yahoo_q": "ゲームボーイミクロ", "ebay_q": "Game Boy Micro",
                "alias": r"ミクロ|MICRO|OXY", "neg": r"$^"},
    "USG-001": {"brand": "Nintendo", "platform": "Nintendo DS Lite", "series": "Nintendo DS Lite USG-001",
                "mercari": "8899", "exact": True, "yahoo_q": "DS Lite", "ebay_q": "Nintendo DS Lite",
                "alias": r"DS\s*(LITE|ライト)|DSL|USG", "neg": r"3DS|DSI"},
    "TWL-001": {"brand": "Nintendo", "platform": "Nintendo DSi", "series": "Nintendo DSi TWL-001",
                "mercari": "8898", "exact": True, "yahoo_q": "DSi", "ebay_q": "Nintendo DSi TWL-001",
                "alias": r"DSI|TWL", "neg": r"3DS|LL|XL"},
    "CTR-001": {"brand": "Nintendo", "platform": "Nintendo 3DS", "series": "Nintendo 3DS CTR-001",
                "mercari": "7022", "exact": True, "yahoo_q": "3DS", "ebay_q": "Nintendo 3DS CTR-001", "region_lock": True,
                "alias": r"3DS|CTR", "neg": r"NEW|ニュー|LL|XL|2DS"},
    "SPR-001": {"brand": "Nintendo", "platform": "Nintendo 3DS LL", "series": "Nintendo 3DS LL SPR-001",
                "mercari": "7038", "exact": True, "yahoo_q": "3DS LL", "ebay_q": "Nintendo 3DS LL SPR-001", "region_lock": True,
                "alias": r"3DS\s*(LL|XL)|SPR", "neg": r"NEW|ニュー|2DS"},
    "RED-001": {"brand": "Nintendo", "platform": "New Nintendo 3DS LL", "series": "New Nintendo 3DS LL RED-001",
                "mercari": "8882", "exact": True, "yahoo_q": "New 3DS LL", "ebay_q": "New Nintendo 3DS LL", "region_lock": True,
                "alias": r"(NEW|ニュー)\s*(ニンテンドー)?\s*3DS\s*(LL|XL)|RED-001", "neg": r"2DS"},
}


def match_game(title, key):
    """商品名がその機種か（型番が書いてあるか、呼び名が合っていて別機種の名前がない）"""
    gi = GAMES.get(key)
    t = unicodedata.normalize("NFKC", title or "").upper()
    if has_model(t, key):
        return True
    if not gi or not gi.get("alias"):
        return False
    return bool(re.search(gi["alias"], t)) and not re.search(gi.get("neg", r"$^"), t)


# ゲーム機は「本体カテゴリ」に部品・箱・ソフト・チラシなどが混ざるので、商品名を厳しく確認する
GAME_HARD_WORDS = re.compile(
    r"フェイスプレート|プレートのみ|チラシ|広告|ポスター|カタログ|攻略本|非売品|販促|ステッカー|シール|スキン|"
    r"交換用|修理用|部品取り|パーツ|ドライバー|互換|外箱|内箱|空箱|箱のみ|シェル|外装|ハウジング|"
    r"ソフト|カセット|UMD|説明書|取説|充電器|ACアダプタ|ケーブル|ポーチ|ケース|カバー|フィルム|"
    r"動作未確認|未確認|ジャンク|故障|不動|訳あり|難あり|画面割れ|液晶割れ")
GAME_MIN_PRICE_RATIO = 0.12   # 相場のこれ未満の値段は部品・ジャンクの可能性が高い


def game_reject_reason(title, model, price_jpy=None, sale_usd=None, settings=None):
    """ゲーム機として出品してはいけない理由（なければ空文字）"""
    t = unicodedata.normalize("NFKC", title or "")
    if not match_game(t, model):
        return "商品名に機種名がありません"
    words = [w for w in GAME_HARD_WORDS.findall(t)
             if not (w in ("ソフト", "カセット", "UMD", "説明書", "取説", "充電器", "ACアダプタ", "ケーブル",
                           "ポーチ", "ケース", "カバー", "フィルム", "外箱", "内箱")
                     and "本体" in t and "のみ" not in t)]   # 「本体＋ソフト付き」「本体 箱付き」は本体
    if words:
        return f"本体ではない可能性（{words[0]}）"
    if re.search(r"(?<!本体)のみ", t):
        return "本体以外の単品の可能性（〜のみ）"
    try:
        if price_jpy and sale_usd and float(price_jpy) < float(sale_usd) * usd_jpy(settings or {}) * GAME_MIN_PRICE_RATIO:
            return "相場に比べて安すぎる（部品・ジャンクの可能性）"
    except (TypeError, ValueError):
        pass
    return ""
GAME_EBAY_CATEGORY = "139971"
MAX_COST_DEFAULT = 20000      # 仕入れ値の上限（円）。設定シートで max_cost を渡せばそちらを優先
PROFIT_RATIO = 0.3            # 値下げしても「仕入れ値の30%」と最低利益の高いほうは残す


def game_info(model):
    key = re.sub(r"[-\s]", "", (model or "").upper())
    for k, v in GAMES.items():
        if k.replace("-", "") == key:
            return v
    return None


def max_cost(settings):
    try:
        return float(settings.get("max_cost") or MAX_COST_DEFAULT)
    except (TypeError, ValueError):
        return MAX_COST_DEFAULT


def min_profit_for(cost, settings):
    return max(float(settings.get("min_profit") or 2000), float(cost or 0) * PROFIT_RATIO)


def _ebay_market_usd(query):
    r = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        params={"q": (game_info(query) or {}).get("ebay_q", query),
                "category_ids": GAME_EBAY_CATEGORY if game_info(query) else "9355",
                "filter": "buyingOptions:{FIXED_PRICE},conditions:{USED}", "limit": 50},
        headers={"Authorization": f"Bearer {app_token()}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        timeout=30,
    )
    r.raise_for_status()
    prices = []
    for it in r.json().get("itemSummaries", []):
        if (it.get("seller", {}).get("username") or "").lower() == MY_SELLER:
            continue   # 自分の出品は相場に入れない
        try:
            p = float(it["price"]["value"])
            ship = it.get("shippingOptions", [{}])[0].get("shippingCost", {}).get("value")
            prices.append(p + (float(ship) if ship else 0))
        except (KeyError, ValueError, IndexError):
            continue
    if not prices:
        return None, 0
    return round(statistics.median(prices), 2), len(prices)


_fx = None


def usd_jpy(settings):
    global _fx
    manual = str(settings.get("fx") or "").strip()
    if manual:
        return float(manual)
    if _fx is None:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=30)
        r.raise_for_status()
        _fx = float(r.json()["rates"]["JPY"])
    return _fx


def adj(sale_usd, settings):
    """出品中の価格は実際に売れる価格より高めなので、補正をかける"""
    return float(sale_usd) * float(settings.get("market_adj") or 80) / 100


def profit(cost_jpy, sale_usd, settings):
    sale_usd = adj(sale_usd, settings)
    fx = usd_jpy(settings)
    fee = float(settings.get("fee_pct") or 15) + float(settings.get("other_pct") or 2)
    ship = float(settings.get("ship_jpy") or 3000)
    revenue = sale_usd * fx
    p = revenue * (1 - fee / 100) - ship - cost_jpy
    return round(p), (round(p / revenue * 100, 1) if revenue else 0)


LABEL = {"on_sale": "販売中", "sold": "売り切れ", "deleted": "削除・非公開",
         "unknown": "判定できず", "blocked": "メルカリにアクセスを拒否された",
         "auction": "オークション商品"}
MIN_COUNT = 10   # eBayの相場の件数がこれ未満の機種は候補にしない


def main():
    data = sheet("list")
    rows, settings = data["rows"], data.get("settings", {})
    KNOWN_MODELS[:] = [k.strip() for k in re.split(r"[,、\n]", str(settings.get("keywords") or "")) if k.strip()]
    targets = [r for r in rows if r["status"] in ("", "active")]
    watch = [r for r in targets if r["ebay_item_id"]]
    research = [r for r in targets if not r["ebay_item_id"]]
    print(f"{now()} 監視 {len(watch)}件／仕入れ候補 {len(research)}件")
    if not targets:
        sheet("log", message=f"{now()} 正常終了：対象の商品なし")
        return

    # eBayの認証を最初に確認しておく（売り切れを見つけてから失敗するのを防ぐ）
    token = get_access_token() if watch else None
    updates, blocked, counts, stopped, researched, listed = [], 0, {}, 0, 0, 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            locale="ja-JP",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        )
        # 高速化：画像・動画・フォントは読み込まない（判定に不要。写真のURLはページ内に残る）
        page.route("**/*", lambda route: route.abort()
                   if route.request.resource_type in ("image", "media", "font") else route.continue_())
        for r in targets:
            yahoo = is_yahoo(r["mercari_url"])
            if yahoo:
                status, info, ydetail = check_yahoo(r["mercari_url"])
            else:
                status, info = check_mercari(page, r["mercari_url"])
            counts[status] = counts.get(status, 0) + 1
            upd = {"row": r["row"], "last_checked": now(), "result": LABEL[status]}

            title_now = (info or {}).get("title") or r.get("title") or ""
            acc = is_accessory(title_now, (info or {}).get("price"), r.get("ebay_usd"), settings)
            if not acc and game_info(r.get("query") or ""):
                acc = bool(game_reject_reason(title_now, (r.get("query") or "").strip(),
                                              (info or {}).get("price"), r.get("ebay_usd"), settings))
            if r["ebay_item_id"]:
                # ---- 出品中の商品：売り切れたらeBayを止める ----
                if acc:
                    token = token or get_access_token()
                    ok, msg = stop_on_ebay(r["ebay_item_id"], token)
                    upd["result"] = f"説明書・充電器などの単体のため出品を止めました → {msg}"
                    if ok:
                        upd["status"] = "stopped"
                        stopped += 1
                elif status in ("sold", "deleted"):
                    ok, msg = stop_on_ebay(r["ebay_item_id"], token)
                    upd["result"] = f"{LABEL[status]} → {msg}"
                    if ok:
                        upd["status"] = "stopped"
                        stopped += 1
                        notify(f"【停止】{r.get('memo','')} メルカリ{LABEL[status]} → {msg}（eBay {r['ebay_item_id']}）")
                    else:
                        notify(f"【要対応】{r.get('memo','')} メルカリ{LABEL[status]}ですが{msg}（eBay {r['ebay_item_id']}）")
                elif status == "on_sale" and info.get("price"):
                    # 出品中の値段を相場に合わせる（ただし利益が最低額を割らない値段まで）
                    try:
                        target, floor = market_price_usd(info["price"], (r.get("query") or "").strip(), settings)
                        if target is not None:
                            new_price = max(target, floor)
                            # 実際の出品価格で売れたときの利益をシートに書く
                            fx = usd_jpy(settings)
                            fee = (float(settings.get("fee_pct") or 15) + float(settings.get("other_pct") or 2)) / 100
                            rev = new_price * fx
                            p = rev * (1 - fee) - float(settings.get("ship_jpy") or 3000) - info["price"]
                            upd["cost"] = info["price"]
                            upd["profit"] = round(p)
                            upd["margin"] = round(p / rev * 100, 1) if rev else 0
                            if f"${new_price:.2f}" in (r.get("result") or ""):   # 値段が同じなら何もしない
                                upd["result"] = r["result"]
                            else:
                                ack, errs = trading_call("ReviseInventoryStatus",
                                    f"<InventoryStatus><ItemID>{r['ebay_item_id']}</ItemID>"
                                    f"<StartPrice>{new_price:.2f}</StartPrice></InventoryStatus>", token)
                                if ack in ("Success", "Warning"):
                                    upd["result"] = f"販売中（eBay ${new_price:.2f}）"
                    except Exception:
                        pass
            else:
                # ---- 仕入れ候補：商品名・価格・相場・利益を埋める ----
                if acc:
                    upd["result"] = "説明書・充電器などの単体のため候補から外しました"
                    upd["status"] = "stopped"
                    upd["list"] = False
                elif status in ("sold", "deleted", "auction"):
                    upd["result"] = f"{LABEL[status]}（候補から外しました）"
                    upd["status"] = "stopped"
                elif status == "on_sale" and info:
                    # ---- 販売中の候補はすべてeBayに出品（「出品する」のチェックは不要） ----
                    try:
                        token = token or get_access_token()
                        detail = ydetail if yahoo else page.evaluate(DETAIL_JS)
                        res, err = create_listing(r, info, detail, settings, token)
                    except Exception as e:
                        res, err = None, f"出品中にエラー：{str(e)[:200]}"
                    upd["list"] = False
                    if res:
                        upd["ebay_item_id"] = res["ebay_item_id"]
                        upd["result"] = f"eBayに出品しました（${res['price']:.2f}）→ 売り切れ監視を開始"
                        listed += 1
                        notify(f"【出品】{res['title_en']} ${res['price']:.2f}（eBay {res['ebay_item_id']}）")
                    elif err and err.startswith("SKIP:"):
                        upd["result"] = err[5:]
                        upd["status"] = "stopped"   # 相場では利益が出ない → 候補から外す（自動で行を削除）
                    else:
                        # 失敗理由が次の確認で上書きされないよう「paused」にして残す
                        upd["result"] = f"【出品できず】{err}"
                        upd["status"] = "paused"
                        notify(f"【出品できず】{info.get('title','')[:30]}：{err}")
                elif status == "on_sale" and info:
                    try:
                        upd.update(research_row(r, info, settings))
                        researched += 1
                        # 利益が少ない・相場の件数が少ないものは候補から外す
                        min_profit = float(settings.get("min_profit") or 2000)
                        low_profit = "profit" in upd and upd["profit"] <= min_profit
                        few = upd.get("ebay_count") not in (None, "") and int(upd["ebay_count"]) < MIN_COUNT
                        if low_profit or few:
                            upd["status"] = "stopped"
                            upd["result"] = "利益が少ないため候補から外しました" if low_profit else "相場の件数が少ないため候補から外しました"
                    except Exception as e:
                        upd["result"] = f"販売中（相場の取得に失敗：{str(e)[:60]}）"

            if status == "blocked":
                blocked += 1
            elif status == "unknown":
                notify(f"【確認して】商品ページの状態を判定できませんでした：{r['mercari_url']}")
            updates.append(upd)
            if len(updates) >= 10:   # 途中で止まっても結果が残るよう、10件ごとにシートへ書く
                sheet("update", updates=updates)
                updates = []
            time.sleep(random.uniform(0.5, 1.5) if yahoo else random.uniform(1.5, 3))
        browser.close()

    sheet("update", updates=updates)
    try:
        sheet("cleanup")   # 外した候補（eBay未出品）の行を削除
    except Exception:
        pass
    if blocked:
        notify(f"【注意】メルカリにアクセスを拒否されました（{blocked}件）。クラウドからのアクセスが制限されている可能性があります。")
    # 記録には件数だけを出す（URLや出品番号は出さない）
    summary = "、".join(f"{LABEL[k]} {v}件" for k, v in counts.items())
    msg = f"{now()} 正常終了：{summary}／eBay停止 {stopped}件／利益計算 {researched}件／出品 {listed}件"
    print(msg)
    sheet("log", message=msg)


def research_row(r, info, settings):
    out = {"title": info.get("title") or r.get("title", "")}
    cost = info.get("price")
    if cost:
        out["cost"] = cost
    query = (r.get("query") or "").strip() or guess_query(out["title"])
    out["query"] = query
    sale = r.get("ebay_usd")
    if not sale and query:
        sale, n = ebay_market_usd(query)
        out["ebay_usd"] = sale or ""
        out["ebay_count"] = n
    if not query:
        out["result"] = "販売中（型番が見つからず相場不明。検索語の列に型番を入れてください）"
    elif not sale:
        out["result"] = "販売中（eBayに同じ型番の出品が見つかりません）"
    elif cost:
        p, rate = profit(cost, float(sale), settings)
        out["profit"] = p
        out["margin"] = rate
        out["result"] = "販売中"
    return out


# ---------- eBayへの出品 ----------
BRANDS = [(r"^(KYF|KYV|KY-|KY)", "Kyocera"), (r"^(SHF|SHV|SH-|SH)", "Sharp"), (r"SH$", "Sharp"), (r"KC$", "Kyocera"),
          (r"^P-?\d", "Panasonic"), (r"^N-?\d", "NEC"), (r"^F-?\d", "Fujitsu"), (r"^SO-?\d", "Sony Ericsson"),
          (r"^CA-?\d", "Casio"), (r"^SA-?\d", "Sanyo"), (r"^D-?\d", "Mitsubishi"), (r"^L-?\d", "LG")]
COLORS = {"ブルーブラック": "Blue Black", "ワインレッド": "Red", "ボルドー": "Red", "ブラウン": "Brown", "茶": "Brown",
          "ライトブルー": "Blue", "水色": "Blue", "ローズ": "Pink", "ベージュ": "Beige", "シャンパン": "Gold",
          "black": "Black", "white": "White", "blue": "Blue", "red": "Red", "pink": "Pink", "silver": "Silver",
          "ブラック": "Black", "黒": "Black", "ホワイト": "White", "白": "White", "ブルー": "Blue", "青": "Blue",
          "ネイビー": "Navy", "レッド": "Red", "赤": "Red", "ピンク": "Pink", "シルバー": "Silver", "ゴールド": "Gold",
          "グリーン": "Green", "緑": "Green", "パープル": "Purple", "オレンジ": "Orange", "イエロー": "Yellow", "グレー": "Gray"}


def brand_of(model, title):
    m = model.upper()
    for pat, name in BRANDS:
        if re.search(pat, m):
            return name
    for name in ["Kyocera", "Sharp", "Panasonic", "NEC", "Fujitsu", "Sony", "Casio"]:
        if name.lower() in title.lower():
            return name
    return "Unbranded"


def carrier_of(model, text):
    t = text.lower()
    if "docomo" in t or "ドコモ" in text:
        return "Docomo"
    if re.search(r"(^|[^a-z])au([^a-z]|$)", t):
        return "au"
    if "softbank" in t or "ソフトバンク" in text:
        return "SoftBank"
    m = model.upper()
    if re.match(r"^[A-Z]{1,2}-\d{2}[A-Z]$", m):
        return "Docomo"
    if re.match(r"^(KYF|SHF|KYV|SHV)", m):
        return "au"
    if re.match(r"^(A\d{3}|\d{3})[A-Z]{2}$", m):
        return "SoftBank"
    return ""


# 型番ごとの正式なシリーズ名（英語タイトルを統一するため）
SERIES = {
    "902KC": "Kyocera DIGNO Keitai 3", "701KC": "Kyocera DIGNO Keitai 2", "A202KC": "Kyocera DIGNO Keitai 4",
    "KY-42C": "Kyocera DIGNO Keitai", "KYF31": "Kyocera GRATINA 4G", "KYF37": "Kyocera GRATINA 4G",
    "KYF39": "Kyocera GRATINA", "KYF42": "Kyocera GRATINA", "KYF38": "Kyocera TORQUE X01",
    "601SH": "Sharp AQUOS Keitai 2", "805SH": "Sharp AQUOS Keitai 3", "A205SH": "Sharp AQUOS Keitai 4",
    "A206SH": "Sharp AQUOS Keitai 4", "SH-01J": "Sharp AQUOS Keitai", "SH-02L": "Sharp AQUOS Keitai",
    "SH-02K": "Sharp AQUOS Keitai", "F-01G": "Fujitsu Raku-Raku Phone", "F-03L": "Fujitsu Raku-Raku Phone",
    "P-01G": "Panasonic", "P-01H": "Panasonic", "P-01J": "Panasonic",
    "903KC": "Kyocera DIGNO Keitai", "A203KC": "Kyocera DIGNO Keitai", "602SH": "Sharp AQUOS Keitai",
}


# 本体の内蔵メモリ（4Gガラホはほぼ8GB）
STORAGE = {"F-01G": "Less than 1 GB", "F-08C": "Less than 1 GB", "P-01G": "Less than 1 GB"}


def storage_of(model):
    m = model.upper()
    if m in STORAGE:
        return STORAGE[m]
    if re.match(r"^(\d{3}|A\d{3})(KC|SH)$|^KY-42C$|^KYF(3[1-9]|4\d)$|^SH-0[12][JKL]$|^SH-32C$|^SHF3\d$", m):
        return "8 GB"
    return "Less than 1 GB"


def build_title(model, title, carrier, unlocked, color):
    """どの商品も同じ形：<メーカー シリーズ> <型番> 4G Flip Phone <SIMフリー/キャリア> <色> Japan"""
    m = model.upper()
    series = SERIES.get(m) or brand_of(model, title)
    net = "SIM Unlocked" if unlocked else (carrier or "")
    gen = "4G " if re.match(r"^(\d{3}|A\d{3})(KC|SH)$|^KY-42C$|^KYF(3[1-9]|4\d)$|^SH-0[12][JKL]$", m) and "4G" not in series else ""
    parts = [series, model, f"{gen}Flip Phone", net, color, "Japan"]
    t = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
    if len(t) > 80:   # 長すぎたら色→Japanの順に削る
        for drop in (color, "Japan"):
            if drop and len(t) > 80:
                t = re.sub(r"\s+", " ", t.replace(f" {drop}", "", 1)).strip()
    return t[:80]


def market_price_usd(cost, query, settings):
    """売れている相場に合わせた値段。利益が最低額（設定の最低利益）を下回るなら None"""
    sale, n = ebay_market_usd(query) if query else (None, 0)
    if not sale:
        return None, None
    fx = usd_jpy(settings)
    fee = (float(settings.get("fee_pct") or 15) + float(settings.get("other_pct") or 2)) / 100
    ship = float(settings.get("ship_jpy") or 3000)
    min_profit = min_profit_for(cost, settings)
    floor = math.ceil((cost + ship + min_profit) / max(0.05, 1 - fee) / fx) - 0.01   # 最低利益が残る最低価格
    target = math.floor(adj(sale, settings)) + 0.99                                  # 実際に売れている値段の目安
    return target, floor


def list_price_usd(cost, settings):
    fx = usd_jpy(settings)
    fee = (float(settings.get("fee_pct") or 15) + float(settings.get("other_pct") or 2)) / 100
    margin = float(settings.get("target_margin") or 20) / 100
    ship = float(settings.get("ship_jpy") or 3000)
    revenue = (cost + ship) / max(0.05, 1 - fee - margin)
    return math.ceil(revenue / fx) - 0.01


def upload_pictures(urls, token):
    out = []
    for u in urls:
        ack, errors, root = trading_call_root(
            "UploadSiteHostedPictures",
            f"<ExternalPictureURL>{escape(u)}</ExternalPictureURL><PictureSet>Supersize</PictureSet>", token)
        full = root.findtext("e:SiteHostedPictureDetails/e:FullURL", default="", namespaces=NS)
        if full:
            out.append(full)
    return out


def trading_call_root(call_name, body_xml, token):
    xml = ('<?xml version="1.0" encoding="utf-8"?>'
           f'<{call_name}Request xmlns="urn:ebay:apis:eBLBaseComponents">{body_xml}</{call_name}Request>')
    r = requests.post(TRADING_URL, headers={
        "X-EBAY-API-CALL-NAME": call_name, "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": "1209", "X-EBAY-API-IAF-TOKEN": token,
        "Content-Type": "text/xml"}, data=xml.encode("utf-8"), timeout=60)
    root = ET.fromstring(r.content)
    ack = root.findtext("e:Ack", default="", namespaces=NS)
    errors = [e.findtext("e:LongMessage", default="", namespaces=NS)
              for e in root.findall("e:Errors", NS) if e.findtext("e:SeverityCode", namespaces=NS) == "Error"]
    return ack, errors, root


# eBayの「eBay外での取引」ポリシー違反を防ぐ：説明文からURL・連絡先・他サイトの話を消す
RISKY_LINE = re.compile(r"メルカリ|ﾒﾙｶﾘ|ヤフオク|ヤフー|Yahoo|PayPay|ペイペイ|ラクマ|Amazon|アマゾン|楽天|ジモティ|"
                        r"ショップ|店舗|オンラインストア|ストア|ホームページ|HP|サイト|LINE|ライン|Instagram|インスタ|"
                        r"Twitter|ツイッター|Facebook|TikTok|YouTube|プロフ|他にも|他の出品|出品中|同時出品|専用|"
                        r"コメント|いいね|即購入|値下げ|交渉|フォロー|#|＃|連絡|問い合わせ|お問合せ|電話|メール|"
                        r"mercari|rakuma|shop|store|http|www\.|@", re.I)
URL_RE = re.compile(r"https?://\S+|www\.\S+|[\w.-]+\.(?:com|jp|net|org|shop|me|info|biz)\S*|[\w.+-]+@[\w-]+\.[\w.]+|"
                    r"0\d{1,4}[-‐ー−]?\d{1,4}[-‐ー−]?\d{3,4}", re.I)


def clean_desc(text):
    lines = []
    for ln in unicodedata.normalize("NFKC", text or "").splitlines():
        if RISKY_LINE.search(ln):
            continue
        ln = URL_RE.sub("", ln).strip()
        if ln:
            lines.append(ln)
    return "\n".join(lines)[:1500]


PHONE_CATEGORY = re.compile(r"携帯電話本体|スマートフォン本体|PHS本体|本体[(（]|テレビゲーム.*>\s*本体")


def not_a_phone(r, title_jp, detail):
    """仕入れ先の商品が携帯電話本体でなさそうなら理由を返す（CDの品番を型番と間違えた、など）"""
    cat = detail.get("category", "")
    if cat and not PHONE_CATEGORY.search(cat):
        return f"携帯電話本体ではない商品です（カテゴリ：{cat[:40]}）"
    if cat:
        return ""   # カテゴリが携帯電話本体なら本体とみなす（型番が書かれていない出品もあるため）
    q = (r.get("query") or "").strip()
    if len(_flat(q)) >= 4 and not has_model(title_jp + " " + detail.get("description", ""), q):
        return f"商品名・説明に型番 {q} がありません"
    return ""


def prepare_text(r, info, detail):
    """出品に使う英語タイトル・説明文など（新規出品と説明文の作り直しで共通）"""
    title_jp = info.get("title") or r.get("title", "")
    model = (r.get("query") or "").strip() or guess_query(title_jp) or ""
    text = title_jp + "\n" + detail.get("description", "")
    unlocked = bool(re.search(r"SIM\s*(フリー|ロック解除)|ロック解除済", unicodedata.normalize("NFKC", text), re.I))
    carrier = carrier_of(model, text)
    color = next((en for jp, en in COLORS.items() if jp in title_jp), "") or \
        next((en for jp, en in COLORS.items() if jp in detail.get("description", "")), "")
    gi = game_info(model)
    if gi:
        title_en = re.sub(r"\s+", " ", f"{gi['series']} Handheld Console {color} Japan").strip()[:80]
    else:
        title_en = build_title(model, title_jp, carrier, unlocked, color)
    tr = sheet("translate", texts=[detail.get("condition", ""), clean_desc(detail.get("description", ""))])["texts"]
    cond_en, desc_en = tr[0], clean_desc(tr[1])
    if gi:
        desc_html = (
            f"<h2>{escape(title_en)}</h2>"
            f"<p><b>Model:</b> {escape(model)}<br><b>Platform:</b> {escape(gi['platform'])}<br>"
            f"<b>Version:</b> Japanese model<br><b>Condition:</b> {escape(cond_en)}</p>"
            "<p>Japanese handheld game console. Only the items shown in the photos are included "
            "(charger, memory card, games and accessories are included only if pictured).</p>"
            + (f"<h3>Notes from the original Japanese description (machine translated)</h3><p>{escape(desc_en).replace(chr(10), '<br>')}</p>" if desc_en else "")
            + "<p>Ships from Japan. Thank you for looking!</p>")
        if gi.get("region_lock"):
            desc_html = desc_html.replace(
                "<p>Ships from Japan.",
                "<p><b>Region:</b> This is a Japanese 3DS system. It plays Japanese region games only.</p><p>Ships from Japan.")
        return {"title_jp": title_jp, "model": model, "unlocked": unlocked, "carrier": carrier, "color": color,
                "title_en": title_en, "cond_en": cond_en, "desc_html": desc_html}
    desc_html = (
        f"<h2>{escape(title_en)}</h2>"
        f"<p><b>Model:</b> {escape(model)}<br><b>Carrier:</b> {escape(carrier or 'See description')}<br>"
        f"<b>SIM lock:</b> {'Unlocked' if unlocked else 'Not stated by the original seller. Please check compatibility before purchase.'}<br>"
        f"<b>Condition:</b> {escape(cond_en)}</p>"
        "<p>Japanese mobile phone (keitai). Only the items shown in the photos are included. "
        "Please make sure the phone supports your carrier's network bands before purchasing.</p>"
        + (f"<h3>Notes from the original Japanese description (machine translated)</h3><p>{escape(desc_en).replace(chr(10), '<br>')}</p>" if desc_en else "")
        + "<p>Ships from Japan. Thank you for looking!</p>")
    return {"title_jp": title_jp, "model": model, "unlocked": unlocked, "carrier": carrier, "color": color,
            "title_en": title_en, "cond_en": cond_en, "desc_html": desc_html}


def create_listing(r, info, detail, settings, token):
    title_jp = info.get("title") or r.get("title", "")
    cost = info.get("price")
    if not cost:
        return None, "価格が読み取れないため出品できません"
    if not settings.get("ship_profile"):
        return None, "「設定」シートに配送ポリシー名が入っていません"
    if float(cost) > max_cost(settings):
        return None, f"SKIP:仕入れ値{int(cost):,}円が上限{int(max_cost(settings)):,}円を超えるため出品しません"
    bad = not_a_phone(r, title_jp, detail)
    if not bad and game_info((r.get("query") or "").strip()):
        bad = game_reject_reason(title_jp, (r.get("query") or "").strip(), cost, r.get("ebay_usd"), settings)
    if bad:
        return None, f"SKIP:{bad}のため出品しません"
    t = prepare_text(r, info, detail)
    model, unlocked, carrier, color, title_en, cond_en, desc_html = (
        t["model"], t["unlocked"], t["carrier"], t["color"], t["title_en"], t["cond_en"], t["desc_html"])
    target, floor = market_price_usd(cost, model, settings)
    if target is None:
        return None, "SKIP:eBayの相場が取れないため出品しません"
    if target < floor:
        return None, f"SKIP:相場(${target:.2f})では利益{int(min_profit_for(cost, settings)):,}円が残らないため出品しません"
    price = target
    pics = upload_pictures(detail.get("images", []), token)
    if not pics:
        return None, "写真をeBayに取り込めなかったため出品を中止しました（もう一度チェックすると再挑戦します）"

    cond_id = 1000 if "新品" in detail.get("condition", "") else 3000
    gi = game_info(model)
    if gi:
        category_id = GAME_EBAY_CATEGORY
        specifics = [("Brand", gi["brand"]), ("Model", model), ("Platform", gi["platform"]),
                     ("Type", "Handheld System"), ("Region Code", "NTSC-J (Japan)"),
                     ("Color", color or "As Pictured"), ("Country of Origin", "Japan")]
    else:
        category_id = "9355"
        specifics = [("Brand", brand_of(model, title_jp)), ("Model", model or "Not Specified"),
                     ("Network", "Unlocked" if unlocked else (carrier or "Unknown")),
                     ("Lock Status", "Network Unlocked" if unlocked else "Network Locked" if carrier else "Unknown"),
                     ("Country of Origin", "Japan")]
        # eBay必須：Color と Storage Capacity
        specifics.append(("Color", color or "As Pictured"))
        specifics.append(("Storage Capacity", storage_of(model)))
    nvl = "".join(f"<NameValueList><Name>{escape(n)}</Name><Value>{escape(v)}</Value></NameValueList>" for n, v in specifics)
    sku = re.sub(r"[^A-Za-z0-9]", "", r["mercari_url"].rstrip("/").split("/")[-1])[:50]
    item = (
        f"<Item><Title>{escape(title_en)}</Title>"
        f"<Description><![CDATA[{desc_html}]]></Description>"
        f"<PrimaryCategory><CategoryID>{category_id}</CategoryID></PrimaryCategory>"
        f'<StartPrice currencyID="USD">{price:.2f}</StartPrice>'
        f"<ConditionID>{cond_id}</ConditionID>"
        + (f"<ConditionDescription>{escape(cond_en[:900])}</ConditionDescription>" if cond_id != 1000 else "") +
        "<Country>JP</Country><Location>Japan</Location><Currency>USD</Currency><Site>US</Site>"
        "<ListingDuration>GTC</ListingDuration><ListingType>FixedPriceItem</ListingType><Quantity>1</Quantity>"
        f"<SKU>{sku}</SKU>"
        "<PictureDetails>" + "".join(f"<PictureURL>{escape(u)}</PictureURL>" for u in pics) + "</PictureDetails>"
        f"<ItemSpecifics>{nvl}</ItemSpecifics>"
        "<SellerProfiles>"
        f"<SellerPaymentProfile><PaymentProfileName>{escape(str(settings.get('pay_profile') or 'Payment'))}</PaymentProfileName></SellerPaymentProfile>"
        f"<SellerReturnProfile><ReturnProfileName>{escape(str(settings.get('return_profile') or 'Return'))}</ReturnProfileName></SellerReturnProfile>"
        f"<SellerShippingProfile><ShippingProfileName>{escape(str(settings.get('ship_profile')))}</ShippingProfileName></SellerShippingProfile>"
        "</SellerProfiles></Item>")
    ack, errors, root = trading_call_root("AddFixedPriceItem", item, token)
    item_id = root.findtext("e:ItemID", default="", namespaces=NS)
    if ack in ("Success", "Warning") and item_id:
        return {"ebay_item_id": item_id, "price": price, "title_en": title_en}, None
    return None, "出品に失敗：" + " / ".join(errors)[:300]


# ---------- 自動仕入れ候補さがし ----------
SEARCH_JS = """async () => {
    for (let i = 0; i < 5; i++) { window.scrollBy(0, 2000); await new Promise(r => setTimeout(r, 1200)); }
    return [...document.querySelectorAll('a[href*="/item/m"], a[href*="/shops/product/"]')]
        .filter(a => !a.getAttribute('href').includes('ad_id'))   // 広告枠は除く
        .map(a => {
            const img = a.querySelector('img');
            const m = a.innerText.match(/([\\d,]+)/);
            return { auction: /現在/.test(a.innerText),   // オークションは「現在 ¥◯◯」と表示される
                     url: 'https://jp.mercari.com' + a.getAttribute('href').split('?')[0],
                     title: img ? img.alt.replace(/のサムネイル$/, '') : '',
                     price: m ? parseInt(m[1].replace(/,/g, ''), 10) : null };
        });
}"""


# ---------- Yahoo!フリマ（旧PayPayフリマ） ----------
YAHOO_API = "https://paypayfleamarket.yahoo.co.jp/api"
YAHOO_HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
                 "Accept": "application/json", "Referer": "https://paypayfleamarket.yahoo.co.jp/"}
YAHOO_PHONE_CATEGORY = 38419   # 携帯電話本体
YAHOO_OK_CATEGORIES = {38419, 37849, 37661, 37697, 37799, 37808, 38155}   # 携帯電話・PSP・Vita・DS・GB・GBA・3DS の本体


def is_yahoo(url):
    return "paypayfleamarket.yahoo.co.jp/item/" in url


def yahoo_item(url):
    item_id = url.rstrip("/").split("/item/")[-1].split("?")[0]
    r = requests.get(f"{YAHOO_API}/item/v2/items/{item_id}", headers=YAHOO_HEADERS, timeout=30)
    return r.status_code, (r.json() if r.status_code == 200 else {})


def check_yahoo(url):
    """メルカリの check_mercari と同じ形 (status, info, detail) を返す"""
    try:
        code, j = yahoo_item(url)
    except Exception:
        return "unknown", {}, {}
    if code == 404:
        return "deleted", {}, {}
    if code in (403, 429):
        return "blocked", {}, {}
    if code != 200:
        return "unknown", {}, {}
    st = j.get("status")
    status = "on_sale" if st == "OPEN" else "sold" if st in ("SOLD", "CLOSED") else "unknown"
    info = {"title": j.get("title", ""), "price": j.get("price")}
    detail = {"images": [i.get("url") for i in j.get("images", []) if i.get("url")][:12],
              "description": j.get("description", ""),
              "condition": (j.get("condition") or {}).get("text", ""),
              "category": " > ".join(c.get("name", "") for c in ((j.get("category") or {}).get("path") or []))
                          or (j.get("category") or {}).get("name", "")}
    return status, info, detail


def yahoo_search(keyword):
    r = requests.get(f"{YAHOO_API}/v1/search", headers=YAHOO_HEADERS, timeout=30,
                     params={"query": keyword, "results": 100, "sort": "price", "order": "asc",
                             "itemStatus": "open"})
    r.raise_for_status()
    out = []
    for it in r.json().get("items", []):
        if (it.get("category") or {}).get("id") not in YAHOO_OK_CATEGORIES:
            continue          # 携帯電話本体以外（電池・ケースなど）は除く
        if it.get("condition") == "used80":
            continue          # 全体的に状態が悪い
        out.append({"url": f"https://paypayfleamarket.yahoo.co.jp/item/{it['id']}",
                    "title": it.get("title", ""), "price": it.get("price")})
    return out


ACCESSORY_WORDS = ["説明書", "取説", "取扱", "充電器", "充電台", "卓上ホルダ", "卓上ホルダー", "アダプタ", "ACアダプ",
                   "ケーブル", "電池パック", "外箱", "空箱", "元箱のみ", "付属品のみ", "パーツ", "部品",
                   "シェル", "外装", "ハウジング", "筐体", "液晶パネル", "液晶のみ", "画面のみ", "基板", "ボタン",
                   "スティック", "UMD", "ソフトのみ", "ソフト単品", "ゲームソフト", "メモリースティック", "メモリーカード", "ポーチ", "スキンシール"]
BODY_WORDS = ["本体", "付き", "付属", "同梱", "セット内容", "揃い", "完品", "一式", "なし", "無し"]


ACCESSORY_PRICE_RATIO = 0.2   # eBay相場のこの割合以下の値段なら「本体にしては安すぎる」


def looks_like_accessory(title):
    t = unicodedata.normalize("NFKC", title or "")
    return any(w in t for w in ACCESSORY_WORDS) and not any(w in t for w in BODY_WORDS)


def is_accessory(title, price_jpy, sale_usd, settings):
    """携帯電話本体カテゴリに出ている「説明書だけ」「充電器だけ」などを見分ける。
    付属品っぽい単語がある「かつ」本体にしては安すぎる、の両方のときだけ付属品とみなす（本体を弾かないため）"""
    t = unicodedata.normalize("NFKC", title or "")
    if not any(w in t for w in ACCESSORY_WORDS) or any(w in t for w in BODY_WORDS):
        return False
    try:
        market_jpy = float(sale_usd) * usd_jpy(settings)
        return float(price_jpy) <= market_jpy * ACCESSORY_PRICE_RATIO
    except (TypeError, ValueError):
        return False   # 値段か相場が分からないときは外さない


def discover():
    data = sheet("list")
    settings = data.get("settings", {})
    known = {r["mercari_url"].split("?")[0] for r in data["rows"]}
    keywords = [k.strip() for k in re.split(r"[,、\n]", str(settings.get("keywords") or "ガラケー")) if k.strip()]
    KNOWN_MODELS[:] = keywords
    min_margin = float(settings.get("min_margin") or 20)
    min_profit = float(settings.get("min_profit") or 1500)
    max_add = int(float(settings.get("max_add") or 5))
    exclude = [w.strip() for w in re.split(r"[,、\n]", str(settings.get("exclude") or "")) if w.strip()]

    found, seen, checked = [], set(), 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(locale="ja-JP", user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
        for kw in keywords:
            gi = game_info(kw)
            url = ("https://jp.mercari.com/search?status=on_sale&sort=created_time&order=desc"
                   f"&category_id={gi['mercari'] if gi else '860'}&keyword="
                   + ("" if gi and gi.get("exact") else requests.utils.quote(kw)))   # 本体カテゴリ（機種別カテゴリなら検索語なし）
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_selector('a[href*="/item/m"], a[href*="/shops/product/"]', timeout=25000)
                items = page.evaluate(SEARCH_JS)
            except Exception:
                continue
            for it in items:
                u = it["url"]
                if u in known or u in seen or not it["price"] or it.get("auction") or it["price"] > max_cost(settings):
                    continue
                seen.add(u)
                if any(w in it["title"] for w in exclude):
                    continue
                q = kw if gi and gi.get("exact") else guess_query(it["title"])   # 機種別カテゴリならその機種
                if not q:
                    continue
                checked += 1
                sale, n = ebay_market_usd(q)
                if not sale or n < MIN_COUNT:  # 相場の件数が少ない機種は入れない
                    continue
                if is_accessory(it["title"], it["price"], sale, settings):   # 説明書・充電器だけの出品
                    continue
                if game_info(q) and game_reject_reason(it["title"], q, it["price"], sale, settings):
                    continue   # ゲーム機は商品名で本体かどうかを厳しく確認
                p, rate = profit(it["price"], sale, settings)
                if rate >= min_margin and p >= min_profit:
                    found.append({"mercari_url": u, "title": it["title"], "cost": it["price"],
                                  "ebay_usd": sale, "ebay_count": n, "query": q,
                                  "profit": p, "margin": rate, "memo": "自動発見",
                                  "result": f"自動発見 {now()}"})
            # Yahoo!フリマも同じキーワードで探す
            try:
                yitems = yahoo_search(gi.get("yahoo_q", kw) if gi else kw)
            except Exception:
                yitems = []
            for it in yitems:
                u = it["url"]
                if u in known or u in seen or not it["price"] or it["price"] > max_cost(settings):
                    continue
                seen.add(u)
                if any(w in it["title"] for w in exclude):
                    continue
                if gi and gi.get("alias"):
                    q = kw if match_game(it["title"], kw) else ""   # 呼び名で機種を見分ける
                else:
                    q = guess_query(it["title"])
                if not q:
                    continue
                checked += 1
                sale, n = ebay_market_usd(q)
                if not sale or n < MIN_COUNT:
                    continue
                if is_accessory(it["title"], it["price"], sale, settings):
                    continue
                if game_info(q) and game_reject_reason(it["title"], q, it["price"], sale, settings):
                    continue
                p, rate = profit(it["price"], sale, settings)
                if rate >= min_margin and p >= min_profit:
                    found.append({"mercari_url": u, "title": it["title"], "cost": it["price"],
                                  "ebay_usd": sale, "ebay_count": n, "query": q,
                                  "profit": p, "margin": rate, "memo": "自動発見（Yahoo!フリマ）",
                                  "result": f"自動発見 {now()}"})
            time.sleep(random.uniform(8, 15))
        browser.close()

    found.sort(key=lambda x: x["profit"], reverse=True)
    added = found[:max_add]
    if added:
        sheet("append", rows=added)
        best = added[0]
        notify(f"【仕入れ候補】{len(added)}件を追加しました。最高：{best['title'][:30]} 想定利益 {best['profit']:,}円（{best['margin']}%）")
    msg = f"{now()} 候補さがし：{checked}件を計算、条件に合う{len(found)}件のうち{len(added)}件を追加"
    print(msg)
    sheet("log", message=msg)


def fixdesc():
    """出品中の全商品：携帯電話本体でないものはeBayを終了、それ以外は説明文を安全なものに作り直す（1回だけ実行）"""
    data = sheet("list")
    settings = data.get("settings", {})
    KNOWN_MODELS[:] = [k.strip() for k in re.split(r"[,、\n]", str(settings.get("keywords") or "")) if k.strip()]
    rows = [r for r in data["rows"] if r["ebay_item_id"] and r["status"] in ("", "active")]
    token = get_access_token()
    fixed = ended = failed = 0
    updates = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(locale="ja-JP", user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
        for r in rows:
            try:
                if is_yahoo(r["mercari_url"]):
                    status, info, detail = check_yahoo(r["mercari_url"])
                else:
                    status, info = check_mercari(page, r["mercari_url"])
                    detail = page.evaluate(DETAIL_JS) if status == "on_sale" else {}
                if status != "on_sale" or not info:
                    continue   # 売り切れ等は通常の監視で止まる
                bad = not_a_phone(r, info.get("title", ""), detail)
                if bad:
                    ok, msg = stop_on_ebay(r["ebay_item_id"], token)
                    updates.append({"row": r["row"], "status": "stopped" if ok else None,
                                    "result": f"{bad}のためeBayを終了 → {msg}"})
                    ended += ok
                    continue
                t = prepare_text(r, info, detail)
                ack, errs, root = trading_call_root(
                    "ReviseFixedPriceItem",
                    f"<Item><ItemID>{r['ebay_item_id']}</ItemID><Description><![CDATA[{t['desc_html']}]]></Description></Item>",
                    token)
                if ack in ("Success", "Warning"):
                    fixed += 1
                else:
                    failed += 1
                    updates.append({"row": r["row"], "result": "説明文の修正に失敗：" + " / ".join(errs)[:200]})
            except Exception as e:
                failed += 1
                print("説明文の修正でエラー:", r["row"], str(e)[:200])
        browser.close()
    updates = [{k: v for k, v in u.items() if v is not None} for u in updates]
    if updates:
        sheet("update", updates=updates)
    msg = f"{now()} 説明文の作り直し：修正 {fixed}件／eBay終了 {ended}件／失敗 {failed}件"
    print(msg)
    sheet("log", message=msg)


def restore():
    """型番チェックの不具合で止めてしまった出品を元に戻し、説明文を作り直す"""
    data = sheet("list")
    token = get_access_token()
    rows = [r for r in data["rows"] if r["ebay_item_id"] and r["status"] == "stopped"
            and (re.search(r"型番 .+ がありませんのためeBayを終了", r.get("result") or "")
                 or ("単体のため出品を止めました" in (r.get("result") or "")
                     and not looks_like_accessory(r.get("title") or "")))]
    updates, ok_n, relist_n, fail_n = [], 0, 0, 0
    for r in rows:
        ack, errs, root = trading_call_root(
            "ReviseInventoryStatus",
            f"<InventoryStatus><ItemID>{r['ebay_item_id']}</ItemID><Quantity>1</Quantity></InventoryStatus>", token)
        if ack in ("Success", "Warning"):
            ok_n += 1
            updates.append({"row": r["row"], "status": "active", "result": "出品を元に戻しました（在庫1）"})
            continue
        ack2, errs2, root2 = trading_call_root(
            "RelistFixedPriceItem", f"<Item><ItemID>{r['ebay_item_id']}</ItemID><Quantity>1</Quantity></Item>", token)
        new_id = root2.findtext("e:ItemID", default="", namespaces=NS)
        if ack2 in ("Success", "Warning") and new_id:
            relist_n += 1
            updates.append({"row": r["row"], "status": "active", "ebay_item_id": new_id,
                            "result": f"再出品しました（新しい出品番号 {new_id}）"})
        else:
            fail_n += 1
            updates.append({"row": r["row"], "result": "元に戻せませんでした：" + " / ".join(errs + errs2)[:200]})
    if updates:
        sheet("update", updates=updates)
    msg = f"{now()} 出品の復元：在庫を戻した {ok_n}件／再出品 {relist_n}件／失敗 {fail_n}件"
    print(msg)
    sheet("log", message=msg)


def end_stopped():
    """在庫0で止めたままの出品（売り切れ・本体でない商品など）をeBayで終了する（1回だけ実行）"""
    data = sheet("list")
    token = get_access_token()
    rows = [r for r in data["rows"] if r["ebay_item_id"] and r["status"] == "stopped"
            and "出品を終了しました" not in (r.get("result") or "")]
    updates, ok_n, fail_n = [], 0, 0
    for r in rows:
        ok, msg = stop_on_ebay(r["ebay_item_id"], token)
        if ok:
            ok_n += 1
            updates.append({"row": r["row"], "result": f"{(r.get('result') or '')[:80]} → {msg}"})
        else:
            fail_n += 1
            print("終了できず:", r["ebay_item_id"], msg)
    if updates:
        sheet("update", updates=updates)
    msg = f"{now()} 在庫なしの出品の終了：{ok_n}件／失敗 {fail_n}件"
    print(msg)
    sheet("log", message=msg)


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "discover":
            discover()
        elif len(sys.argv) > 1 and sys.argv[1] == "fixdesc":
            fixdesc()
        elif len(sys.argv) > 1 and sys.argv[1] == "endstopped":
            end_stopped()
        elif len(sys.argv) > 1 and sys.argv[1] == "restore":
            restore()
            fixdesc()
        else:
            main()
    except Exception as e:
        notify(f"【エラー】監視中に問題が起きました: {e}")
        print("エラーが起きました（詳細はスプレッドシートの「設定」シートを確認）:", type(e).__name__)
        try:
            sheet("log", message=f"{now()} エラー：{e}")
        except Exception:
            pass
        sys.exit(1)
