# -*- coding: utf-8 -*-
"""
LINE上で「当日の分析シグナル一覧」をユーザーに提示し、
返信(番号 または 銘柄コード + 株数)から発注内容を確定するモジュール。

想定フロー:
1. 朝、line_analysis_parser.parse_message() の出力(to_order_dict()のリスト)を
   save_today_signals() でファイルに保存(1〜Nの通し番号を付与)
2. format_signals_message() で一覧テキストを作成し、
   LINE Messaging API の push message でユーザーに送信
3. ユーザーが例えば
     1 100
     7201 300
   のように返信(1行1銘柄で"番号 株数" or "銘柄コード 株数")
4. LINE Webhookが受信したテキストを parse_user_reply() に渡す
   → 当日のシグナルと突き合わせて、発注用の辞書リストを返す
   (このあと楽天RSSの発注クロに渡す想定)

保存先はさしあたりローカルにJSONファイル(1日1ファイル)。
将来的にDBに置き換える場合も、save/load関数の中身だけ差し替えればよい設計。
"""

import json
import re
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

# スクリプトの起動元(作業ディレクトリ)に依存しないよう、このファイル自身の場所を基準にする。
# 朝のpushスクリプトとWebhookサーバーが別々の作業ディレクトリから起動されても、
# 必ず同じフォルダを見るようにするための対策。
BASE_DIR = Path(__file__).resolve().parent
SIGNALS_DIR = BASE_DIR / "signals_store"
SIGNALS_DIR.mkdir(exist_ok=True)

UNIT_SHARE = 100  # 東証の一般的な単元株数(銘柄によって異なる点に注意)

_WEEKDAY_KANJI = ["月", "火", "水", "木", "金", "土", "日"]


def _format_date_with_weekday(d: date) -> str:
    """'7/10（金）'のような表示用の日付文字列を作る"""
    return f"{d.month}/{d.day}（{_WEEKDAY_KANJI[d.weekday()]}）"


def _today_path(d: Optional[date] = None) -> Path:
    d = d or date.today()
    return SIGNALS_DIR / f"{d.isoformat()}.json"


def save_today_signals(order_dicts: List[dict], d: Optional[date] = None) -> List[dict]:
    """to_order_dict()の出力リストに通し番号(index)を付けて保存する。
    保存後のリスト(index付き)を返す"""
    numbered = []
    for i, od in enumerate(order_dicts, start=1):
        od = dict(od)
        od["index"] = i
        numbered.append(od)
    _today_path(d).write_text(
        json.dumps(numbered, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return numbered


def load_today_signals(d: Optional[date] = None) -> List[dict]:
    p = _today_path(d)
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def format_signals_message(numbered_signals: List[dict]) -> str:
    """LINE push message用の一覧テキストを作成(1銘柄ごとに改行で見やすく整形)"""
    date_label = _format_date_with_weekday(date.today())
    blocks = []
    for od in numbered_signals:
        side_label = "買い" if od.get("side") == "buy" else "売り"
        entry = f"{od['entry_price']:.0f}円" if od.get("entry_price") is not None else "成行"
        tp = f"{od['take_profit']:.0f}円" if od.get("take_profit") is not None else "-"
        sl = f"{od['stop_loss']:.0f}円" if od.get("stop_loss") is not None else "-"
        warn = "⚠要確認 " if od.get("confidence") != "ok" else ""
        name = f" {od['name']}" if od.get("name") else ""

        # Gemini突合結果
        # True=両AI一致 / False=Claude単独(不一致) / "gemini_only"=Gemini単独補完 / None=突合できず不明
        gemini_agreed = od.get("gemini_agreed")
        if gemini_agreed is True:
            combined = od.get("combined_confidence")
            conf_text = f"確信度{combined:.2f}" if combined is not None else "確信度不明"
            gemini_label = f" 🤝両AI一致({conf_text})"
        elif gemini_agreed == "gemini_only":
            gemini_label = " (Gemini単独)"
        elif gemini_agreed is False:
            gemini_label = " (Claude単独)"
        else:
            gemini_label = " (Gemini突合失敗、Claude単独)"

        block_lines = [
            f"{od['index']}. {warn}{od['code']}{name} {side_label}{gemini_label}",
            f"   エントリー: {entry}",
            f"   利確: {tp}",
            f"   損切: {sl}",
            f"   [{od.get('order_style', '')}]",
        ]
        # 期待値スコアが付与されている場合(通知フィルタリング後)は判断材料として併記する。
        # 両AI一致銘柄はClaude・Gemini双方のentry/TP/SLから計算した値をそれぞれ表示する。
        # (③Gemini単独補完はClaude側の値を持たないため、Geminiの値だと明示する)
        claude_score = od.get("expected_value_score")
        gemini_score = od.get("gemini_expected_value_score")
        if claude_score is not None and gemini_score is not None:
            block_lines.append(
                f"   期待値スコア: Claude {claude_score:.2f} / Gemini {gemini_score:.2f}"
            )
        elif claude_score is not None:
            block_lines.append(f"   期待値スコア: {claude_score:.2f}")
        elif gemini_score is not None:
            block_lines.append(f"   期待値スコア: Gemini {gemini_score:.2f}")
        if od.get("reason"):
            block_lines.append(f"   理由: {od['reason']}")
        blocks.append("\n".join(block_lines))

    return (
        f"■本日のシグナル■ {date_label}\n\n"
        + "\n\n".join(blocks)
        + f"\n\n{date_label}"
    )


# "1 100" / "7201 300" / "1  100株" などを許容
REPLY_LINE_PATTERN = re.compile(r"^(\S+?)\s+(\d{1,6})\s*株?\s*$")


def parse_user_reply(reply_text: str, today_signals: List[dict]) -> List[dict]:
    """
    ユーザーの返信(複数行可)を当日のシグナルと突き合わせる。
    発注確定データのリストを返す。各要素は以下のどちらか:
      - 成功時: シグナルの全項目 + "quantity"
      - 失敗時: {"raw": 元の行, "error": 理由}
    番号(index)・銘柄コードのどちらでも指定可能。
    """
    by_index: Dict[int, dict] = {}
    by_code: Dict[str, dict] = {}
    for s in today_signals:
        if s.get("index") is not None:
            by_index[int(s["index"])] = s
        if s.get("code"):
            by_code[str(s["code"])] = s

    results = []
    for raw_line in reply_text.strip().splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        m = REPLY_LINE_PATTERN.match(raw_line)
        if not m:
            results.append({"raw": raw_line, "error": "書式を認識できません(例: '1 100' または '7201 100')"})
            continue

        key, qty_str = m.group(1), m.group(2)
        qty = int(qty_str)

        signal = None
        if key.isdigit():
            signal = by_index.get(int(key)) or by_code.get(key)
        else:
            signal = by_code.get(key)

        if signal is None:
            results.append({"raw": raw_line, "error": f"該当するシグナルが見つかりません('{key}')"})
            continue
        if qty <= 0:
            results.append({"raw": raw_line, "error": "株数は1以上を指定してください"})
            continue

        order = dict(signal)
        order["quantity"] = qty
        if qty % UNIT_SHARE != 0:
            order["unit_warning"] = f"{UNIT_SHARE}株単位ではありません(単元未満の可能性)"
        results.append(order)

    return results


# ---- 動作確認用サンプル ---------------------------------------------
if __name__ == "__main__":
    from line_analysis_parser import parse_message, to_order_dict

    sample = """
    ```json
    {
      "signals": [
        {"code": "6367", "name": "DAIKIN INDUSTRIES", "side": "sell", "entry_low": 25600, "entry_high": 25700, "take_profit": 24900, "stop_loss": 26200},
        {"code": "7201", "name": "NISSAN MOTOR", "side": "buy", "entry_low": 310, "entry_high": 315, "take_profit": 325, "stop_loss": 298},
        {"code": "9501", "name": "TOKYO ELECTRIC POWER", "side": "buy", "entry_low": 460, "entry_high": 468, "take_profit": 485, "stop_loss": 450}
      ]
    }
    ```
    """

    parsed = parse_message(sample)
    order_dicts = [to_order_dict(p) for p in parsed]

    numbered = save_today_signals(order_dicts)
    print("=== LINEに送るメッセージ ===")
    print(format_signals_message(numbered))

    print("\n=== ユーザー返信を解析 ===")
    reply = "1 100\n7201 300\n99 50\nよくわからない文字列"
    for r in parse_user_reply(reply, load_today_signals()):
        print(r)
