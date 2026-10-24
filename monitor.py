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
from datetime import datetime, timedelta
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


def alert(subject, body):
    """売れた・要確認など、すぐ見てほしい知らせだけをメール（スマホのGmail）とDiscordに送る"""
    notify(f"{subject}\n{body}")
    try:
        sheet("mail", subject=subject, body=body)
    except Exception:
        print("メール通知に失敗しました")


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


# eBayのRefresh Tokenの期限（取り直したら TOKEN_ISSUED を書き換える）
TOKEN_ISSUED = "2026-10-21"
TOKEN_DAYS = 547   # 約18か月
TOKEN_HOWTO = ("取り直し方：Claudeに「eBayのトークンを取り直したい」と伝えてください。"
               "取り直すまで、在庫チェック・出品・売り切れ時の出品停止がすべて止まります。")


def token_days_left():
    issued = datetime.strptime(TOKEN_ISSUED, "%Y-%m-%d").replace(tzinfo=JST)
    return (issued + timedelta(days=TOKEN_DAYS) - datetime.now(JST)).days


def token_warning():
    """期限の60日前から、毎日9時台に1回だけメールで知らせる"""
    left = token_days_left()
    if left <= 60 and datetime.now(JST).hour == 9:
        alert(f"【要対応】eBayのトークンの期限まであと{max(left, 0)}日です",
              f"eBayとツールをつなぐトークン（Refresh Token）の期限が近づいています（あと約{max(left, 0)}日）。\n\n{TOKEN_HOWTO}")


FINANCES_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.finances"


ANALYTICS_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly"
NON_SALE_OTHER = []   # 広告費以外の手数料（出品手数料など）。経費シートに入れる


def get_finances_token(scope=FINANCES_SCOPE):
    """追加の権限（お金の明細・閲覧数）のトークン。権限がまだなければ None（ほかの処理は止めない）"""
    auth = "Basic " + base64.b64encode(f"{APP_ID}:{CERT_ID}".encode()).decode()
    try:
        r = requests.post(
            TOKEN_URL,
            headers={"Authorization": auth, "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN, "scope": scope},
            timeout=30,
        )
        return r.json().get("access_token") if r.status_code == 200 else None
    except Exception:
        return None


def ebay_ad_fees(days=90):
    """広告費 {注文番号: ドル}。お金の明細の権限がまだなければ None"""
    tok = get_finances_token()
    if not tok:
        return None
    since = (datetime.now(ZoneInfo("UTC")) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    out, offset = {}, 0
    while offset < 2000:
        r = requests.get("https://apiz.ebay.com/sell/finances/v1/transaction",
                         params={"filter": [f"transactionDate:[{since}..]", "transactionType:{NON_SALE_CHARGE}"],
                                 "limit": 200, "offset": offset},
                         headers={"Authorization": f"Bearer {tok}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}, timeout=30)
        if r.status_code != 200:
            print("広告費の明細を読めませんでした:", r.status_code, r.text[:150])
            return None
        js = r.json()
        for t in js.get("transactions", []):
            ft = str(t.get("feeType", "")).upper()
            if "AD_FEE" not in ft and "PROMOT" not in ft:   # 広告以外（出品手数料など）は経費シートへ
                v = float((t.get("amount") or {}).get("value") or 0)
                NON_SALE_OTHER.append({"id": t.get("transactionId", ""), "date": (t.get("transactionDate") or "")[:10],
                                       "type": ft or "OTHER", "usd": round(-v if t.get("bookingEntry") == "CREDIT" else v, 2),
                                       "memo": str(t.get("transactionMemo") or "")[:80]})
                continue
            oid = t.get("orderId") or next((x.get("referenceId") for x in t.get("references", [])
                                             if x.get("referenceType") == "ORDER_ID"), "")
            if not oid:
                continue
            v = float((t.get("amount") or {}).get("value") or 0)
            if t.get("bookingEntry") == "CREDIT":
                v = -v   # 返金された広告費
            out[oid] = round(out.get(oid, 0) + v, 2)
        if len(js.get("transactions", [])) < 200:
            break
        offset += 200
    return out


def listing_views(ids, days=30):
    """出品ごとの閲覧数と表示回数 {出品番号: (閲覧, 表示)}（直近30日）。権限がなければ {}"""
    tok = get_finances_token(ANALYTICS_SCOPE)
    if not tok or not ids:
        return {}
    end = datetime.now(ZoneInfo("America/Los_Angeles"))
    rng = f"[{(end - timedelta(days=days)).strftime('%Y%m%d')}..{end.strftime('%Y%m%d')}]"
    out, ids = {}, list(ids)
    for k in range(0, len(ids), 200):
        r = requests.get("https://api.ebay.com/sell/analytics/v1/traffic_report", params={
            "dimension": "LISTING", "metric": "LISTING_VIEWS_TOTAL,LISTING_IMPRESSION_TOTAL",
            "filter": f"marketplace_ids:{{EBAY_US}},date_range:{rng},listing_ids:{{{'|'.join(ids[k:k + 200])}}}"},
            headers={"Authorization": f"Bearer {tok}"}, timeout=60)
        if r.status_code != 200:
            print("閲覧数を読めませんでした:", r.status_code, r.text[:150])
            return out
        js = r.json()
        keys = [m.get("key") for m in (js.get("header") or {}).get("metrics", [])]
        for rec in js.get("records", []):
            lid = ((rec.get("dimensionValues") or [{}])[0]).get("value")
            vals = {kk: (mv or {}).get("value") for kk, mv in zip(keys, rec.get("metricValues", []))}
            out[str(lid)] = (int(vals.get("LISTING_VIEWS_TOTAL") or 0), int(vals.get("LISTING_IMPRESSION_TOTAL") or 0))
    return out


def unsold_reason(days, views, watch, price, target, model):
    """売れていない理由の予想（リストのY列）"""
    if days is None:
        return ""
    if days < 3:
        return "出品して間もない（様子見）"
    rs = []
    try:
        if target and price and float(price) > float(target) * 1.1:
            rs.append(f"相場より高い（出品${float(price):.0f}／売れている相場${float(target):.0f}）")
    except (TypeError, ValueError):
        pass
    if views is not None and views < 3:
        rs.append(f"ほとんど見られていない（{int(days)}日で閲覧{views}回）→検索に出ていない")
    elif views is not None and views >= 10 and not watch:
        rs.append(f"見られているがウォッチされない（閲覧{views}回）→値段・写真・状態が合っていない可能性")
    if watch:
        rs.append(f"ウォッチ{watch}人→少し値下げすれば売れる可能性")
    sold = SOLD90_BY_KEY.get(model_key(model))
    if sold is not None and sold < 10:
        rs.append(f"売れるペースが遅い機種（90日で{sold}台）")
    return " / ".join(rs[:3]) or "はっきりした原因なし（もう少し様子見）"


# ---------- ウォッチしている人への値引きオファー ----------
OFFER_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.inventory"
OFFER_DISCOUNT = 0.10      # 今の値段から10%引き（ただし利益2,000円の値段より下げない）
OFFER_EVERY_DAYS = 7       # 同じ出品に送るのは7日に1回まで
OFFER_FILE = os.path.join(".cache", "offers.json")
OFFER_MSG = ("Thank you for watching this item! Here is a special price just for you. "
             "This is the only one we have, and it ships from Japan with tracking.")


def send_watch_offers(targets):
    """ウォッチしている人（eBayが「興味あり」と判定した人）に値引きオファーを送る"""
    if not targets:
        return
    tok = get_finances_token(OFFER_SCOPE)
    if not tok:
        print("値引きオファー：権限がないため送れません")
        return
    hdr = {"Authorization": f"Bearer {tok}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US", "Content-Type": "application/json"}
    eligible, offset = set(), 0
    while offset < 2000:
        r = requests.get("https://api.ebay.com/sell/negotiation/v1/find_eligible_items",
                         params={"limit": 200, "offset": offset}, headers=hdr, timeout=30)
        if r.status_code == 204:
            break
        if r.status_code != 200:
            print("値引きオファー：対象の出品を読めませんでした:", r.status_code, r.text[:150])
            return
        items = r.json().get("eligibleItems", [])
        eligible |= {str(x.get("listingId")) for x in items}
        if len(items) < 200:
            break
        offset += 200
    try:
        with open(OFFER_FILE, encoding="utf-8") as f:
            sent = json.load(f)
    except (OSError, ValueError):
        sent = {}
    cut = time.time() - OFFER_EVERY_DAYS * 86400
    n_sent = 0
    for lid, (cur, floor) in targets.items():
        if lid not in eligible or sent.get(lid, 0) > cut or not cur or not floor:
            continue
        price = max(float(floor), round(float(cur) * (1 - OFFER_DISCOUNT), 2))
        if price > float(cur) * 0.95:   # 5%以上安くできない（利益2,000円の値段に近い）ときは送らない
            continue
        body = {"allowCounterOffer": False, "message": OFFER_MSG,
                "offeredItems": [{"listingId": lid, "quantity": 1,
                                  "price": {"currency": "USD", "value": f"{price:.2f}"}}]}
        r = requests.post("https://api.ebay.com/sell/negotiation/v1/send_offer_to_interested_buyers",
                          json=body, headers=hdr, timeout=30)
        if r.status_code == 200:
            n_sent += 1
            sent[lid] = time.time()
            print(f"値引きオファーを送りました：{lid} ${cur:.2f}→${price:.2f}（{len(r.json().get('offers', []))}人）")
        else:
            print(f"値引きオファーを送れませんでした：{lid}", r.status_code, r.text[:150])
            sent[lid] = time.time() - (OFFER_EVERY_DAYS - 1) * 86400   # 失敗したら1日後にもう一度
    os.makedirs(".cache", exist_ok=True)
    with open(OFFER_FILE, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in sent.items() if v > cut - 86400}, f)
    print(f"値引きオファー：対象{len(eligible & set(targets))}件、送信{n_sent}件")


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


def check_mercari(page, url, retry=True):
    st, info = _check_mercari(page, url)
    if st == "unknown" and retry:   # メルカリが一時的に重いことがあるので、少し待ってもう一度だけ見る
        page.wait_for_timeout(8000)
        st, info = _check_mercari(page, url)
        if st != "unknown":
            print(f"2回目で判定できました：{url} → {st}")
    return st, info


def _check_mercari(page, url):
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print(f"判定できず（ページを開けない）{url}:", repr(e)[:120])
        return "unknown", {}
    if resp is not None and resp.status == 404:
        return "deleted", {}
    if resp is not None and resp.status in (403, 429):
        return "blocked", {}
    try:
        page.wait_for_function(
            """() => { const t = document.body ? document.body.innerText : '';
                return /購入手続きへ|売り切れ|削除されています|見つかりません|公開停止|入札する|オークション商品/.test(t); }""",
            timeout=25000,
        )
    except Exception:
        try:
            snip = re.sub(r"\s+", " ", page.evaluate("() => document.body ? document.body.innerText : ''"))[:120]
            print(f"判定できず（表示が出ない）{url} 状態{resp.status if resp else '?'} 題名「{page.title()[:40]}」本文「{snip}」")
        except Exception:
            pass
        return "unknown", {}
    page.wait_for_timeout(600)
    status = page.evaluate(JUDGE_JS)
    info = page.evaluate(INFO_JS) if status in ("on_sale", "sold") else {}
    if status == "on_sale" and not info.get("price"):   # 値段の表示が遅れることがあるので、少し待って読み直す
        page.wait_for_timeout(2500)
        info = page.evaluate(INFO_JS)
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
    const head = main.split('商品の説明')[0].replace(/過去価格\\s*¥\\s*[\\d,]+/g, '').replace(/\\d+\\s*%\\s*OFF/gi, '');   // メルカリShopsの値引き表示（過去価格・◯%OFF）は除く
    const pe = document.querySelector('[data-testid="price"]');   // 値段の欄を優先して読む
    const pm = pe ? pe.innerText.replace(/\\s/g, '').match(/([\\d,]+)/) : null;
    const m = pm || head.match(/¥\\s*([\\d,]+)/);
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
        _market_stats["hit"] += 1
        return _market_cache[query]
    _market_stats["miss"] += 1
    _market_cache[query] = _ebay_market_usd(query)
    _market_time[query] = time.time()
    return _market_cache[query]


# ---- 候補さがし用：eBay相場を6時間ファイルに覚えておく（出品の直前には必ず最新の相場を取り直す） ----
MARKET_FILE = os.path.join(".cache", "market.json")
MARKET_TTL = 6 * 3600
_market_time = {}
_market_stats = {"hit": 0, "miss": 0}


def load_market_cache():
    try:
        with open(MARKET_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        cut = time.time() - MARKET_TTL
        for q, (t, v) in saved.items():
            if t >= cut:
                _market_cache[q] = tuple(v)
                _market_time[q] = t
        print(f"保存してあった相場：{len(_market_cache)}件（6時間以内）")
    except FileNotFoundError:
        print("保存してあった相場：なし")
    except Exception as e:
        print("保存してあった相場を読めませんでした:", str(e)[:100])


def save_market_cache():
    try:
        os.makedirs(os.path.dirname(MARKET_FILE), exist_ok=True)
        with open(MARKET_FILE, "w", encoding="utf-8") as f:
            json.dump({q: [_market_time[q], list(v)] for q, v in _market_cache.items()
                       if q in _market_time and v and v[0]}, f, ensure_ascii=False)
    except Exception as e:
        print("相場を保存できませんでした:", str(e)[:100])


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
    # 2026/10/21 追加（eBayで売れた台数と利益を調べて選んだもの）。DSi LL はメルカリでは DSi と同じカテゴリ
    "UTL-001": {"brand": "Nintendo", "platform": "Nintendo DSi XL", "series": "Nintendo DSi LL XL UTL-001",
                "mercari": "8898", "exact": True, "yahoo_q": "DSi LL", "ebay_q": "Nintendo DSi XL",
                "alias": r"DSI\s*(LL|XL)|UTL", "neg": r"3DS",
                "region_note": "This Japanese DSi XL plays Nintendo DS games from any region. DSi-enhanced games and DSiWare are Japanese region only."},
    # 古いゲーム機（液晶の不具合が多いので最低利益を高めにする：RETRO_MIN_PROFIT）
    "WSC-001": {"brand": "Bandai", "platform": "Bandai WonderSwan Color", "series": "Bandai WonderSwan Color WSC-001",
                "mercari": "7197", "exact": True, "yahoo_q": "ワンダースワンカラー", "ebay_q": "Bandai WonderSwan Color",
                "alias": r"スワン\s*カラー|SWAN\s*COLOU?R|WSC", "neg": r"クリスタル|CRYSTAL|SC-001"},
    "NEOP-10": {"brand": "SNK", "platform": "SNK Neo Geo Pocket Color", "series": "SNK Neo Geo Pocket Color",
                "mercari": "7228", "exact": True, "yahoo_q": "ネオジオポケットカラー", "ebay_q": "Neo Geo Pocket Color",
                "alias": r"ネオジオ\s*ポケット\s*カラー|NEO\s*-?\s*GEO\s*POCKET\s*COLOU?R|NGPC", "neg": r"$^"},
    # リサーチ用に追加（仕入れが2万円を超えるものは自動で外れる）
    "KTR-001": {"brand": "Nintendo", "platform": "New Nintendo 3DS", "series": "New Nintendo 3DS KTR-001",
                "mercari": "8885", "exact": True, "yahoo_q": "New 3DS", "ebay_q": "New Nintendo 3DS KTR-001", "region_lock": True,
                "alias": r"(NEW|ニュー)\s*(ニンテンドー)?\s*3DS|KTR", "neg": r"LL|XL|2DS"},
    "JAN-001": {"brand": "Nintendo", "platform": "New Nintendo 2DS XL", "series": "New Nintendo 2DS LL XL JAN-001",
                "mercari": "8888", "exact": True, "yahoo_q": "New 2DS LL", "ebay_q": "New Nintendo 2DS XL", "region_lock": True,
                "alias": r"2DS\s*(LL|XL)|JAN-001", "neg": r"$^"},
    "PSP-N1000": {"brand": "Sony", "platform": "Sony PSP Go", "series": "Sony PSP Go N1000",
                  "mercari": "8900", "exact": True, "yahoo_q": "PSP go", "ebay_q": "Sony PSP Go",
                  "alias": r"PSP\s*-?\s*GO|N1000", "neg": r"$^"},
}


# ---------- デジカメ・MDウォークマン（ゲーム機と同じ仕組みで扱う） ----------
# 数字は eBay で過去90日に売れた中古の台数（2026年10月にセラーハブの売れ行き調査で確認）
CAMERA_MODELS = {"DSC-W830": 663, "DSC-W570": 670, "DSC-W800": 530, "DSC-W55": 383, "DSC-W120": 314,
                 "DSC-W350": 310, "DSC-W220": 174, "DSC-W50": 138, "DSC-W310": 132, "DSC-W100": 66, "DSC-W300": 53,
                 # 2026/10/21 追加
                 "DSC-T700": 582, "DSC-W80": 321, "DSC-TX1": 281, "DSC-W630": 227, "DSC-W320": 223,
                 # 2026/10/21 リサーチ用に追加（1,000件の枠がある間に出してみて、売れる機種を見つける）
                 "DSC-WX350": 242, "DSC-T77": 190, "DSC-T900": 157, "DSC-W810": 123, "DSC-W730": 121, "DSC-W610": 109,
                 "DSC-W150": 102, "DSC-T10": 81, "DSC-W170": 78, "DSC-W710": 78, "DSC-W200": 74, "DSC-T100": 72,
                 "DSC-W690": 68, "DSC-W650": 66, "DSC-WX200": 60, "DSC-T200": 57, "DSC-WX100": 48, "DSC-TX5": 41, "DSC-WX7": 40,
                 "DSC-T99": 376, "DSC-W530": 279, "DSC-W330": 221, "DSC-TX7": 156, "DSC-TX10": 145, "DSC-WX500": 137,
                 "DSC-W290": 128, "DSC-WX220": 116, "DSC-WX300": 111, "DSC-W510": 109, "DSC-W230": 102, "DSC-T90": 98,
                 "DSC-T70": 86, "DSC-TX30": 86, "DSC-TX66": 84, "DSC-T2": 78, "DSC-W560": 76, "DSC-T20": 73,
                 "DSC-TX55": 73, "DSC-WX50": 72, "DSC-TX20": 57, "DSC-WX80": 47, "DSC-T300": 41}
MD_MODELS = {"MZ-NE410": 62, "MZ-RH1": 29, "MZ-E620": 26, "MZ-N1": 22, "MZ-N910": 20, "MZ-N920": 15, "MZ-E900": 10,
             "MZ-N707": 51, "MZ-N505": 46}
for _m in CAMERA_MODELS:
    GAMES[_m] = {"kind": "camera", "brand": "Sony", "platform": "Sony Cyber-shot", "series": f"Sony Cyber-shot {_m}",
                 "mercari": "4007", "ebay_cat": "31388", "ebay_q": f"Sony Cyber-shot {_m}", "yahoo_q": _m}
for _m in MD_MODELS:
    GAMES[_m] = {"kind": "md", "brand": "Sony", "platform": "Sony MD Walkman", "series": f"Sony MD Walkman {_m}",
                 "mercari": "3951", "ebay_cat": "15056", "ebay_q": f"Sony MiniDisc {_m}", "yahoo_q": _m}
# ほかのメーカーのコンパクトデジカメ（型番: (メーカー, eBayの項目Series, 商品名に使う呼び名)）
OTHER_CAMERAS = {
    "S6300": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S6300"), "S8200": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S8200"),
    "S6200": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S6200"), "S7000": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S7000"),
    "S9500": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S9500"), "S9300": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S9300"),
    "S9900": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S9900"), "S3300": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S3300"),
    "IXY-650": ("Canon", "Canon PowerShot", "Canon IXY 650 PowerShot ELPH 360 HS"),
    "IXY-180": ("Canon", "Canon PowerShot", "Canon IXY 180 PowerShot ELPH 180"),   # 2026/10/21 追加
    # 2026/10/21 リサーチ用に追加
    "S3500": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S3500"), "S4300": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S4300"),
    "S6500": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S6500"), "S6800": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S6800"),
    **{_k: ("Nikon", "Nikon Coolpix", f"Nikon COOLPIX {_k}") for _k in
       ("S4100", "S6100", "S3600", "S9700", "S8100", "S6000", "S9100", "S3700", "S9600", "S8000", "S6600", "S5200")},
}
for _m, (_b, _s, _n) in OTHER_CAMERAS.items():
    GAMES[_m] = {"kind": "camera", "brand": _b, "platform": _s, "series": _n, "ser_name": _s,
                 "mercari": "4007", "ebay_cat": "31388", "ebay_q": _n, "yahoo_q": _n.split(" PowerShot")[0]}
for _m in OTHER_CAMERAS:
    GAMES[_m]["need_brand"] = True   # 型番が短く他社と重なる（例：富士フイルム FinePix S8200）ので、商品名にメーカー名が必須
GAMES["IXY-650"]["ebay_q"] = "Canon PowerShot ELPH 360 HS"   # eBayでは主にこの名前で売られている
GAMES["IXY-180"]["ebay_q"] = "Canon PowerShot ELPH 180"
# カメラ・MDのメーカー名（ほかのメーカー名が商品名にあれば、同じ型番の別商品として外す）
CAMERA_BRANDS = {
    "Sony": r"ソニー|SONY|CYBER-?SHOT|サイバーショット|WALKMAN|ウォークマン",
    "Nikon": r"ニコン|NIKON|COOLPIX|クールピクス",
    "Canon": r"キヤノン|キャノン|CANON|IXY|POWERSHOT|パワーショット",
    "Fujifilm": r"富士フイルム|富士フィルム|フジフイルム|フジフィルム|FUJI|FINEPIX|ファインピクス",
    "Olympus": r"オリンパス|OLYMPUS",
    "Panasonic": r"パナソニック|PANASONIC|LUMIX|ルミックス",
    "Casio": r"カシオ|CASIO|EXILIM|エクシリム",
    "Pentax": r"ペンタックス|PENTAX|リコー|RICOH",
    "Kodak": r"コダック|KODAK",
}


def brand_reject_reason(t, gi):
    """カメラ・MD：商品名に別メーカー名がある／（必要な機種で）自社のメーカー名がない"""
    u = unicodedata.normalize("NFKC", t or "").upper()
    own = gi.get("brand", "")
    for b, pat in CAMERA_BRANDS.items():
        if b != own and re.search(pat, u):
            return f"別メーカーの商品の可能性（{b}）"
    if gi.get("need_brand") and not re.search(CAMERA_BRANDS.get(own, r"$^"), u):
        return "商品名にメーカー名がありません（別メーカーの同じ型番の可能性）"
    return ""


# ゲーム機以外で「本体ではない・動かない」可能性が高い商品名
OTHER_HARD_WORDS = re.compile(
    r"(ケース|バッテリー|電池|充電器|ストラップ|説明書|取説|箱|リモコン|イヤホン|ディスク|パーツ|部品|ケーブル)(のみ|単品|だけ)|"
    r"部品取り|動作未確認|稼働未確認|未確認|ジャンク|ジャング|故障|不動|訳あり|難あり|画面割れ|液晶割れ|レンズ割れ|カビ")


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
    r"動作未確認|未確認|ジャンク|ジャング|故障|不動|訳あり|難あり|画面割れ|液晶割れ")
# 説明文に「本体は付属しません」など、本体が入っていないことが書かれている出品（本体に見せかけた出品を防ぐ）
NO_BODY_RE = re.compile(
    r"本体(は|が)?\s*(付属し(ません|ない)|含まれ(ません|ない)|入って(いません|いない|おりません)|ありません|ございません|"
    r"付きません|つきません|なし|無し|ない|ナシ)|本体\s*(なし|無し|ナシ|別売)|"
    r"(外箱|空箱|箱|ケース|説明書|付属品|ソフト|カセット|シェル|外装|プレート|パーツ)(だけ|のみ)の(出品|販売)|"
    r"(外箱|空箱|付属品|説明書|ケース|シェル|外装)のみ(です|となります|の出品|の販売|になります)|"
    r"(画像|写真)の(外箱|箱)のみ|部品取り|パーツ取り")


# 動作未確認・ジャンクなど、動くと言えない出品（普通の中古品として売ると返品・悪い評価になる）
JUNK_RE = re.compile(
    r"動作(は)?未確認|稼働未確認|動作未チェック|動作確認(は|を)?(して|でき|行って|おこなって)(い|お)?(ません|ない|ず|りません)|"
    r"動作確認(が|は)?(取れて|とれて|出来て)(い|お)?(ません|ない|りません)|動作確認(が|は)?(取れ|とれ|出来|でき)(ず|ません)|"
    r"動作(の)?確認(が|は|を|も)?(でき|出来|取れ|とれ|して|行って|おこなって)(い|お)?(ない|ません|ず|りません)|"
    r"(動作|通電)(の)?(確認|チェック)(が|は)?(できない|出来ない|できず|出来ず|不可|していない)|"
    r"(動作|操作|通電)(の|は|が)?(確認|チェック)(が|は|を|も)?(されて|しておら|できておら|取れておら|行われて)(い|お)?(ない|ません|ず|りません)|"
    r"電源(を|は)?(入れて|入れておら)(い|お)?(ない|ません|ず|りません)|"
    r"訳あり|訳アリ|ワケアリ|わけあり|訳有り?|"
    r"ジャン[クグ](?!では|じゃ|ではな)|ガラクタ|がらくた|通電(確認)?のみ|電源(が|は)?(入りません|入らない|つきません|つかない)|"
    r"起動(しません|しない|できません)|故障(品|中|しています|あり)|不動品|部品取り|画面割れ|液晶割れ|"
    r"(液晶|画面|モニター|ディスプレイ)(画面)?(が|は|に|には|も)?(何も|全く|まったく|一切|全然)?"
    r"(映ら|写ら|映りません|写りません|表示され(ない|ません|ず)|表示しな|つかな|点かな|点灯しな|真っ暗|真っ黒)")


# 不具合の記載（「不具合なし」「ムラはありません」のような否定は除く）
DEFECT_RE = re.compile(
    r"(誤作動|誤動作|不具合|不良|故障|モヤ|ムラ|シミ|ドット抜け|画面焼け|液晶焼け|焼き付き|反応しない|反応が悪い|"
    r"効かない|作動しない|動かない|映らない|写らない|ピントが合わない|切り替わらない|充電できない|"
    r"エラーが出|エラー表示|ノイズが|音が出ない|音飛び|黒点|黒い点|黄ばみ|勝手に|[JＪ]グレード|[JＪ]ランク)"
    r"(?![^。\n、]{0,5}?(なし|無し|ナシ|ない|なく|無く|ありません|ございません|見当たりません|見られません|確認されません))")


def junk_reason(text):
    t = unicodedata.normalize("NFKC", text or "")
    m = JUNK_RE.search(t)
    if m:
        return f"動作が確認できない出品（{m.group(0)[:15]}）"
    m = DEFECT_RE.search(t)
    return f"不具合の記載がある出品（{m.group(0)[:15]}）" if m else ""


# 商品名の「〇〇様」「〇〇様専用」は特定の人向けの専用出品（ほかの人は買えない・買うとトラブル）
RESERVED_RE = re.compile(r"(?<![仕模同多異有王皇神殿])様(?![々子式相変])|専用出品|お取り置き|取り置き中|購入不可")


def reserved_reason(title):
    t = unicodedata.normalize("NFKC", title or "")
    m = RESERVED_RE.search(t)
    return f"専用出品の可能性（{m.group(0)}）" if m else ""


def no_body_reason(text):
    t = unicodedata.normalize("NFKC", text or "")
    m = NO_BODY_RE.search(t)
    return f"説明に本体が入っていない旨の記載（{m.group(0)[:20]}）" if m else ""


GAME_MIN_PRICE_RATIO = 0.12   # 相場のこれ未満の値段は部品・ジャンクの可能性が高い


def game_reject_reason(title, model, price_jpy=None, sale_usd=None, settings=None):
    """ゲーム機として出品してはいけない理由（なければ空文字）"""
    t = unicodedata.normalize("NFKC", title or "")
    if not match_game(t, model):
        return "商品名に機種名がありません"
    if (game_info(model) or {}).get("kind") in ("camera", "md"):
        why = brand_reject_reason(t, game_info(model))
        if why:
            return why
        m = OTHER_HARD_WORDS.search(t)
        if m:
            return f"本体ではない・動かない可能性（{m.group(0)}）"
        try:
            if price_jpy and sale_usd and float(price_jpy) < float(sale_usd) * usd_jpy(settings or {}) * GAME_MIN_PRICE_RATIO:
                return "相場に比べて安すぎる（部品・ジャンクの可能性）"
        except (TypeError, ValueError):
            pass
        return ""
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
# ゲームボーイ系は古くて仕入れが高く数も少ないので、利益が大きく取れるときだけ扱う
GAMEBOY_MODELS = {"DMG-01", "MGB-001", "CGB-001", "AGB-001", "AGS-001", "OXY-001"}
GAMEBOY_MIN_PROFIT = 10000
RETRO_MODELS = {"WSC-001", "NEOP-10"}   # 古いゲーム機：液晶の不具合・返品が多いので利益が大きいときだけ
RETRO_MIN_PROFIT = 4000

# 出品枠（月350件・2万ドル）を有効に使うためのルール
MAX_PER_MODEL = 3   # 同じ機種の出品は3件まで（仕入れが安い＝利益が大きいものを残す）
SWAP_MARGIN = 1000  # 枠がいっぱいのとき、利益がこれ以上大きい候補なら一番利益の小さい出品と入れ替える
# 過去90日にeBayで売れた台数（多い機種ほど優先して出品枠を使う）
SOLD90 = {"902KC": 41, "KY-42C": 23, "601SH": 20, "A202KC": 18, "P-01G": 18, "903KC": 16, "KYF31": 13,
          "701KC": 12, "SH-02L": 11, "805SH": 9, "SH-01J": 8, "A203KC": 8, "A205SH": 7, "602SH": 7, "KYF42": 6,
          "PCH-1000": 80, "PCH-2000": 77, "PSP-3000": 73, "PSP-1000": 41, "RED-001": 34, "USG-001": 22,
          "CTR-001": 17, "CGB-001": 15, **CAMERA_MODELS, **MD_MODELS,
          **{k: 40 for k in OTHER_CAMERAS}, "IXY-180": 120, "UTL-001": 80, "WSC-001": 25, "NEOP-10": 36,
          "KTR-001": 97, "JAN-001": 163, "PSP-N1000": 67, "S4100": 170, "S6100": 169, "S3600": 147, "S9700": 141,
          "S8100": 135, "S6000": 134, "S9100": 134, "S3700": 106, "S9600": 75, "S8000": 67, "S6600": 64, "S5200": 62,
          "S3500": 190, "S4300": 123, "S6500": 114, "S6800": 91}


def model_key(q):
    return re.sub(r"[-\s]", "", (q or "").upper())


SOLD90_BY_KEY = {model_key(k): v for k, v in SOLD90.items()}


def cap_for(q):
    """機種ごとの出品数の上限：よく売れる機種ほど多く出す（90日の販売台数÷4、最低3件・最大20件）"""
    return max(MAX_PER_MODEL, min(20, round(SOLD90_BY_KEY.get(model_key(q), 0) / 4)))
MAX_COST_DEFAULT = 20000      # 仕入れ値の上限（円）。設定シートで max_cost を渡せばそちらを優先
PROFIT_RATIO = 0              # 2026/10/22 仕入れ値の割合はやめて、最低利益（2,000円・広告付きは1,500円）だけにした（以前は0.3）


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


def min_profit_for(cost, settings, model=""):
    base = max(float(settings.get("min_profit") or 2000), float(cost or 0) * PROFIT_RATIO)
    if (model or "").strip().upper() in GAMEBOY_MODELS:
        base = max(base, GAMEBOY_MIN_PROFIT)
    elif (model or "").strip().upper() in RETRO_MODELS:
        base = max(base, RETRO_MIN_PROFIT)
    return base


def _ebay_market_usd(query):
    r = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        params={"q": (game_info(query) or {}).get("ebay_q", query),
                "category_ids": (game_info(query) or {}).get("ebay_cat", GAME_EBAY_CATEGORY) if game_info(query) else "9355",
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


AD_RATE = 3            # 広告率（%）：デジカメ・MD・ゲーム機だけに広告を付ける（ガラケーは付けない）
AD_PROFIT_RELIEF = 0    # 2026/10/22 広告付きも最低利益2,000円にそろえた（以前は500円下げて1,500円）


def ad_pct(model):
    return AD_RATE if game_info(model or "") else 0


def profit(cost_jpy, sale_usd, settings, model=""):
    sale_usd = adj(sale_usd, settings)
    fx = usd_jpy(settings)
    fee = float(settings.get("fee_pct") or 15) + float(settings.get("other_pct") or 2) + ad_pct(model)
    ship = float(settings.get("ship_jpy") or 3000)
    revenue = sale_usd * fx
    p = revenue * (1 - fee / 100) - ship - cost_jpy
    return round(p), (round(p / revenue * 100, 1) if revenue else 0)


LABEL = {"on_sale": "販売中", "sold": "売り切れ", "deleted": "削除・非公開",
         "unknown": "判定できず", "blocked": "メルカリにアクセスを拒否された",
         "auction": "オークション商品"}
MIN_COUNT = 10   # eBayの相場の件数がこれ未満の機種は候補にしない


# ---------- 同じ商品が複数のサイトに出ているかを写真で見分ける ----------
IMGHASH_FILE = os.path.join(".cache", "imgthumb.json")
_imghash = {}   # 仕入れ先URL → 1枚目の写真を16×16の白黒に縮めたもの（256バイト）
# 同じ商品なら「値段＋この金額」が小さいほうを残す（メルカリ優先。Yahoo!フリマは500円以上安いときだけ残す）
SRC_PENALTY = {"メルカリ": 0, "Yahoo!フリマ": 499, "ラクマ": 599}


def load_imghash():
    try:
        with open(IMGHASH_FILE, encoding="utf-8") as f:
            _imghash.update({k: bytes.fromhex(v) for k, v in json.load(f).items()})
    except FileNotFoundError:
        pass
    except Exception as e:
        print("写真の記録を読めませんでした:", str(e)[:80])


def save_imghash():
    try:
        os.makedirs(os.path.dirname(IMGHASH_FILE), exist_ok=True)
        with open(IMGHASH_FILE, "w", encoding="utf-8") as f:
            json.dump({k: v.hex() for k, v in _imghash.items()}, f)
    except Exception as e:
        print("写真の記録を保存できませんでした:", str(e)[:80])


def dhash_url(u):
    from PIL import Image
    import io
    r = requests.get(u, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("L").resize((16, 16), Image.LANCZOS).tobytes()


def src_hash(url, images):
    if url in _imghash:
        return _imghash[url]
    if not images:
        return None
    try:
        _imghash[url] = dhash_url(images[0])
    except Exception as e:
        print("写真を比べられませんでした:", str(e)[:80])
        return None
    return _imghash[url]


def same_img(a, b):
    """同じ写真か（縮めた白黒画像の明るさの差の平均が6以下）。
    同じお店が同じ場所で撮った別の個体（差は40以上）は同じとみなさない"""
    if not a or not b or len(a) != len(b):
        return False
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a) <= 6


def src_score(url, price):
    return float(price or 0) + SRC_PENALTY.get(source_name(url), 0)


def main():
    load_imghash()
    try:
        _main()
    finally:
        save_imghash()


def _main():
    data = sheet("list")
    rows, settings = data["rows"], data.get("settings", {})
    KNOWN_MODELS[:] = [k.strip() for k in re.split(r"[,、\n]", str(settings.get("keywords") or "")) if k.strip()]
    targets = [r for r in rows if r["status"] in ("", "active")]
    # 出品中の商品を先に確認して利益を把握してから、候補を出品する（入れ替えの判断に使う）
    targets.sort(key=lambda r: 0 if r["ebay_item_id"] else 1)
    watch = [r for r in targets if r["ebay_item_id"]]
    research = [r for r in targets if not r["ebay_item_id"]]
    print(f"{now()} 監視 {len(watch)}件／仕入れ候補 {len(research)}件")
    if not targets:
        sheet("log", message=f"{now()} 正常終了：対象の商品なし")
        return

    # eBayの認証を最初に確認しておく（売り切れを見つけてから失敗するのを防ぐ）
    try:
        token = get_access_token() if watch else None
    except RuntimeError as e:
        # トークンが切れた・無効になった → すぐにメールで知らせる（このままだと売り切れても出品が止まらない）
        alert("【緊急】eBayとツールの接続が切れました",
              f"eBayのトークンが使えなくなったため、在庫チェックができません。\n\nエラー：{str(e)[:200]}\n\n{TOKEN_HOWTO}")
        raise
    updates, blocked, counts, stopped, researched, listed = [], 0, {}, 0, 0, 0
    sold_on_ebay, sold_n = {}, 0
    if watch:
        try:
            sold_on_ebay = ebay_sold_items(token)   # eBayで売れた商品（仕入れ元で買う必要がある）
            # 売れた商品を一番先に確認して、仕入れ先のURLをすぐメールで送る
            targets.sort(key=lambda r: 0 if r["ebay_item_id"] in sold_on_ebay else (1 if r["ebay_item_id"] else 2))
        except Exception as e:
            print("eBayで売れた商品の確認に失敗:", str(e)[:100])
    # ---- eBayでキャンセルが済んだ注文の行は、止めて「履歴」に移す（リストから消える） ----
    try:
        cancelled = ebay_cancelled_items(token)
    except Exception as e:
        cancelled = set()
        print("キャンセルされた注文の確認に失敗:", str(e)[:100])
    for r in rows:
        if r["ebay_item_id"] and r["ebay_item_id"] in cancelled and r["status"] != "stopped":
            updates.append({"row": r["row"], "status": "stopped",
                            "result": f"{now()} eBayの注文がキャンセルされました（出品を終了しました）"})
            sold_on_ebay.pop(r["ebay_item_id"], None)
            print("キャンセルされた注文の行を履歴に移します:", r["ebay_item_id"])
    if cancelled:
        rows = [r for r in rows if r["ebay_item_id"] not in cancelled]
        targets = [r for r in targets if r["ebay_item_id"] not in cancelled]
        watch = [r for r in watch if r["ebay_item_id"] not in cancelled]
    active_items, today = {}, datetime.now(JST).strftime("%m/%d")
    if watch:
        try:
            active_items = ebay_active_items(token)   # ウォッチ数・今の値段・出品日
            print(f"出品中 {len(active_items)}件／ウォッチあり {sum(1 for v in active_items.values() if v['watch'])}件")
        except Exception as e:
            print("出品中の商品の確認に失敗:", str(e)[:100])
    try:
        views = listing_views(active_items.keys())   # 閲覧数（売れない理由の予想に使う）
    except Exception as e:
        views = {}
        print("閲覧数の確認に失敗:", str(e)[:100])
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
        desc_pending = []   # 説明文の理由で止める出品（20件を超えたら判定ミスとみなして止めない）
        listed_per_model = {}
        for x in rows:
            if x["ebay_item_id"] and x["status"] in ("", "active"):
                k = model_key(x.get("query"))
                listed_per_model[k] = listed_per_model.get(k, 0) + 1
        quota_hit = False   # eBayの出品枠の上限に達したら、入れ替えできるときだけ出品する
        free_used = None    # 今月使う無料出品枠（出品するときに初めて数える）
        free_full = False
        day_full = False    # 今日の出品ペースの上限に達した
        unknown_twice = []  # 2回続けてメルカリの状態を判定できなかった出品（売り切れに気づけないおそれ）
        offer_targets = {}  # ウォッチされている出品 {出品番号: (今の値段, 利益2,000円の値段)}：値引きオファーを送る
        listed_profit = {}  # 出品中の商品の利益 {行番号: (機種, 利益, eBay出品番号)}

        def swap_out(model=None, cand_p=None):
            """候補の利益が出品中の一番小さい利益より SWAP_MARGIN 円以上多ければ、その出品を終了して枠を空ける"""
            pool = [(v[1], row, v) for row, v in listed_profit.items() if model is None or v[0] == model]
            if not pool or cand_p is None:
                return False
            low_p, low_row, (low_model, _, low_id) = min(pool)
            if cand_p < low_p + SWAP_MARGIN:
                return False
            ok, msg = stop_on_ebay(low_id, token or get_access_token())
            if not ok:
                return False
            updates.append({"row": low_row, "status": "stopped",
                            "result": f"利益が大きい商品（約{int(cand_p):,}円）と入れ替えのため終了（この出品の利益は約{int(low_p):,}円） → {msg}"})
            del listed_profit[low_row]
            listed_per_model[low_model] = max(0, listed_per_model.get(low_model, 1) - 1)
            return True

        dup_seen, dup_stop = [], []   # 出品中の商品の写真／同じ商品で止めるほう

        def dup_match(h, row_no):
            for d in dup_seen:
                if d["row"] != row_no and same_img(d["hash"], h):
                    return d
            return None

        def dup_note(r, price, images, ebay_id):
            """出品中の商品を登録。同じ写真の出品がすでにあれば、条件の悪いほうを止める予定に入れる"""
            h = src_hash(r["mercari_url"], images)
            if h is None:
                return
            me = {"row": r["row"], "hash": h, "ebay_item_id": ebay_id, "url": r["mercari_url"],
                  "price": price, "score": src_score(r["mercari_url"], price)}
            other = dup_match(h, r["row"])
            if not other:
                dup_seen.append(me)
                return
            lose, keep = (me, other) if me["score"] >= other["score"] else (other, me)
            lose["keep"] = keep
            dup_stop.append(lose)
            if lose is other:
                dup_seen.remove(other)
                dup_seen.append(me)

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
            acc_why = acc_note = ""
            if is_rakuma(r["mercari_url"]):
                acc, acc_why = True, "ラクマからの仕入れはやめたため"   # 出品中・候補ともに止める
            if not acc and reserved_reason(title_now):
                acc_why = reserved_reason(title_now)   # 専用出品は買えないので候補・出品とも止める
                acc = True
            if not acc and game_info(r.get("query") or ""):
                acc_why = game_reject_reason(title_now, (r.get("query") or "").strip(),
                                             (info or {}).get("price"), r.get("ebay_usd"), settings)
                acc = bool(acc_why)
                if acc and r["ebay_item_id"] and acc_why.startswith("商品名にメーカー名がありません"):
                    acc = False   # 出品中のものはメーカー名がないだけでは止めない（知らせるだけ）
                    acc_note = f"【要確認】{acc_why}"
            if not acc and status == "on_sale":
                # 説明文も確認（本体なし・箱のみ・動作未確認・不具合などの記載があれば止める）
                try:
                    det_now = ydetail if yahoo else page.evaluate(DETAIL_JS)
                    desc_now, cond_now = det_now.get("description", ""), det_now.get("condition", "")
                except Exception:
                    desc_now, cond_now = "", ""
                why = no_body_reason(title_now + "\n" + desc_now) or junk_reason(title_now + "\n" + desc_now) \
                    or ("商品の状態が「全体的に状態が悪い」" if "状態が悪い" in cond_now else "")
                if why and not r["ebay_item_id"]:
                    acc, acc_why = True, why   # 候補の段階で外す（待機中の候補も）
                elif why and r["ebay_item_id"] not in sold_on_ebay:
                    desc_pending.append({"row": r["row"], "ebay_item_id": r["ebay_item_id"], "why": why})
            if r["ebay_item_id"] and r["ebay_item_id"] in sold_on_ebay:
                # ---- eBayで売れた：仕入れ元で買うように目立たせる（この行は自動では消さない） ----
                s = sold_on_ebay[r["ebay_item_id"]]
                usd = f"${s['usd']:.2f}" if s.get("usd") else ""
                src = source_name(r["mercari_url"])
                upd["status"] = PENDING
                if s.get("usd"):
                    upd["ebay_usd"] = s["usd"]   # 実際に売れた値段（売上表に移すときに使う）
                if status == "on_sale" and (info or {}).get("price"):
                    p_txt = ""
                    if s.get("usd"):
                        fee = (float(settings.get("fee_pct") or 15) + float(settings.get("other_pct") or 2)) / 100 + ad_pct(r.get("query")) / 100
                        p = s["usd"] * usd_jpy(settings) * (1 - fee) - float(settings.get("ship_jpy") or 3000) - info["price"]
                        p_txt = f"・見込み利益 約{int(p):,}円"
                        upd["profit"] = round(p)
                    upd["cost"] = info["price"]
                    upd["result"] = (f"🔴【要購入】eBayで売れました（{usd}）。{src}でまだ買えます"
                                     f"（{int(info['price']):,}円{p_txt}）→ 商品URLから購入してください")
                else:
                    # 仕入れ元が売り切れ：代わりの商品は使わず、購入者にお詫びしてキャンセルする
                    upd["result"] = (f"⚠️【確認】【仕入れ先が売り切れ】eBayで売れました（{usd}）が、{src}はすでに{LABEL[status]}です。"
                                     "購入者にお詫びしてキャンセルしてください（キャンセルが済んだらこの行を削除してください）")
                k = model_key(r.get("query"))
                listed_per_model[k] = max(0, listed_per_model.get(k, 1) - 1)
                sold_n += 1
                name = (title_now or r.get("title") or r.get("query") or "")[:40]
                alert(f"【要仕入れ】eBayで売れました {usd}｜{name}",
                      f"{upd['result']}\n\n"
                      f"■ 仕入れ先（{src}）\n{r['mercari_url']}\n\n"
                      f"■ eBay出品番号：{r['ebay_item_id']}\n\n"
                      "購入したら、スプレッドシートの「仕入れた」にチェックを入れてください。")
            elif r["ebay_item_id"]:
                # ---- 出品中の商品：売り切れたらeBayを止める ----
                if acc:
                    token = token or get_access_token()
                    ok, msg = stop_on_ebay(r["ebay_item_id"], token)
                    upd["result"] = f"{acc_why or '説明書・充電器などの単体'}のため出品を止めました → {msg}"
                    if ok:
                        upd["status"] = "stopped"
                        stopped += 1
                elif status in ("sold", "deleted", "auction"):
                    ok, msg = stop_on_ebay(r["ebay_item_id"], token)
                    upd["result"] = f"{LABEL[status]} → {msg}"
                    if ok:
                        upd["status"] = "stopped"
                        stopped += 1
                        notify(f"【停止】{r.get('memo','')} メルカリ{LABEL[status]} → {msg}（eBay {r['ebay_item_id']}）")
                    else:
                        notify(f"【要対応】{r.get('memo','')} メルカリ{LABEL[status]}ですが{msg}（eBay {r['ebay_item_id']}）")
                elif status == "on_sale" and info.get("price"):
                    try:   # 同じ商品が別サイトにも出ていないか、1枚目の写真で確認する
                        imgs = [] if r["mercari_url"] in _imghash else \
                            (ydetail if yahoo else page.evaluate(DETAIL_JS)).get("images", [])
                        dup_note(r, info["price"], imgs, r["ebay_item_id"])
                    except Exception as e:
                        print("写真の比較に失敗:", str(e)[:80])
                    # 出品中の値段を見直す（利益2,500円が下限。ウォッチありは少しずつ、なしは下限まで下げる）
                    try:
                        q = (r.get("query") or "").strip()
                        target, floor = market_price_usd(info["price"], q, settings)
                        if floor is None:
                            floor = floor_price_usd(info["price"], q, settings)
                        act = active_items.get(r["ebay_item_id"], {})
                        if "watch" in act:
                            upd["watch"] = act["watch"]
                        if act.get("days") is not None:
                            upd["days_listed"] = int(act["days"])
                            v = views.get(r["ebay_item_id"])
                            upd["reason"] = unsold_reason(act["days"], v[0] if v else None, act.get("watch"),
                                                          act.get("price"), target, q)
                        hours_since_drop = None   # 前回の値下げからの時間（T列「最終値下げ」から読む）
                        try:
                            last = datetime.strptime(str(r.get("last_drop") or "")[:16], "%Y-%m-%d %H:%M").replace(tzinfo=JST)
                            hours_since_drop = (datetime.now(JST) - last).total_seconds() / 3600 + 0.1
                        except ValueError:
                            pass
                        new_price = decide_price(act.get("price"), target, floor, act.get("watch"),
                                                 act.get("days"), hours_since_drop)
                        if new_price:
                            # 実際の出品価格で売れたときの利益をシートに書く
                            fx = usd_jpy(settings)
                            fee = (float(settings.get("fee_pct") or 15) + float(settings.get("other_pct") or 2)) / 100 + ad_pct(r.get("query")) / 100
                            rev = new_price * fx
                            p = rev * (1 - fee) - float(settings.get("ship_jpy") or 3000) - info["price"]
                            upd["cost"] = info["price"]
                            upd["profit"] = round(p)
                            listed_profit[r["row"]] = (model_key(r.get("query")), p, r["ebay_item_id"])
                            upd["margin"] = round(p / rev * 100, 1) if rev else 0
                            cur = act.get("price")
                            same = (abs(cur - new_price) < 0.005) if cur is not None \
                                else f"${new_price:.2f}" in (r.get("result") or "")
                            if not same:   # 値段が変わるときだけeBayを更新（結果欄は「販売中」だけ。利益は想定利益の列に出る）
                                ack, errs = trading_call("ReviseInventoryStatus",
                                    f"<InventoryStatus><ItemID>{r['ebay_item_id']}</ItemID>"
                                    f"<StartPrice>{new_price:.2f}</StartPrice></InventoryStatus>", token)
                                if ack in ("Success", "Warning") and cur is not None and new_price < cur:
                                    upd["last_drop"] = now()
                            if act.get("watch"):
                                offer_targets[r["ebay_item_id"]] = (new_price, floor)
                    except Exception as e:
                        print(f"値段の見直しに失敗（eBay {r['ebay_item_id']}）:", repr(e)[:150])
            else:
                # ---- 仕入れ候補：商品名・価格・相場・利益を埋める ----
                if acc:
                    upd["result"] = f"{acc_why or '説明書・充電器などの単体'}のため候補から外しました"
                    upd["status"] = "stopped"
                    upd["list"] = False
                elif status in ("sold", "deleted", "auction"):
                    upd["result"] = f"{LABEL[status]}（候補から外しました）"
                    upd["status"] = "stopped"
                elif status == "on_sale" and info and listed_per_model.get(model_key(r.get("query")), 0) >= cap_for(r.get("query")) \
                        and not swap_out(model_key(r.get("query")), expected_profit(info.get("price"), r.get("query"), settings)):
                    upd["result"] = f"同じ機種がすでに{cap_for(r.get('query'))}件出品中のため候補から外しました"
                    upd["status"] = "stopped"
                elif status == "on_sale" and info and free_full:
                    upd["result"] = f"【待機】今月の無料出品枠（{FREE_LISTINGS}件）を使い切ったため、来月1日まで出品を待ちます"
                elif status == "on_sale" and info and day_full:
                    upd["result"] = f"【待機】今日の出品ペースの上限（{day_cap(free_used)}件）に達したため、明日以降に出品します"
                elif status == "on_sale" and info and quota_hit \
                        and not swap_out(None, expected_profit(info.get("price"), r.get("query"), settings)):
                    upd["result"] = "【待機】eBayの月間出品枠の上限のため、枠が空くまで待ちます"
                elif status == "on_sale" and info:
                    # ---- 販売中の候補はすべてeBayに出品（「出品する」のチェックは不要） ----
                    try:
                        token = token or get_access_token()
                        detail = ydetail if yahoo else page.evaluate(DETAIL_JS)
                        if free_used is None:
                            try:
                                free_used = free_listings_used(token)
                            except Exception as e:   # 数えられないときは、手数料がかからないよう出品しない
                                print("無料出品枠を確認できませんでした:", str(e)[:100])
                                free_used = FREE_LISTINGS
                            print(f"今月の無料出品枠：{free_used}件使用（月末までの自動更新を含む）／{FREE_LISTINGS}件")
                            print(f"今日の出品：{FREE_TODAY['new']}件／今日の上限{day_cap(free_used)}件（残りの枠を月末までの{FREE_TODAY['days_left']}日で割った数）")
                        if free_used >= FREE_LISTINGS - FREE_MARGIN:
                            free_full = True
                            raise RuntimeError("FREE_FULL")
                        if FREE_TODAY["new"] >= day_cap(free_used):
                            day_full = True
                            raise RuntimeError("DAY_FULL")
                        other = dup_match(src_hash(r["mercari_url"], detail.get("images", [])), r["row"])
                        if other and src_score(r["mercari_url"], info.get("price")) >= other["score"]:
                            res, err = None, (f"SKIP:同じ商品を{source_name(other['url'])}（{int(other['price'] or 0):,}円）で"
                                              f"出品中のため候補から外しました（eBay {other['ebay_item_id']}）")
                        else:
                            res, err = create_listing(r, info, detail, settings, token)
                    except Exception as e:
                        res, err = None, f"出品中にエラー：{str(e)[:200]}"
                    if free_full:
                        res, err = None, "WAIT"
                    elif day_full:
                        res, err = None, "DAYWAIT"
                    upd["list"] = False
                    if res:
                        free_used = (free_used or 0) + 1
                        FREE_TODAY["new"] += 1
                        dup_note(r, info.get("price"), detail.get("images", []), res["ebay_item_id"])
                        upd["ebay_item_id"] = res["ebay_item_id"]
                        upd["listed_at"] = now()
                        upd["result"] = f"eBayに出品しました（${res['price']:.2f}）→ 売り切れ監視を開始"
                        listed += 1
                        k = model_key(r.get("query"))
                        listed_per_model[k] = listed_per_model.get(k, 0) + 1
                        notify(f"【出品】{res['title_en']} ${res['price']:.2f}（eBay {res['ebay_item_id']}）")
                    elif err == "WAIT":
                        upd["result"] = f"【待機】今月の無料出品枠（{FREE_LISTINGS}件）を使い切ったため、来月1日まで出品を待ちます"
                    elif err == "DAYWAIT":
                        upd["result"] = f"【待機】今日の出品ペースの上限（{day_cap(free_used)}件）に達したため、明日以降に出品します"
                    elif err and err.startswith("SKIP:"):
                        upd["result"] = err[5:]
                        upd["status"] = "stopped"   # 相場では利益が出ない → 候補から外す（自動で行を削除）
                    elif err and re.search(r"limit|限度|上限|exceed", err, re.I):
                        quota_hit = True   # 出品枠の上限：利益が大きければ入れ替え、そうでなければ枠が空くまで待つ
                        upd["result"] = "【待機】eBayの月間出品枠の上限のため、枠が空くまで待ちます"
                        if swap_out(None, expected_profit(info.get("price"), r.get("query"), settings)):
                            try:
                                res2, err2 = create_listing(r, info, detail, settings, token)
                            except Exception as e:
                                res2, err2 = None, str(e)[:200]
                            if res2:
                                upd["ebay_item_id"] = res2["ebay_item_id"]
                                upd["listed_at"] = now()
                                upd["result"] = f"利益が大きいため入れ替えて出品しました（${res2['price']:.2f}）→ 売り切れ監視を開始"
                                listed += 1
                                k = model_key(r.get("query"))
                                listed_per_model[k] = listed_per_model.get(k, 0) + 1
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
                if r.get("ebay_item_id") and str(r.get("result") or "").startswith("判定できず"):
                    unknown_twice.append(r)
            if acc_note and upd.get("status") != "stopped":
                upd["result"] = f"{acc_note} / {upd.get('result') or ''}"
            updates.append(upd)
            if len(updates) >= 10:   # 途中で止まっても結果が残るよう、10件ごとにシートへ書く
                sheet("update", updates=updates)
                updates = []
            time.sleep(random.uniform(0.5, 1.5) if yahoo else random.uniform(1.5, 3))

        # ---- 出品中なのにメルカリの状態が2回続けて読めない商品があれば知らせる（3時間に1回まで） ----
        if unknown_twice:
            flag = os.path.join(".cache", "unknown_alert.txt")
            try:
                last = float(open(flag, encoding="utf-8").read().strip() or 0)
            except (OSError, ValueError):
                last = 0
            if time.time() - last > 3 * 3600:
                alert(f"【確認】メルカリの状態を読めない出品が{len(unknown_twice)}件あります",
                      "30分以上、メルカリで売り切れたかどうかを確認できていません。eBayで売れたら、"
                      "仕入れ先でまだ買えるか確認してから対応してください。\n\n"
                      + "\n".join(f"eBay {x['ebay_item_id']}：{x.get('title', '')[:30]} {x['mercari_url']}" for x in unknown_twice[:30]))
                os.makedirs(".cache", exist_ok=True)
                with open(flag, "w", encoding="utf-8") as fh:
                    fh.write(str(time.time()))

        # ---- 見張る件数が増えて30分に間に合わなくなりそうなら知らせる（見張りを2本に分ける目安。7日に1回まで） ----
        if len(targets) >= SPLIT_AT:
            flag = os.path.join(".cache", "split_alert.txt")
            try:
                last = float(open(flag, encoding="utf-8").read().strip() or 0)
            except (OSError, ValueError):
                last = 0
            if time.time() - last > 7 * 86400:
                alert(f"【お知らせ】在庫の見張りが{len(targets)}件になりました（2本に分ける目安）",
                      f"見張る件数が{SPLIT_AT}件を超えたので、このままだと30分ごとの見張りに間に合わなくなります。\n"
                      "Claudeに「在庫の見張りを2本に分けて」と頼んでください（費用はかかりません）。")
                os.makedirs(".cache", exist_ok=True)
                with open(flag, "w", encoding="utf-8") as fh:
                    fh.write(str(time.time()))

        # ---- ウォッチしている人に値引きオファーを送る（利益2,000円は必ず残す） ----
        try:
            send_watch_offers(offer_targets)
        except Exception as e:
            print("値引きオファーの送信に失敗:", str(e)[:150])

        # ---- 無料出品枠を使い切ったら、その月に1回だけ知らせる ----
        if free_full:
            flag = os.path.join(".cache", "freefull.txt")
            month = datetime.now(PT).strftime("%Y-%m")
            try:
                done = open(flag, encoding="utf-8").read().strip() == month
            except OSError:
                done = False
            if not done:
                alert(f"【お知らせ】今月の無料出品枠（{FREE_LISTINGS}件）を使い切りました",
                      f"手数料がかからないよう、来月1日まで新しい出品を止めています（使用：{free_used}件）。\n"
                      "ストアのプランを見直すときの参考にしてください。")
                os.makedirs(".cache", exist_ok=True)
                with open(flag, "w", encoding="utf-8") as fh:
                    fh.write(month)

        # ---- 説明文に問題があった出品を止める（一度に20件を超えたら判定ミスの可能性が高いので止めずに知らせる） ----
        if len(desc_pending) > DESC_STOP_MAX:
            alert(f"【確認】説明文の判定で{len(desc_pending)}件が引っかかりました（止めていません）",
                  "一度にこれほど多く引っかかるのは判定の不具合の可能性が高いため、出品は止めていません。\n\n"
                  + "\n".join(f"eBay {d['ebay_item_id']}：{d['why']}" for d in desc_pending[:40]))
        else:
            for d in desc_pending:
                ok, msg = stop_on_ebay(d["ebay_item_id"], token or get_access_token())
                updates.append({"row": d["row"], "status": "stopped" if ok else None,
                                "result": f"{d['why']}のため出品を止めました → {msg}"})
                if ok:
                    stopped += 1

        # ---- 同じ商品が別サイトにも出ていて、両方eBayに出品していたら条件の悪いほうを止める ----
        print(f"写真を確認した出品中の商品：{len(dup_seen) + len(dup_stop)}件／同じ商品：{len(dup_stop)}件")
        done_rows = set()
        for d in dup_stop:
            if d["row"] in done_rows or not d.get("ebay_item_id"):
                continue
            done_rows.add(d["row"])
            k = d["keep"]
            ok, msg = stop_on_ebay(d["ebay_item_id"], token or get_access_token())
            updates.append({"row": d["row"], "status": "stopped" if ok else None,
                            "result": f"同じ商品が{source_name(k['url'])}にも出ているため停止"
                                      f"（{source_name(k['url'])} {int(k['price'] or 0):,}円のほうを残しました） → {msg}"})
            if ok:
                stopped += 1
            print("同じ商品のため停止:", d["ebay_item_id"], "残した出品:", k["ebay_item_id"], msg)

        # ---- 「要仕入れ」の行：まだ仕入れていないうちに仕入れ先が売り切れたら知らせる ----
        for r in [x for x in rows if x["status"] == PENDING and x.get("list") is not True]:
            if "【確認】" in (r.get("result") or ""):
                continue   # 一度知らせたものは繰り返さない
            try:
                if is_yahoo(r["mercari_url"]):
                    st, _, _ = check_yahoo(r["mercari_url"])
                else:
                    st, _ = check_mercari(page, r["mercari_url"])
            except Exception:
                continue
            if st not in ("sold", "deleted"):
                continue
            msg = ("⚠️【確認】仕入れ先が売り切れになりました。自分で買った場合は「仕入れた」にチェックしてください。"
                   "買っていない場合は、購入者にお詫びしてキャンセルしてください（キャンセルが済んだらこの行を削除してください）")
            updates.append({"row": r["row"], "result": msg})
            alert(f"【確認】仕入れ先が売り切れました｜{(r.get('title') or r.get('query') or '')[:40]}",
                  f"{msg}\n\n■ 仕入れ先\n{r['mercari_url']}\n\n■ eBay出品番号：{r['ebay_item_id']}")
        browser.close()

    sheet("update", updates=updates)
    try:
        sheet("cleanup")   # 止めた行（売り切れ・候補から外したもの・eBay終了済み）を「履歴」シートに移して削除
    except Exception:
        pass
    if blocked:
        notify(f"【注意】メルカリにアクセスを拒否されました（{blocked}件）。クラウドからのアクセスが制限されている可能性があります。")
    # 記録には件数だけを出す（URLや出品番号は出さない）
    summary = "、".join(f"{LABEL[k]} {v}件" for k, v in counts.items())
    try:
        # 売上シートに、注文番号・税・手数料・受取額を書き込む（売れた商品を仕入れ済みにした後に反映）
        if token:
            orders = ebay_orders(token)
            try:
                ad_fees = ebay_ad_fees()   # 広告経由で売れた注文の広告費（権限がまだなければ None）
            except Exception as e:
                ad_fees = None
                print("広告費の確認に失敗:", str(e)[:100])
            if ad_fees is not None:
                for o in orders:
                    o["ad_fee"] = ad_fees.get(o["order_id"], 0)   # 広告経由でなければ 0
            try:   # 出品手数料など広告以外のeBayの費用を経費シートへ（同じ明細は二重に入らない）
                sheet("expenses_sync", items=NON_SALE_OTHER, fx=round(usd_jpy(settings), 2))
            except Exception as e:
                print("経費の記録に失敗:", str(e)[:100])
            if orders:
                res = sheet("sales_sync", orders=orders, fx=round(usd_jpy(settings), 2),
                            ad_models=[model_key(k) for k in GAMES], ad_rate=AD_RATE)   # 広告を付ける機種（売上の広告費の計算に使う）
                # 仕入れ済みになって初めて売上シートに入った注文 → 購入者にお礼と発送予定日を送る
                for o in orders:
                    if o["item_id"] in (res.get("new_ids") or []) and o.get("buyer"):
                        send_thanks(o, token)
    except Exception as e:
        print("売上シートの更新に失敗:", str(e)[:100])
    msg = f"{now()} 正常終了：{summary}／eBayで売れた {sold_n}件／eBay停止 {stopped}件／利益計算 {researched}件／出品 {listed}件"
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
        p, rate = profit(cost, float(sale), settings, query)
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


PRICE_FLOOR_PROFIT = 2000   # 出品価格はこの利益（円）が残る値段より下げない
NEW_LISTING_DAYS = 3        # 出品から3日間は相場の値段で様子を見る
WATCH_STEP_USD = 2.0        # ウォッチされている商品は $2（約300円）ずつ値下げ（2026/10/22 $0.5→$2）
WATCH_INTERVAL_H = 3        # 値下げの間隔（時間）（2026/10/22 1時間→3時間）


def floor_price_usd(cost, query, settings):
    """最低利益（2,000円か、仕入れ値・機種ごとの最低利益の高いほう）が残る最低価格"""
    fx = usd_jpy(settings)
    fee = (float(settings.get("fee_pct") or 15) + float(settings.get("other_pct") or 2)) / 100 + ad_pct(query) / 100
    ship = float(settings.get("ship_jpy") or 3000)
    min_profit = max(min_profit_for(cost, settings, query), PRICE_FLOOR_PROFIT)
    if ad_pct(query):
        min_profit = max(min_profit - AD_PROFIT_RELIEF, 1500)   # 広告代の分だけ最低利益を下げて、値段を上げすぎない
    return math.ceil((cost + ship + min_profit) / max(0.05, 1 - fee) / fx) - 0.01


def decide_price(cur, target, floor, watch, days, hours_since_drop):
    """出品中の値段を決める。
    ・ウォッチあり：3時間ごとに$2ずつ下げる（下限まで）。別に値引きオファーも送る
    ・ウォッチなし：すぐに下限の値段（利益2,000円）まで下げる"""
    if cur is None:
        return max(target or floor, floor)
    if watch:   # ウォッチが付いたら、出品からの日数に関係なく少しずつ下げる
        if hours_since_drop is not None and hours_since_drop < WATCH_INTERVAL_H:
            return max(cur, floor)
        return max(round(cur - WATCH_STEP_USD, 2), floor)
    return floor


def market_price_usd(cost, query, settings):
    """売れている相場に合わせた値段。利益が最低額（設定の最低利益）を下回るなら None"""
    sale, n = ebay_market_usd(query) if query else (None, 0)
    if not sale:
        return None, None
    fx = usd_jpy(settings)
    fee = (float(settings.get("fee_pct") or 15) + float(settings.get("other_pct") or 2)) / 100
    ship = float(settings.get("ship_jpy") or 3000)
    floor = floor_price_usd(cost, query, settings)                                   # 最低利益が残る最低価格
    target = math.floor(adj(sale, settings)) + 0.99                                  # 実際に売れている値段の目安
    return target, floor


def expected_profit(cost, query, settings):
    """候補を今出品したときに見込める利益（円）。相場が取れなければ None"""
    try:
        target, floor = market_price_usd(float(cost), (query or "").strip(), settings)
        if target is None or target < floor:
            return None
        fx = usd_jpy(settings)
        fee = (float(settings.get("fee_pct") or 15) + float(settings.get("other_pct") or 2)) / 100 + ad_pct(query) / 100
        return target * fx * (1 - fee) - float(settings.get("ship_jpy") or 3000) - float(cost)
    except (TypeError, ValueError):
        return None


def list_price_usd(cost, settings):
    fx = usd_jpy(settings)
    fee = (float(settings.get("fee_pct") or 15) + float(settings.get("other_pct") or 2)) / 100
    margin = float(settings.get("target_margin") or 20) / 100
    ship = float(settings.get("ship_jpy") or 3000)
    revenue = (cost + ship) / max(0.05, 1 - fee - margin)
    return math.ceil(revenue / fx) - 0.01


MIN_PIC_SIDE = 500   # eBayの写真ルール：長い辺が500ピクセル以上


def small_picture_bytes(u):
    """写真が小さすぎる（長い辺が500ピクセル未満）ときだけ、800ピクセルに拡大した画像を返す。十分大きければNone"""
    try:
        from PIL import Image
        import io
        r = requests.get(u, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content))
        w, h = im.size
        if max(w, h) >= MIN_PIC_SIDE:
            return None
        scale = 800 / max(w, h)
        im = im.convert("RGB").resize((round(w * scale), round(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92)
        return buf.getvalue()
    except Exception as e:
        print("写真の大きさを確認できませんでした:", str(e)[:100])
        return None


def upload_picture_bytes(data, token):
    """画像データをそのままeBayに送る（小さい写真を拡大したときに使う）"""
    boundary = "GTRBOUNDARY" + str(random.randint(10 ** 8, 10 ** 9))
    xml = ('<?xml version="1.0" encoding="utf-8"?>'
           '<UploadSiteHostedPicturesRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
           '<PictureName>gtr</PictureName><PictureSet>Supersize</PictureSet></UploadSiteHostedPicturesRequest>')
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"XML Payload\"\r\n"
            f"Content-Type: text/xml;charset=utf-8\r\n\r\n{xml}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"image.jpg\"\r\n"
            "Content-Type: image/jpeg\r\nContent-Transfer-Encoding: binary\r\n\r\n").encode("utf-8") \
        + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
    r = requests.post(TRADING_URL, headers={
        "X-EBAY-API-CALL-NAME": "UploadSiteHostedPictures", "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": "1209", "X-EBAY-API-IAF-TOKEN": token,
        "Content-Type": f"multipart/form-data; boundary={boundary}"}, data=body, timeout=60)
    root = ET.fromstring(r.content)
    return root.findtext("e:SiteHostedPictureDetails/e:FullURL", default="", namespaces=NS)


def upload_pictures(urls, token):
    out = []
    for u in urls:
        small = small_picture_bytes(u)
        if small:
            full = upload_picture_bytes(small, token)   # 小さい写真は拡大してから送る
        else:
            ack, errors, root = trading_call_root(
                "UploadSiteHostedPictures",
                f"<ExternalPictureURL>{escape(u)}</ExternalPictureURL><PictureSet>Supersize</PictureSet>", token)
            full = root.findtext("e:SiteHostedPictureDetails/e:FullURL", default="", namespaces=NS)
        if full:
            out.append(full)
    return out


def ebay_active_items(token):
    """出品中の商品 {出品番号: {"watch": ウォッチ数, "price": 今の値段, "days": 出品からの日数}}"""
    out = {}
    nowu = datetime.now(ZoneInfo("UTC"))
    for page_no in range(1, 6):
        ack, errs, root = trading_call_root(
            "GetMyeBaySelling",
            "<ActiveList><Include>true</Include>"
            f"<Pagination><EntriesPerPage>200</EntriesPerPage><PageNumber>{page_no}</PageNumber></Pagination></ActiveList>",
            token)
        if ack not in ("Success", "Warning"):
            raise RuntimeError("出品中の商品の取得に失敗：" + " / ".join(errs)[:200])
        for it in root.iter("{" + NS["e"] + "}Item"):
            item_id = it.findtext("e:ItemID", default="", namespaces=NS)
            if not item_id:
                continue
            price = it.findtext("e:SellingStatus/e:CurrentPrice", default="", namespaces=NS) or \
                it.findtext("e:BuyItNowPrice", default="", namespaces=NS)
            start = it.findtext("e:ListingDetails/e:StartTime", default="", namespaces=NS)
            days = None
            if start:
                try:
                    days = (nowu - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds() / 86400
                except ValueError:
                    days = None
            out[item_id] = {"watch": int(it.findtext("e:WatchCount", default="0", namespaces=NS) or 0),
                            "price": float(price) if price else None, "days": days}
        pages = root.findtext("e:ActiveList/e:PaginationResult/e:TotalNumberOfPages", default="1", namespaces=NS)
        if page_no >= int(pages or 1):
            break
    return out


def ebay_orders(token, days=60):
    """最近の注文（注文番号・合計・税・手数料・受取額）を出品番号ごとに返す"""
    since = (datetime.now(ZoneInfo("UTC")) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    r = requests.get("https://api.ebay.com/sell/fulfillment/v1/order",
                     params={"filter": f"creationdate:[{since}..]", "limit": 200},
                     headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    out = []
    for o in r.json().get("orders", []):
        total = float(((o.get("pricingSummary") or {}).get("total") or {}).get("value") or 0)
        fee = float((o.get("totalMarketplaceFee") or {}).get("value") or 0)
        shipped = order_shipped_date(o, token)
        for li in o.get("lineItems", []):
            tax = sum(float((t.get("amount") or {}).get("value") or 0) for t in li.get("ebayCollectAndRemitTaxes", []))
            out.append({"item_id": str(li.get("legacyItemId") or ""), "order_id": o.get("orderId", ""),
                        "buyer": (o.get("buyer") or {}).get("username", ""),
                        "ship_by": (li.get("lineItemFulfillmentInstructions") or {}).get("shipByDate", ""),
                        "date": (o.get("creationDate") or "")[:10], "total": round(total, 2), "tax": round(tax, 2),
                        "fee": round(fee, 2), "payout": round(total - fee, 2),   # 税はeBayが別に預かるので、受取額は合計−手数料
                        "shipped": shipped})
    return out


THANKS_MSG = ("Hi! Thank you so much for your purchase. Your item is being prepared with care, "
              "and we will ship it by {date} via FedEx. You'll receive tracking information as soon as it ships. "
              "If you have any questions, feel free to message us anytime!")


def send_thanks(o, token):
    """仕入れが済んだ注文の購入者に、お礼と発送予定日のメッセージを送る"""
    date = ""
    if o.get("ship_by"):
        d = datetime.fromisoformat(o["ship_by"].replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Tokyo"))
        date = d.strftime("%B ") + str(d.day)
    body = THANKS_MSG.format(date=date or "the handling time shown in the listing")
    ack, errs, root = trading_call_root(
        "AddMemberMessageAAQToPartner",
        f"<ItemID>{o['item_id']}</ItemID><MemberMessage><Subject>Thank you for your purchase!</Subject>"
        f"<Body>{escape(body)}</Body><QuestionType>General</QuestionType>"
        f"<RecipientID>{escape(o['buyer'])}</RecipientID></MemberMessage>", token)
    ok = ack in ("Success", "Warning")
    print("購入者へのお礼メッセージ:", o["item_id"], "送信" if ok else "失敗 " + " / ".join(errs)[:200])
    return ok


def thanks(item_ids):
    """指定した出品番号の注文の購入者に、お礼メッセージを手動で送る（1回だけの作業）"""
    token = get_access_token()
    for o in ebay_orders(token):
        if o["item_id"] in item_ids:
            send_thanks(o, token)


def order_shipped_date(o, token):
    """発送日（CPaSSが追跡番号をeBayに入れた日・日本時間）。未発送なら空"""
    if not o.get("fulfillmentHrefs"):
        return ""
    try:
        r = requests.get(f"https://api.ebay.com/sell/fulfillment/v1/order/{o['orderId']}/shipping_fulfillment",
                         headers={"Authorization": f"Bearer {token}"}, timeout=30)
        ds = [x["shippedDate"] for x in r.json().get("fulfillments", []) if x.get("shippedDate")]
        if not ds:
            return ""
        d = datetime.fromisoformat(min(ds).replace("Z", "+00:00"))
        return d.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
    except Exception as e:
        print("発送日の取得に失敗:", str(e)[:100])
        return ""


def ebay_sold_items(token, days=30):
    """最近eBayで売れた出品 {出品番号: {"usd": 売れた値段, "buyer": 購入者ID, "date": 日時}}"""
    out = {}
    for page_no in range(1, 6):
        ack, errs, root = trading_call_root(
            "GetMyeBaySelling",
            f"<SoldList><Include>true</Include><DurationInDays>{days}</DurationInDays>"
            f"<Pagination><EntriesPerPage>200</EntriesPerPage><PageNumber>{page_no}</PageNumber></Pagination></SoldList>",
            token)
        if ack not in ("Success", "Warning"):
            raise RuntimeError("売れた商品の取得に失敗：" + " / ".join(errs)[:200])
        for t in root.iter("{" + NS["e"] + "}Transaction"):
            item_id = t.findtext("e:Item/e:ItemID", default="", namespaces=NS)
            if not item_id:
                continue
            usd = (t.findtext("e:TotalTransactionPrice", default="", namespaces=NS)
                   or t.findtext("e:TransactionPrice", default="", namespaces=NS))
            out[item_id] = {"usd": float(usd) if usd else None,
                            "buyer": t.findtext("e:Buyer/e:UserID", default="", namespaces=NS),
                            "date": t.findtext("e:CreatedDate", default="", namespaces=NS)}
        pages = root.findtext("e:SoldList/e:PaginationResult/e:TotalNumberOfPages", default="1", namespaces=NS)
        if page_no >= int(pages or 1):
            break
    return out


def ebay_cancelled_items(token, days=30):
    """キャンセルが済んだeBayの注文の出品番号"""
    since = (datetime.now(ZoneInfo("UTC")) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    r = requests.get("https://api.ebay.com/sell/fulfillment/v1/order",
                     params={"filter": f"creationdate:[{since}..]", "limit": 200},
                     headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return {str(li.get("legacyItemId") or "") for o in r.json().get("orders", [])
            if (o.get("cancelStatus") or {}).get("cancelState") == "CANCELED"
            for li in o.get("lineItems", [])} - {""}


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


# ---------- 無料の出品枠（ベーシックストア：月1,000件）を超えないようにする ----------
MAX_WAIT = 18 * 60          # 在庫の見張りを待つのは合計18分まで（それ以上は待たずに探す）
DISCOVER_BUDGET = 18 * 60   # 候補さがしで探す時間の上限（秒）。機種が多いので、残りは次の回に回す
SPLIT_AT = 230   # 見張る件数がこれ以上になったら、見張りを2本に分けるようメールで知らせる
FREE_LISTINGS = 1000   # 2026/10/21 ベーシックストア契約（それまでは250件）
DESC_STOP_MAX = 20     # 説明文の理由で一度に止める上限（超えたら判定ミスとみなして止めずに知らせる）
FREE_MARGIN = 5        # 数え違いに備えて、245件で止める
PT = ZoneInfo("America/Los_Angeles")   # eBayの月の区切り（米国太平洋時間）


FREE_TODAY = {"new": 0, "days_left": 30}


def day_cap(free_used):
    """今日出してよい新しい出品の件数：残りの無料枠を月末までの日数で割る（月の途中で枠を使い切らないため）"""
    left = (FREE_LISTINGS - FREE_MARGIN) - free_used + FREE_TODAY["new"]
    return max(0, math.ceil(left / FREE_TODAY["days_left"]))


def free_listings_used(token):
    """今月使う無料出品枠の件数（今月出した件数＋30日ごとの自動更新。出品中のものは月末までの更新も先に数える）"""
    now_pt = datetime.now(PT)
    m0 = now_pt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    m1 = (m0 + timedelta(days=32)).replace(day=1)
    f = lambda d: d.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    used, page_no = 0, 1
    today0 = now_pt.replace(hour=0, minute=0, second=0, microsecond=0)
    FREE_TODAY["new"] = 0
    FREE_TODAY["days_left"] = max(1, (m1 - today0).days)
    while page_no <= 20:
        ack, errs, root = trading_call_root(
            "GetSellerList",
            f"<EndTimeFrom>{f(m0)}</EndTimeFrom><EndTimeTo>{f(m0 + timedelta(days=118))}</EndTimeTo>"
            "<GranularityLevel>Coarse</GranularityLevel>"
            f"<Pagination><EntriesPerPage>200</EntriesPerPage><PageNumber>{page_no}</PageNumber></Pagination>", token)
        if ack not in ("Success", "Warning"):
            raise RuntimeError("出品の件数を数えられませんでした：" + " / ".join(errs)[:200])
        for it in root.iter("{" + NS["e"] + "}Item"):
            st = it.findtext("e:ListingDetails/e:StartTime", default="", namespaces=NS)
            en = it.findtext("e:ListingDetails/e:EndTime", default="", namespaces=NS)
            status = it.findtext("e:SellingStatus/e:ListingStatus", default="", namespaces=NS)
            if not st:
                continue
            t = datetime.fromisoformat(st.replace("Z", "+00:00"))
            if t >= today0:
                FREE_TODAY["new"] += 1   # 今日（米国太平洋時間）新しく出した件数
            end = datetime.fromisoformat(en.replace("Z", "+00:00")) if en else m1
            last = m1 if status == "Active" else min(end, m1)   # 出品中なら月末までの更新も数える
            while t < last:
                if t >= m0:
                    used += 1
                t += timedelta(days=30)
        pages = int(root.findtext("e:PaginationResult/e:TotalNumberOfPages", default="1", namespaces=NS) or 1)
        if page_no >= pages:
            break
        page_no += 1
    return used


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


PHONE_CATEGORY = re.compile(r"携帯電話本体|スマートフォン本体|PHS本体|本体[(（]|テレビゲーム.*>\s*本体|コンパクトデジタルカメラ|ポータブルMDプレーヤー")


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


# 1点ものの中古品であることを正直に伝えて、ウォッチしている人に早めの購入を促す
URGENCY_HTML = ("<p><b>Only 1 available.</b> This is a one-of-a-kind used item and we have just this single unit. "
                "Once it sells, it will not be restocked, so please don't wait too long if you would like it!</p>")


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
        noun = {"camera": "Digital Camera", "md": "MiniDisc Player"}.get(gi.get("kind"), "Handheld Console")
        title_en = re.sub(r"\s+", " ", f"{gi['series']} {noun} {color} Japan").strip()[:80]
    else:
        title_en = build_title(model, title_jp, carrier, unlocked, color)
    tr = sheet("translate", texts=[detail.get("condition", ""), clean_desc(detail.get("description", ""))])["texts"]
    cond_en, desc_en = tr[0], clean_desc(tr[1])
    if gi:
        desc_html = (
            f"<h2>{escape(title_en)}</h2>"
            f"<p><b>Model:</b> {escape(model)}<br><b>Platform:</b> {escape(gi['platform'])}<br>"
            f"<b>Version:</b> Japanese model<br><b>Condition:</b> {escape(cond_en)}</p>"
            + {"camera": "<p>Japanese domestic model digital camera. The menu language may be Japanese only, so please check before purchase. "
                         "Only the items shown in the photos are included (battery, charger, memory card and accessories are included only if pictured).</p>",
               "md": "<p>Japanese domestic model MiniDisc player. Only the items shown in the photos are included "
                     "(battery, charger, remote, discs and accessories are included only if pictured).</p>"}.get(gi.get("kind"),
            "<p>Japanese handheld game console. Only the items shown in the photos are included "
            "(charger, memory card, games and accessories are included only if pictured).</p>")
            + (f"<h3>Notes from the original Japanese description (machine translated)</h3><p>{escape(desc_en).replace(chr(10), '<br>')}</p>" if desc_en else "")
            + URGENCY_HTML + "<p>Ships from Japan. Thank you for looking!</p>")
        if gi.get("region_note"):
            desc_html = desc_html.replace(
                "<p>Ships from Japan.", f"<p><b>Region:</b> {escape(gi['region_note'])}</p><p>Ships from Japan.")
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
        + URGENCY_HTML + "<p>Ships from Japan. Thank you for looking!</p>")
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
    bad = not_a_phone(r, title_jp, detail) or no_body_reason(title_jp + "\n" + detail.get("description", "")) \
        or junk_reason(title_jp + "\n" + detail.get("description", ""))
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
        return None, f"SKIP:相場(${target:.2f})では利益{int(min_profit_for(cost, settings, model)):,}円が残らないため出品しません"
    price = target
    pics = upload_pictures(detail.get("images", []), token)
    if not pics:
        return None, "写真をeBayに取り込めなかったため出品を中止しました（もう一度チェックすると再挑戦します）"

    cond_id = 1000 if "新品" in detail.get("condition", "") else 3000
    gi = game_info(model)
    if gi:
        category_id = gi.get("ebay_cat", GAME_EBAY_CATEGORY)
        specifics = [("Brand", gi["brand"]), ("Model", model), ("Series", gi.get("ser_name", "Sony Cyber-shot")),
                     ("Type", "Point & Shoot"), ("Color", color or "As Pictured")] if gi.get("kind") == "camera" else \
            [("Brand", gi["brand"]), ("Model", model), ("Type", "Portable MiniDisc Player"),
             ("Color", color or "As Pictured")] if gi.get("kind") == "md" else [("Brand", gi["brand"]), ("Model", model), ("Platform", gi["platform"]),
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
    sku = sku_for(model) or re.sub(r"[^A-Za-z0-9]", "", r["mercari_url"].rstrip("/").split("/")[-1])[:50]
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
            const t = a.innerText.replace(/\\d+\\s*%\\s*OFF/gi, '');   // メルカリShopsの「6%OFF」を値段と間違えない
            const ys = [...t.matchAll(/[¥￥]\\s*([\\d,]+)/g)].map(x => parseInt(x[1].replace(/,/g, ''), 10));
            const m = ys.length ? [null, String(Math.min(...ys))] : t.match(/([\\d,]+)/);   // 値引き前と後があれば安いほう
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
YAHOO_OK_CATEGORIES = {38419, 37849, 37661, 37697, 37799, 37808, 38155, 19801, 49488}   # ＋コンパクトデジカメ・ポータブルMD   # 携帯電話・PSP・Vita・DS・GB・GBA・3DS の本体


def is_yahoo(url):
    """ブラウザを使わずに確認できる仕入れ先（Yahoo!フリマ・ラクマ）"""
    return "paypayfleamarket.yahoo.co.jp/item/" in url or is_rakuma(url)


def is_rakuma(url):
    return "item.fril.jp/" in (url or "")


def source_name(url):
    return "ラクマ" if is_rakuma(url) else "Yahoo!フリマ" if "paypayfleamarket" in (url or "") else "メルカリ"


def yahoo_item(url):
    item_id = url.rstrip("/").split("/item/")[-1].split("?")[0]
    r = requests.get(f"{YAHOO_API}/item/v2/items/{item_id}", headers=YAHOO_HEADERS, timeout=30)
    return r.status_code, (r.json() if r.status_code == 200 else {})


def check_yahoo(url):
    """メルカリの check_mercari と同じ形 (status, info, detail) を返す（ラクマのURLならラクマを確認）"""
    if is_rakuma(url):
        return check_rakuma(url)
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


PENDING = "要仕入れ"   # eBayで売れて、まだ仕入れていない行の状態
MERCARI_FIXED = ("&d664efe3-ae5a-4824-b729-e789bf93aba9=B38F1DC9286E0B80812D9B19DB14298C1FF1116CA8332D9EE9061026635C9088"   # 出品形式：通常出品（オークションを除く）
                 "&item_condition_id=1%2C2%2C3%2C4%2C5")   # 商品の状態：「全体的に状態が悪い」を除く


def find_alternative(page, query, sold_usd, settings, skip=()):
    """eBayで売れたのに仕入れ元が売り切れていたとき、同じ機種で利益が残る代わりを探す（利益が一番大きいもの）"""
    q = (query or "").strip()
    if not q or not sold_usd:
        return None
    gi = game_info(q)
    items = []
    url = ("https://jp.mercari.com/search?status=on_sale&sort=price&order=asc"
           f"&category_id={gi['mercari'] if gi else '860'}&keyword="
           + ("" if gi and gi.get("exact") else requests.utils.quote(q)) + MERCARI_FIXED)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_selector('a[href*="/item/m"], a[href*="/shops/product/"]', timeout=25000)
        items += [dict(it, exact=bool(gi and gi.get("exact"))) for it in page.evaluate(SEARCH_JS)]
    except Exception:
        pass
    try:
        items += yahoo_search(gi.get("yahoo_q", q) if gi else q)
    except Exception:
        pass
    fx = usd_jpy(settings)
    fee = (float(settings.get("fee_pct") or 15) + float(settings.get("other_pct") or 2)) / 100 + ad_pct(q) / 100
    ship = float(settings.get("ship_jpy") or 3000)
    best = None
    for it in items:
        u = it["url"].split("?")[0]
        if u in skip or not it.get("price") or it.get("auction") or it["price"] > max_cost(settings):
            continue
        title = it.get("title") or ""
        if not it.get("exact") and not has_model(title, q) and not (gi and gi.get("alias") and match_game(title, q)):
            continue   # 同じ機種と確認できないものは使わない
        if looks_like_accessory(title) or is_accessory(title, it["price"], sold_usd, settings):
            continue
        if gi and game_reject_reason(title, q, it["price"], sold_usd, settings):
            continue
        p = sold_usd * fx * (1 - fee) - ship - it["price"]
        if p > 0 and (best is None or p > best["profit"]):
            best = {"url": u, "price": it["price"], "profit": p, "title": title}
    return best


_mon_check = {"t": 0, "busy": False}


def monitor_running():
    """在庫の見張りが動いているか（動いている間は候補さがしがメルカリを見に行くのを待つ）"""
    tok, repo = os.environ.get("GH_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not tok or not repo:
        return False
    if time.time() - _mon_check["t"] < 45:
        return _mon_check["busy"]
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/actions/workflows/monitor.yml/runs",
                         params={"status": "in_progress", "per_page": 1},
                         headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}, timeout=15)
        busy = r.status_code == 200 and r.json().get("total_count", 0) > 0
    except Exception:
        busy = False
    _mon_check.update(t=time.time(), busy=busy)
    return busy


SHARDS = int(os.environ.get("SHARDS") or 1)   # 候補さがしを何本に分けて同時に動かすか（discover.yml で指定）
SHARD = int(os.environ.get("SHARD") or 0)     # この候補さがしが受け持つ番号（0から）


def discover():
    try:
        if SHARD == 0:
            token_warning()   # トークンの期限が近ければ知らせる（1日1回・1本目だけ）
    except Exception as e:
        print("トークンの期限の確認に失敗:", str(e)[:100])
    data = sheet("list")
    settings = data.get("settings", {})
    known = {r["mercari_url"].split("?")[0] for r in data["rows"]}
    keywords = [k.strip() for k in re.split(r"[,、\n]", str(settings.get("keywords") or "ガラケー")) if k.strip()]
    KNOWN_MODELS[:] = keywords
    min_margin = 0   # 2026/10/22 利益率の条件はやめて、利益2,000円以上かどうかだけで判断する（設定シートB13は使わない）
    min_profit = float(settings.get("min_profit") or 1500)
    max_add = int(float(settings.get("max_add") or 5))
    exclude = [w.strip() for w in re.split(r"[,、\n]", str(settings.get("exclude") or "")) if w.strip()]

    found, seen, checked = [], set(), 0
    load_market_cache()   # 前回までに調べたeBay相場（6時間以内）を使い回す
    t0 = time.time()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(locale="ja-JP", user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
        # 高速化：画像・動画・フォントは読み込まない（検索結果の判定に不要）
        page.route("**/*", lambda route: route.abort()
                   if route.request.resource_type in ("image", "media", "font") else route.continue_())
        waited = 0   # 在庫の見張りを待った時間（秒）
        order = [k for i, k in enumerate(keywords) if i % SHARDS == SHARD]   # 受け持ちの機種だけ（4本で分ける）
        random.Random(int(time.time() // 3600)).shuffle(order)   # 毎回ちがう順番で回る（時間切れでも、全部の機種を何回かに分けて探せる）
        for kw in order:
            if monitor_running() and waited < MAX_WAIT:
                print("在庫の見張りが動いているので、終わるまで待ちます")
                w0 = time.time()
                while monitor_running() and waited + (time.time() - w0) < MAX_WAIT:
                    time.sleep(20)   # 在庫の見張りを優先（同時にメルカリを見に行くと、見張りが判定できなくなることがある）
                waited += time.time() - w0
            if time.time() - t0 - waited > DISCOVER_BUDGET:   # 待った時間は探す時間に数えない
                print(f"時間切れ（{DISCOVER_BUDGET // 60}分）のため、残りの機種は次回に回します")
                break
            gi = game_info(kw)
            url = ("https://jp.mercari.com/search?status=on_sale&sort=created_time&order=desc"
                   f"&category_id={gi['mercari'] if gi else '860'}&keyword="
                   + ("" if gi and gi.get("exact") else requests.utils.quote(kw)) + MERCARI_FIXED)   # 本体カテゴリ・通常出品のみ（機種別カテゴリなら検索語なし）
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
                if any(w in it["title"] for w in exclude) or reserved_reason(it["title"]):
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
                p, rate = profit(it["price"], sale, settings, q)
                if rate >= min_margin and p >= max(min_profit, min_profit_for(0, settings, q)):
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
                if any(w in it["title"] for w in exclude) or reserved_reason(it["title"]):
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
                p, rate = profit(it["price"], sale, settings, q)
                if rate >= min_margin and p >= max(min_profit, min_profit_for(0, settings, q)):
                    found.append({"mercari_url": u, "title": it["title"], "cost": it["price"],
                                  "ebay_usd": sale, "ebay_count": n, "query": q,
                                  "profit": p, "margin": rate, "memo": "自動発見（Yahoo!フリマ）",
                                  "result": f"自動発見 {now()}"})
            time.sleep(random.uniform(4, 7))   # メルカリに止められないよう、検索ごとに少し待つ
        browser.close()
    save_market_cache()

    # よく売れる機種を優先し、同じ機種は（出品中＋候補で）3件までにする
    per_model = {}
    for x in data["rows"]:
        if x["status"] in ("", "active"):
            k = model_key(x.get("query"))
            per_model[k] = per_model.get(k, 0) + 1
    found.sort(key=lambda x: (SOLD90.get(x["query"], 0), x["profit"]), reverse=True)
    added = []
    for x in found:
        k = model_key(x["query"])
        if per_model.get(k, 0) >= cap_for(x["query"]):
            continue
        per_model[k] = per_model.get(k, 0) + 1
        added.append(x)
        if len(added) >= max_add:
            break
    if added:
        sheet("append", rows=added)
        best = added[0]
        notify(f"【仕入れ候補】{len(added)}件を追加しました。最高：{best['title'][:30]} 想定利益 {best['profit']:,}円（{best['margin']}%）")
    msg = (f"{now()} 候補さがし：{checked}件を計算、条件に合う{len(found)}件のうち{len(added)}件を追加"
           f"（{(time.time() - t0) / 60:.0f}分・相場は{_market_stats['hit']}件を使い回し、{_market_stats['miss']}件を新しく取得）")
    print(msg)
    sheet("log", message=msg)


# ---------- ラクマ（仕入れはやめた。以前の行を確認して止めるためだけに残す） ----------
RAKUMA_GENRES = {"669": "携帯電話本体", "668": "スマートフォン本体", "1386": "コンパクトデジタルカメラ",
                 "789": "テレビゲーム > 本体", "695": "ポータブルMDプレーヤー"}
RAKUMA_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36", "Accept-Language": "ja-JP"}


def check_rakuma(url):
    """ラクマの商品ページ → (status, info, detail)"""
    import html as _html
    try:
        r = requests.get(url, headers=RAKUMA_UA, timeout=30)
    except Exception:
        return "unknown", {}, {}
    if r.status_code == 404:
        return "deleted", {}, {}
    if r.status_code in (403, 429):
        return "blocked", {}, {}
    if r.status_code != 200:
        return "unknown", {}, {}
    h = r.text
    ld = {}
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', h, re.S):
        try:
            j = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(j, dict) and j.get("@type") == "Product":
            ld = j
            break
    offers = ld.get("offers") or {}
    if "SOLD OUT" in h:
        status = "sold"
    elif "購入に進む" in h or "InStock" in str(offers.get("availability", "")):
        status = "on_sale"
    else:
        status = "unknown"
    try:
        price = int(float(offers.get("price")))
    except (TypeError, ValueError):
        price = None
    imgs = []
    for u in re.findall(r"https://img\.fril\.jp/img/\d+/l/\d+\.jpg", h):
        if u not in imgs:
            imgs.append(u)
    g = re.search(r'category_id(?:&quot;|")\s*:\s*(?:&quot;|")(\d+)', h)
    genre = g.group(1) if g else ""
    info = {"title": _html.unescape(ld.get("name", "")).strip(), "price": price}
    detail = {"images": imgs[:12], "description": ld.get("description", ""),
              "condition": "新品" if "NewCondition" in str(offers.get("itemCondition", "")) else "",
              "category": RAKUMA_GENRES.get(genre, f"その他（ラクマ {genre}）")}
    return status, info, detail


def orphans():
    """eBayで出品中なのにシートにない出品を表示する（調査用・何も変更しない）"""
    data = sheet("list")
    ids = {r["ebay_item_id"]: r for r in data["rows"] if r["ebay_item_id"]}
    act = ebay_active_items(get_access_token())
    print(f"eBayで出品中 {len(act)}件／シートの出品番号 {len(ids)}件")
    for i in act:
        if i not in ids:
            print("シートにない出品:", i)
    for i, r in ids.items():
        if i not in act:
            print("eBayで出品中でない行:", i, r["status"], r["row"], (r.get("title") or "")[:30])
    for i in ("366746609929", "366746610851"):
        for r in data["rows"]:
            if i in str(r.get("result")) or i in str(r.get("ebay_item_id")):
                print("関係する行:", r["row"], r["status"], r["ebay_item_id"], r["mercari_url"], (r.get("result") or "")[:60])


def dupdebug(ids):
    """指定したeBay出品の仕入れ先と写真の差を表示する（調査用・何も変更しない）"""
    data = sheet("list")
    rows = [r for r in data["rows"] if r["ebay_item_id"] in ids]
    thumbs = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(locale="ja-JP", user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
        for r in rows:
            if is_yahoo(r["mercari_url"]):
                st, info, detail = check_yahoo(r["mercari_url"])
            else:
                st, info = check_mercari(page, r["mercari_url"])
                detail = page.evaluate(DETAIL_JS) if st == "on_sale" else {}
            imgs = detail.get("images", [])
            print(r["ebay_item_id"], r["status"], st, info.get("price"), r["mercari_url"])
            print("  写真:", imgs[:3])
            t = []
            for u in imgs[:4]:
                try:
                    t.append(dhash_url(u))
                except Exception as e:
                    print("  写真エラー:", u, str(e)[:80])
            thumbs.append((r["ebay_item_id"], t))
        browser.close()
    for i in range(len(thumbs)):
        for j in range(i + 1, len(thumbs)):
            for a_i, a in enumerate(thumbs[i][1]):
                for b_i, b in enumerate(thumbs[j][1]):
                    d = sum(abs(x - y) for x, y in zip(a, b)) / len(a)
                    print(f"  {thumbs[i][0]}[{a_i}] vs {thumbs[j][0]}[{b_i}]: 差 {d:.1f}")


def sku_for(model):
    """eBayのSKU＝型番（CPaSSのSKU設定と同じ文字にして、発送時の申告内容を自動で入れる）"""
    return re.sub(r"[^A-Z0-9-]", "", (model or "").upper())[:50]


def setsku():
    """出品中の全商品のSKUを型番に書き換える（1回だけ実行）"""
    data = sheet("list")
    rows = [r for r in data["rows"] if r["ebay_item_id"] and r["status"] in ("", "active") and sku_for(r.get("query"))]
    token = get_access_token()
    ok_n = fail_n = 0
    for r in rows:
        ack, errs, root = trading_call_root(
            "ReviseFixedPriceItem",
            f"<Item><ItemID>{r['ebay_item_id']}</ItemID><SKU>{sku_for(r.get('query'))}</SKU></Item>", token)
        if ack in ("Success", "Warning"):
            ok_n += 1
        else:
            fail_n += 1
            print("SKUの変更に失敗:", r["row"], " / ".join(errs)[:200])
    msg = f"{now()} SKUを型番に変更：{ok_n}件／失敗 {fail_n}件"
    print(msg)
    sheet("log", message=msg)


def fixdesc(end_bad=True):
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
                if bad and end_bad:
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
                 or ("にも出ているため停止" in (r.get("result") or ""))
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


def trim():
    """同じ機種が4件以上出品されている場合、仕入れが安い3件を残して残りをeBayで終了する（1回だけ実行）"""
    data = sheet("list")
    token = get_access_token()
    rows = [r for r in data["rows"] if r["ebay_item_id"] and r["status"] in ("", "active")]
    groups = {}
    for r in rows:
        groups.setdefault(model_key(r.get("query")), []).append(r)
    updates, ended, kept = [], 0, 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(locale="ja-JP", user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
        for k, rs in groups.items():
            if len(rs) <= MAX_PER_MODEL:
                kept += len(rs)
                continue
            priced = []
            for r in rs:
                try:
                    if is_yahoo(r["mercari_url"]):
                        st, info, _ = check_yahoo(r["mercari_url"])
                    else:
                        st, info = check_mercari(page, r["mercari_url"])
                    price = info.get("price") if st == "on_sale" and info else None
                except Exception:
                    price = None
                if price:
                    priced.append((price, r))
                else:
                    kept += 1   # 値段が確認できないものは触らない（売り切れなら通常の監視で止まる）
            priced.sort(key=lambda t: t[0])
            for i, (price, r) in enumerate(priced):
                if i < MAX_PER_MODEL:
                    kept += 1
                    continue
                ok, msg = stop_on_ebay(r["ebay_item_id"], token)
                if ok:
                    ended += 1
                    updates.append({"row": r["row"], "status": "stopped",
                                    "result": f"同じ機種が多いため終了（仕入れが安い{MAX_PER_MODEL}件を残しました） → {msg}"})
        browser.close()
    if updates:
        sheet("update", updates=updates)
    msg = f"{now()} 重複機種の整理：残した {kept}件／終了 {ended}件"
    print(msg)
    sheet("log", message=msg)


def exclude_japan():
    """配送ポリシーの発送先から日本を除外する（画面では地域単位でしか選べないため、APIで国単位に設定）"""
    settings = sheet("list").get("settings", {})
    name = str(settings.get("ship_profile") or "").strip()
    base = "https://api.ebay.com/sell/account/v1/fulfillment_policy"
    h = {"Authorization": f"Bearer {get_access_token()}", "Content-Type": "application/json",
         "Content-Language": "en-US", "Accept-Language": "en-US"}
    r = requests.get(base, params={"marketplace_id": "EBAY_US"}, headers=h, timeout=30)
    r.raise_for_status()
    pol = next((x for x in r.json().get("fulfillmentPolicies", []) if x.get("name") == name), None)
    if not pol:
        print(f"配送ポリシー「{name}」が見つかりません")
        return

    def summary(x):
        return {"exclude": [e.get("regionName") for e in (x.get("shipToLocations") or {}).get("regionExcluded", [])],
                "options": [(o.get("optionType"), [(sv.get("shippingServiceCode"), sv.get("freeShipping"),
                             [g.get("regionName") for g in (sv.get("shipToLocations") or {}).get("regionIncluded", [])])
                             for sv in o.get("shippingServices", [])]) for o in x.get("shippingOptions", [])],
                "handling": x.get("handlingTime")}
    print("変更前:", json.dumps(summary(pol), ensure_ascii=False))
    ex = pol.setdefault("shipToLocations", {}).setdefault("regionExcluded", [])
    if any(e.get("regionName") == "JP" for e in ex):
        print("日本はすでに除外されています")
        return
    ex.append({"regionName": "JP", "regionType": "COUNTRY"})
    pid = pol.pop("fulfillmentPolicyId")
    r2 = requests.put(f"{base}/{pid}", headers=h, json=pol, timeout=30)
    print("更新:", r2.status_code, r2.text[:600])
    r3 = requests.get(f"{base}/{pid}", headers=h, timeout=30)
    print("変更後:", json.dumps(summary(r3.json()), ensure_ascii=False))
    sheet("log", message=f"{now()} 配送ポリシーで日本を除外：{'成功' if r2.ok else '失敗'}")


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
        elif len(sys.argv) > 1 and sys.argv[1] == "orphans":
            orphans()
        elif len(sys.argv) > 1 and sys.argv[1] == "dupdebug":
            dupdebug(sys.argv[2:])
        elif len(sys.argv) > 1 and sys.argv[1] == "fixdesc":
            fixdesc()
        elif len(sys.argv) > 1 and sys.argv[1] == "redesc":
            fixdesc(end_bad=False)   # 説明文だけ作り直す（出品は終了しない）
        elif len(sys.argv) > 1 and sys.argv[1] == "end":
            tk = get_access_token()
            for iid in sys.argv[2:]:
                print("出品の終了:", iid, stop_on_ebay(iid, tk))
        elif len(sys.argv) > 1 and sys.argv[1] == "thanks":
            thanks(sys.argv[2:])
        elif len(sys.argv) > 1 and sys.argv[1] == "setsku":
            setsku()
        elif len(sys.argv) > 1 and sys.argv[1] == "excludejp":
            exclude_japan()
        elif len(sys.argv) > 1 and sys.argv[1] == "trim":
            trim()
        elif len(sys.argv) > 1 and sys.argv[1] == "endstopped":
            end_stopped()
        elif len(sys.argv) > 1 and sys.argv[1] == "restore":
            restore()
            fixdesc()
        else:
            main()
    except Exception as e:
        # 通信が一瞬つながらなかっただけのエラーは「失敗」にしない（失敗メールが届かないように）。次の回で自然にやり直される
        if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)) or type(e).__name__ == "TimeoutError":
            print("通信が一時的につながりませんでした。次の回でやり直します:", type(e).__name__)
            try:
                sheet("log", message=f"{now()} 通信エラー（一時的・次の回でやり直し）：{type(e).__name__}")
            except Exception:
                pass
            sys.exit(0)
        notify(f"【エラー】監視中に問題が起きました: {e}")
        print("エラーが起きました（詳細はスプレッドシートの「設定」シートを確認）:", type(e).__name__)
        try:
            sheet("log", message=f"{now()} エラー：{e}")
        except Exception:
            pass
        sys.exit(1)
