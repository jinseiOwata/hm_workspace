# -*- coding: utf-8 -*-
"""
LINE Messaging API の Webhook。

必要な環境変数:
  LINE_CHANNEL_SECRET        - Messaging APIのチャネルシークレット
  LINE_CHANNEL_ACCESS_TOKEN  - 長期アクセストークン

やっていること:
  1. 署名検証(X-Line-Signatureヘッダ)
  2. follow イベント: userId を保存(push送信の宛先に使う)
  3. message(text) イベント:
     - 当日のシグナル(line_selection.load_today_signals())と突き合わせ
     - 成功分は pending_orders/ 以下にJSONで書き出す(次段のRakuten RSS発注側が読む想定)
     - 結果(成功/エラー)をreplyメッセージとしてユーザーに返す

本番運用時の注意:
  - LINEはWebhookに数秒以内の応答を要求するため、時間のかかる処理(実際の発注実行など)は
    ここでは行わず、キューに書き出すだけに留めている
  - 実際の発注実行(楽天RSS/VBA側)は別プロセス(Excelマクロ等)がpending_orders/を
    定期的にポーリングする想定(次のフェーズで実装)
"""

import hashlib
import hmac
import base64
import json
import os
import time
from pathlib import Path
from typing import List

import requests
from flask import Flask, request, abort

from line_selection import load_today_signals, parse_user_reply

app = Flask(__name__)

CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")

# このファイル自身の場所を基準にする(朝のpushスクリプトと作業ディレクトリが
# 異なっていても、必ず同じsignals_store/pending_ordersを見るようにするため)
BASE_DIR = Path(__file__).resolve().parent
USER_ID_FILE = BASE_DIR / "line_user_id.txt"
PENDING_ORDERS_DIR = BASE_DIR / "pending_orders"
PENDING_ORDERS_DIR.mkdir(exist_ok=True)

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


# ---- 署名検証 ---------------------------------------------------------

def verify_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    if not signature or not channel_secret:
        return False
    mac = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature)


# ---- LINE API呼び出し ---------------------------------------------------

def reply_message(reply_token: str, text: str) -> None:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ACCESS_TOKEN}",
    }
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text[:4900]}],  # LINEの文字数上限対策
    }
    resp = requests.post(LINE_REPLY_URL, headers=headers, json=payload, timeout=5)
    if resp.status_code != 200:
        print(f"[reply_message] failed: {resp.status_code} {resp.text}")


def push_message(user_id: str, text: str) -> None:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ACCESS_TOKEN}",
    }
    payload = {
        "to": user_id,
        "messages": [{"type": "text", "text": text[:4900]}],
    }
    resp = requests.post(LINE_PUSH_URL, headers=headers, json=payload, timeout=5)
    if resp.status_code != 200:
        print(f"[push_message] failed: {resp.status_code} {resp.text}")


def save_user_id(user_id: str) -> None:
    USER_ID_FILE.write_text(user_id, encoding="utf-8")


def load_user_id() -> str:
    if USER_ID_FILE.exists():
        return USER_ID_FILE.read_text(encoding="utf-8").strip()
    return ""


# ---- 発注待ちキュー -----------------------------------------------------

def enqueue_orders(orders: List[dict]) -> Path:
    """確定した発注データをJSONで書き出す(Rakuten RSS側がポーリングする想定)"""
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = PENDING_ORDERS_DIR / f"orders_{ts}.json"
    path.write_text(json.dumps(orders, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def build_result_text(results: List[dict]) -> str:
    ok_lines, err_lines = [], []
    for r in results:
        if "error" in r:
            err_lines.append(f"× {r['raw']} → {r['error']}")
        else:
            side = "買い" if r["side"] == "buy" else "売り"
            warn = f" ({r['unit_warning']})" if r.get("unit_warning") else ""
            ok_lines.append(f"○ {r['code']} {side} {r['quantity']}株{warn}")

    lines = []
    if ok_lines:
        lines.append("【受付】")
        lines.extend(ok_lines)
    if err_lines:
        lines.append("【エラー】")
        lines.extend(err_lines)
    if not lines:
        lines.append("有効な指示が見つかりませんでした。例: '1 100'")
    return "\n".join(lines)


# ---- Webhook本体 -------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()  # 署名検証は必ず生のbytesに対して行う

    if not verify_signature(body, signature, CHANNEL_SECRET):
        abort(400, "invalid signature")

    data = json.loads(body.decode("utf-8"))

    for event in data.get("events", []):
        event_type = event.get("type")

        if event_type == "follow":
            user_id = event.get("source", {}).get("userId")
            if user_id:
                save_user_id(user_id)

        elif event_type == "message" and event.get("message", {}).get("type") == "text":
            reply_token = event.get("replyToken")
            text = event["message"]["text"]

            today_signals = load_today_signals()
            if not today_signals:
                reply_message(reply_token, "本日のシグナルがまだ登録されていません。")
                continue

            results = parse_user_reply(text, today_signals)
            ok_orders = [r for r in results if "error" not in r]

            if ok_orders:
                path = enqueue_orders(ok_orders)
                print(f"[webhook] enqueued {len(ok_orders)} orders -> {path}")

            reply_message(reply_token, build_result_text(results))

    return "OK", 200


if __name__ == "__main__":
    app.run(port=5000)
