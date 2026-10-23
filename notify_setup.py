"""eBayで売れた瞬間に、管理表（Apps Script）へ直接知らせが届くように eBay に登録する。

使い方（GitHub の Actions →「eBay即時通知の設定」→ Run workflow）
  setup : 登録する（最初に1回だけ。何度実行しても大丈夫）
  test  : 売れた知らせのテストを管理表に送る（【テスト】付きのメールが届けば成功）
  usage : 最近 eBay から知らせを送った回数・失敗の数を見る
公開リポジトリなので、受け口のURLや合言葉は記録に出さない。
"""
import base64
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

import requests

APP_ID = os.environ["EBAY_APP_ID"]
CERT_ID = os.environ["EBAY_CERT_ID"]
REFRESH_TOKEN = os.environ["EBAY_REFRESH_TOKEN"]
SHEET_API_URL = os.environ["SHEET_API_URL"]
SHEET_API_KEY = os.environ["SHEET_API_KEY"]
MODE = (sys.argv[1] if len(sys.argv) > 1 else "setup").strip()
NS = {"e": "urn:ebay:apis:eBLBaseComponents"}
EVENTS = ["FixedPriceTransaction", "AuctionCheckoutComplete"]   # 即決で売れた・オークションの支払い完了


def token():
    auth = "Basic " + base64.b64encode(f"{APP_ID}:{CERT_ID}".encode()).decode()
    r = requests.post("https://api.ebay.com/identity/v1/oauth2/token",
                      headers={"Authorization": auth, "Content-Type": "application/x-www-form-urlencoded"},
                      data={"grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN,
                            "scope": "https://api.ebay.com/oauth/api_scope https://api.ebay.com/oauth/api_scope/sell.inventory"},
                      timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def trading(call, body, tok):
    xml = f'<?xml version="1.0" encoding="utf-8"?><{call}Request xmlns="urn:ebay:apis:eBLBaseComponents">{body}</{call}Request>'
    r = requests.post("https://api.ebay.com/ws/api.dll", data=xml.encode(), timeout=30, headers={
        "X-EBAY-API-CALL-NAME": call, "X-EBAY-API-SITEID": "0", "X-EBAY-API-COMPATIBILITY-LEVEL": "1209",
        "X-EBAY-API-IAF-TOKEN": tok, "Content-Type": "text/xml"})
    root = ET.fromstring(r.content)
    errs = [e.findtext("e:LongMessage", default="", namespaces=NS) for e in root.findall("e:Errors", NS)]
    return root, root.findtext("e:Ack", default="", namespaces=NS), errs


def sheet(action, **kw):
    r = requests.post(SHEET_API_URL, data=json.dumps({"key": SHEET_API_KEY, "action": action, **kw}),
                      headers={"Content-Type": "text/plain"}, timeout=60)
    return r.json()


def receiver_url():
    t = sheet("notify_token")["token"]
    return SHEET_API_URL + ("&" if "?" in SHEET_API_URL else "?") + "t=" + t


def show_prefs(tok):
    root, ack, errs = trading("GetNotificationPreferences", "<PreferenceLevel>Application</PreferenceLevel>", tok)
    url = root.findtext(".//e:ApplicationURL", default="", namespaces=NS)
    print("アプリの通知：", root.findtext(".//e:ApplicationEnable", default="?", namespaces=NS),
          "／受け口が管理表になっている：", "はい" if url == receiver_url() else "いいえ", ack, errs)
    root, ack, errs = trading("GetNotificationPreferences", "<PreferenceLevel>User</PreferenceLevel>", tok)
    for n in root.findall(".//e:NotificationEnable", NS):
        ev = n.findtext("e:EventType", default="", namespaces=NS)
        if ev in EVENTS:
            print(f"  {ev}: {n.findtext('e:EventEnable', default='', namespaces=NS)}")


tok = token()
if MODE == "setup":
    body = ("<ApplicationDeliveryPreferences>"
            f"<ApplicationURL>{escape(receiver_url())}</ApplicationURL>"
            "<ApplicationEnable>Enable</ApplicationEnable><DeviceType>Platform</DeviceType>"
            "</ApplicationDeliveryPreferences><UserDeliveryPreferenceArray>"
            + "".join(f"<NotificationEnable><EventType>{e}</EventType><EventEnable>Enable</EventEnable></NotificationEnable>" for e in EVENTS)
            + "</UserDeliveryPreferenceArray>")
    _, ack, errs = trading("SetNotificationPreferences", body, tok)
    print("登録：", ack, errs)
    show_prefs(tok)

elif MODE == "test":
    rows = [r for r in sheet("list")["rows"] if r.get("ebay_item_id") and r.get("status") == "active"]
    item = rows[0]["ebay_item_id"] if rows else "000000000000"
    soap = ('<?xml version="1.0" encoding="UTF-8"?><soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
            '<soapenv:Body><GetItemTransactionsResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
            '<Ack>Success</Ack><NotificationEventName>FixedPriceTransaction</NotificationEventName>'
            f'<Item><ItemID>{item}</ItemID><Title>TEST</Title></Item>'
            '<TransactionArray><Transaction><Buyer><UserID>test_buyer</UserID></Buyer>'
            '<TransactionID>0</TransactionID><TransactionPrice currencyID="USD">99.99</TransactionPrice></Transaction></TransactionArray>'
            '</GetItemTransactionsResponse></soapenv:Body></soapenv:Envelope>')
    r = requests.post(receiver_url() + "&test=1", data=soap.encode(), timeout=60,
                      headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '"http://developer.ebay.com/notification/FixedPriceTransaction"'})
    print("テスト送信：", r.status_code, r.text[:100])
    print("管理表が最後に受け取った知らせ：", sheet("notify_last").get("last"))

elif MODE == "usage":
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=1)
    root, ack, errs = trading("GetNotificationsUsage",
                              f"<StartTime>{start:%Y-%m-%dT%H:%M:%S.000Z}</StartTime><EndTime>{end:%Y-%m-%dT%H:%M:%S.000Z}</EndTime>", tok)
    s = root.find(".//e:NotificationStatistics", NS)
    if s is not None:
        for k in ["DeliveredCount", "QueuedNewCount", "QueuedPendingCount", "ExpiredCount", "ErrorCount"]:
            print(k, s.findtext(f"e:{k}", default="", namespaces=NS))
    print(ack, errs)
    print("管理表が最後に受け取った知らせ：", sheet("notify_last").get("last"))
    show_prefs(tok)
