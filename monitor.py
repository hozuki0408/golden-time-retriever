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
    ack, errors = trading_call(
        "ReviseInventoryStatus",
        f"<InventoryStatus><ItemID>{item_id}</ItemID><Quantity>0</Quantity></InventoryStatus>",
        token,
    )
    if ack in ("Success", "Warning"):
        return True, "eBay在庫を0にしました"
    ack2, errors2 = trading_call(
        "EndFixedPriceItem",
        f"<ItemID>{item_id}</ItemID><EndingReason>NotAvailable</EndingReason>",
        token,
    )
    if ack2 in ("Success", "Warning"):
        return True, "在庫0にできず、eBay出品を終了しました"
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


def guess_query(title):
    """商品名から型番らしき文字列を拾う（例：KY-42C、SH-02L、903KC、SO903i）"""
    t = unicodedata.normalize("NFKC", title or "")
    ft = _flat(t)
    for k in KNOWN_MODELS:
        if len(_flat(k)) >= 4 and _flat(k) in ft:
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


def _ebay_market_usd(query):
    r = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        params={"q": query, "category_ids": "9355",
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

            if r["ebay_item_id"]:
                # ---- 出品中の商品：売り切れたらeBayを止める ----
                if status in ("sold", "deleted"):
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
                if status in ("sold", "deleted", "auction"):
                    upd["result"] = f"{LABEL[status]}（候補から外しました）"
                    upd["status"] = "stopped"
                elif status == "on_sale" and info and r.get("list"):
                    # ---- 「出品する」にチェック：eBayに出品 ----
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
    min_profit = float(settings.get("min_profit") or 2000)
    floor = math.ceil((cost + ship + min_profit) / max(0.05, 1 - fee) / fx) - 0.01   # 利益2,000円が残る最低価格
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


def create_listing(r, info, detail, settings, token):
    title_jp = info.get("title") or r.get("title", "")
    cost = info.get("price")
    if not cost:
        return None, "価格が読み取れないため出品できません"
    if not settings.get("ship_profile"):
        return None, "「設定」シートに配送ポリシー名が入っていません"
    model = (r.get("query") or "").strip() or guess_query(title_jp) or ""
    text = title_jp + "\n" + detail.get("description", "")
    unlocked = bool(re.search(r"SIM\s*(フリー|ロック解除)|ロック解除済", unicodedata.normalize("NFKC", text), re.I))
    carrier = carrier_of(model, text)
    color = next((en for jp, en in COLORS.items() if jp in title_jp), "") or \
        next((en for jp, en in COLORS.items() if jp in detail.get("description", "")), "")
    title_en = build_title(model, title_jp, carrier, unlocked, color)

    tr = sheet("translate", texts=[detail.get("condition", ""), detail.get("description", "")[:3000]])["texts"]
    cond_en, desc_en = tr[0], tr[1]
    target, floor = market_price_usd(cost, model, settings)
    if target is None:
        return None, "SKIP:eBayの相場が取れないため出品しません"
    if target < floor:
        return None, f"SKIP:相場(${target:.2f})では利益{int(float(settings.get('min_profit') or 2000)):,}円が残らないため出品しません"
    price = target
    pics = upload_pictures(detail.get("images", []), token)
    if not pics:
        return None, "写真をeBayに取り込めなかったため出品を中止しました（もう一度チェックすると再挑戦します）"

    cond_id = 1000 if "新品" in detail.get("condition", "") else 3000
    specifics = [("Brand", brand_of(model, title_jp)), ("Model", model or "Not Specified"),
                 ("Network", "Unlocked" if unlocked else (carrier or "Unknown")),
                 ("Lock Status", "Network Unlocked" if unlocked else "Network Locked" if carrier else "Unknown"),
                 ("Country of Origin", "Japan")]
    # eBay必須：Color と Storage Capacity
    specifics.append(("Color", color or "As Pictured"))
    specifics.append(("Storage Capacity", storage_of(model)))
    desc_html = (
        f"<h2>{escape(title_en)}</h2>"
        f"<p><b>Model:</b> {escape(model)}<br><b>Carrier:</b> {escape(carrier or 'See description')}<br>"
        f"<b>SIM lock:</b> {'Unlocked' if unlocked else 'Not stated by the original seller. Please check compatibility before purchase.'}<br>"
        f"<b>Condition:</b> {escape(cond_en)}</p>"
        "<p>Japanese mobile phone (keitai). Only the items shown in the photos are included. "
        "Please make sure the phone supports your carrier's network bands before purchasing.</p>"
        f"<h3>Notes from the original Japanese description (machine translated)</h3><p>{escape(desc_en).replace(chr(10), '<br>')}</p>"
        "<p>Ships from Japan. Thank you for looking!</p>")
    nvl = "".join(f"<NameValueList><Name>{escape(n)}</Name><Value>{escape(v)}</Value></NameValueList>" for n, v in specifics)
    sku = re.sub(r"[^A-Za-z0-9]", "", r["mercari_url"].rstrip("/").split("/")[-1])[:50]
    item = (
        f"<Item><Title>{escape(title_en)}</Title>"
        f"<Description><![CDATA[{desc_html}]]></Description>"
        "<PrimaryCategory><CategoryID>9355</CategoryID></PrimaryCategory>"
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
              "condition": (j.get("condition") or {}).get("text", "")}
    return status, info, detail


def yahoo_search(keyword):
    r = requests.get(f"{YAHOO_API}/v1/search", headers=YAHOO_HEADERS, timeout=30,
                     params={"query": keyword, "results": 100, "sort": "price", "order": "asc",
                             "itemStatus": "open"})
    r.raise_for_status()
    out = []
    for it in r.json().get("items", []):
        if (it.get("category") or {}).get("id") != YAHOO_PHONE_CATEGORY:
            continue          # 携帯電話本体以外（電池・ケースなど）は除く
        if it.get("condition") == "used80":
            continue          # 全体的に状態が悪い
        out.append({"url": f"https://paypayfleamarket.yahoo.co.jp/item/{it['id']}",
                    "title": it.get("title", ""), "price": it.get("price")})
    return out


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
            url = ("https://jp.mercari.com/search?status=on_sale&sort=created_time&order=desc"
                   "&category_id=860&keyword=" + requests.utils.quote(kw))   # 携帯電話本体カテゴリ
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_selector('a[href*="/item/m"], a[href*="/shops/product/"]', timeout=25000)
                items = page.evaluate(SEARCH_JS)
            except Exception:
                continue
            for it in items:
                u = it["url"]
                if u in known or u in seen or not it["price"] or it.get("auction"):
                    continue
                seen.add(u)
                if any(w in it["title"] for w in exclude):
                    continue
                q = guess_query(it["title"])
                if not q:
                    continue
                checked += 1
                sale, n = ebay_market_usd(q)
                if not sale or n < MIN_COUNT:  # 相場の件数が少ない機種は入れない
                    continue
                p, rate = profit(it["price"], sale, settings)
                if rate >= min_margin and p >= min_profit:
                    found.append({"mercari_url": u, "title": it["title"], "cost": it["price"],
                                  "ebay_usd": sale, "ebay_count": n, "query": q,
                                  "profit": p, "margin": rate, "memo": "自動発見",
                                  "result": f"自動発見 {now()}"})
            # Yahoo!フリマも同じキーワードで探す
            try:
                yitems = yahoo_search(kw)
            except Exception:
                yitems = []
            for it in yitems:
                u = it["url"]
                if u in known or u in seen or not it["price"]:
                    continue
                seen.add(u)
                if any(w in it["title"] for w in exclude):
                    continue
                q = guess_query(it["title"])
                if not q:
                    continue
                checked += 1
                sale, n = ebay_market_usd(q)
                if not sale or n < MIN_COUNT:
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


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "discover":
            discover()
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
