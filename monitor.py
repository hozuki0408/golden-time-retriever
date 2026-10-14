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
import xml.etree.ElementTree as ET
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
    r = requests.post(
        SHEET_API_URL,
        data=json.dumps({"key": SHEET_API_KEY, "action": action, **payload}),
        headers={"Content-Type": "text/plain"},
        timeout=60,
    )
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"スプレッドシートの応答が読めません ({r.status_code}): {r.text[:200]}")
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
    page.wait_for_timeout(1500)
    status = page.evaluate(JUDGE_JS)
    info = page.evaluate(INFO_JS) if status in ("on_sale", "sold") else {}
    return status, info


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


def guess_query(title):
    """商品名から型番らしき文字列を拾う（例：KY-42C、SH-02L、SO903i）"""
    t = unicodedata.normalize("NFKC", title or "")
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


def profit(cost_jpy, sale_usd, settings):
    fx = usd_jpy(settings)
    fee = float(settings.get("fee_pct") or 15) + float(settings.get("other_pct") or 2)
    ship = float(settings.get("ship_jpy") or 3000)
    revenue = sale_usd * fx
    p = revenue * (1 - fee / 100) - ship - cost_jpy
    return round(p), (round(p / revenue * 100, 1) if revenue else 0)


LABEL = {"on_sale": "販売中", "sold": "売り切れ", "deleted": "削除・非公開",
         "unknown": "判定できず", "blocked": "メルカリにアクセスを拒否された"}


def main():
    data = sheet("list")
    rows, settings = data["rows"], data.get("settings", {})
    targets = [r for r in rows if r["status"] in ("", "active")]
    watch = [r for r in targets if r["ebay_item_id"]]
    research = [r for r in targets if not r["ebay_item_id"]]
    print(f"{now()} 監視 {len(watch)}件／仕入れ候補 {len(research)}件")
    if not targets:
        sheet("log", message=f"{now()} 正常終了：対象の商品なし")
        return

    # eBayの認証を最初に確認しておく（売り切れを見つけてから失敗するのを防ぐ）
    token = get_access_token() if watch else None
    updates, blocked, counts, stopped, researched = [], 0, {}, 0, 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            locale="ja-JP",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        )
        for r in targets:
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
            else:
                # ---- 仕入れ候補：商品名・価格・相場・利益を埋める ----
                if status in ("sold", "deleted"):
                    upd["result"] = f"{LABEL[status]}（候補から外しました）"
                    upd["status"] = "stopped"
                elif status == "on_sale" and info:
                    try:
                        upd.update(research_row(r, info, settings))
                        researched += 1
                    except Exception as e:
                        upd["result"] = f"販売中（相場の取得に失敗：{str(e)[:60]}）"

            if status == "blocked":
                blocked += 1
            elif status == "unknown":
                notify(f"【確認して】メルカリの状態を判定できませんでした：{r['mercari_url']}")
            updates.append(upd)
            time.sleep(random.uniform(6, 12))
        browser.close()

    sheet("update", updates=updates)
    if blocked:
        notify(f"【注意】メルカリにアクセスを拒否されました（{blocked}件）。クラウドからのアクセスが制限されている可能性があります。")
    # 記録には件数だけを出す（URLや出品番号は出さない）
    summary = "、".join(f"{LABEL[k]} {v}件" for k, v in counts.items())
    msg = f"{now()} 正常終了：{summary}／eBay停止 {stopped}件／利益計算 {researched}件"
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


# ---------- 自動仕入れ候補さがし ----------
SEARCH_JS = """async () => {
    for (let i = 0; i < 5; i++) { window.scrollBy(0, 2000); await new Promise(r => setTimeout(r, 1200)); }
    return [...document.querySelectorAll('a[href*="/item/m"], a[href*="/shops/product/"]')]
        .filter(a => !a.getAttribute('href').includes('ad_id'))   // 広告枠は除く
        .map(a => {
            const img = a.querySelector('img');
            const m = a.innerText.match(/([\\d,]+)/);
            return { url: 'https://jp.mercari.com' + a.getAttribute('href').split('?')[0],
                     title: img ? img.alt.replace(/のサムネイル$/, '') : '',
                     price: m ? parseInt(m[1].replace(/,/g, ''), 10) : null };
        });
}"""


def discover():
    data = sheet("list")
    settings = data.get("settings", {})
    known = {r["mercari_url"].split("?")[0] for r in data["rows"]}
    keywords = [k.strip() for k in re.split(r"[,、\n]", str(settings.get("keywords") or "ガラケー")) if k.strip()]
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
                if not sale or n < 3:          # 比較できる出品が少ないものは信用しない
                    continue
                p, rate = profit(it["price"], sale, settings)
                if rate >= min_margin and p >= min_profit:
                    found.append({"mercari_url": u, "title": it["title"], "cost": it["price"],
                                  "ebay_usd": sale, "ebay_count": n, "query": q,
                                  "profit": p, "margin": rate, "memo": "自動発見",
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
