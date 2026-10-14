"""Golden Time Retriever (cloud) - メルカリ売り切れ検知 → eBay在庫0

GitHub Actions から30分ごとに実行される。
商品リストは Google スプレッドシート（Apps Script 経由）から読み書きする。
秘密情報はすべて GitHub の Secrets から環境変数として受け取る。
"""
import base64
import json
import os
import random
import sys
import time
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
        return "unknown"
    if resp is not None and resp.status == 404:
        return "deleted"
    if resp is not None and resp.status in (403, 429):
        return "blocked"
    try:
        page.wait_for_function(
            """() => { const t = document.body ? document.body.innerText : '';
                return /購入手続きへ|売り切れ|削除されています|見つかりません|公開停止/.test(t); }""",
            timeout=25000,
        )
    except Exception:
        return "unknown"
    page.wait_for_timeout(1500)
    return page.evaluate(JUDGE_JS)


LABEL = {"on_sale": "販売中", "sold": "売り切れ", "deleted": "削除・非公開",
         "unknown": "判定できず", "blocked": "メルカリにアクセスを拒否された"}


def main():
    rows = sheet("list")["rows"]
    targets = [r for r in rows if r["status"] in ("", "active")]
    print(f"{now()} チェック対象: {len(targets)}件")
    if not targets:
        sheet("log", message=f"{now()} 正常終了：監視中の商品なし")
        return

    # eBayの認証を最初に確認しておく（売り切れを見つけてから失敗するのを防ぐ）
    token = get_access_token()
    updates, blocked, counts, stopped = [], 0, {}, 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            locale="ja-JP",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        )
        for r in targets:
            status = check_mercari(page, r["mercari_url"])
            counts[status] = counts.get(status, 0) + 1
            upd = {"row": r["row"], "last_checked": now(), "result": LABEL[status]}
            if status in ("sold", "deleted"):
                ok, msg = stop_on_ebay(r["ebay_item_id"], token)
                upd["result"] = f"{LABEL[status]} → {msg}"
                if ok:
                    upd["status"] = "stopped"
                    stopped += 1
                    notify(f"【停止】{r.get('memo','')} メルカリ{LABEL[status]} → {msg}（eBay {r['ebay_item_id']}）")
                else:
                    notify(f"【要対応】{r.get('memo','')} メルカリ{LABEL[status]}ですが{msg}（eBay {r['ebay_item_id']}）")
            elif status == "blocked":
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
    print(f"{now()} 完了：{summary}／eBay停止 {stopped}件")
    sheet("log", message=f"{now()} 正常終了：{summary}／eBay停止 {stopped}件")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        notify(f"【エラー】監視中に問題が起きました: {e}")
        print("エラーが起きました（詳細はスプレッドシートの「設定」シートを確認）:", type(e).__name__)
        try:
            sheet("log", message=f"{now()} エラー：{e}")
        except Exception:
            pass
        sys.exit(1)
