# -*- coding: utf-8 -*-
"""
LINEで届く株価分析テキストから、IFD/IFO発注に必要な情報を抽出するパーサー。

対応を想定している表記例:
- 銘柄コード: 7203 / 7203.T / (7203)
- 売買方向: 買い/買/ロング/LONG/新規買い → 売り/売/ショート/SHORT
- エントリー: エントリー/IN/新規/指値/成行 + 価格
- 利確: 利確/利益確定/TP/目標株価/利食い + 価格
- 損切: 損切/ストップ/SL/逆指値/損切り + 価格
- 価格表記: 2,850円 / 2850円 / 2850

1メッセージに複数銘柄が含まれる場合は改行や「①②③」などの区切りで分割して処理します。

完全のパターンコード・方向・エントリー価格のいずれかが取れない行は
「要確認(unparsed)」として結果に含め、自動発注側で除外・通知できるようにします。
(完全自動運用でも解析に自信が持てない行だけは弾く、という安全弁です)
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional, List

logger = logging.getLogger(__name__)


@dataclass
class ParsedOrder:
    raw_text: str
    code: Optional[str] = None
    name: Optional[str] = None
    side: Optional[str] = None          # "buy" or "sell"
    entry_type: Optional[str] = None    # "limit" (指値) or "market" (成行)
    entry_price: Optional[float] = None
    take_profit: Optional[float] = None
    take_profit_2: Optional[float] = None
    stop_loss: Optional[float] = None
    quantity: Optional[int] = None
    ai_confidence: Optional[float] = None  # Claude自身の確信度(0.0〜1.0)。期待値スコア算出に使う
    reason: Optional[str] = None        # この銘柄を選んだ理由の一文要約
    confidence: str = "ok"              # "ok" or "needs_review"
    issues: List[str] = field(default_factory=list)


# ---- キーワード辞書 -------------------------------------------------

BUY_WORDS = ["買い", "買え", "買", "ロング", "LONG", "新規買い", "IN買い"]
SELL_WORDS = ["売り", "空売り", "売", "ショート", "SHORT", "新規売り", "IN売り"]

ENTRY_WORDS = ["エントリー", "IN", "新規", "指値", "成行", "仕込み"]
TP_WORDS = ["利確", "利益確定", "利食い", "目標株価", "TP", "ターゲット"]
SL_WORDS = ["損切", "損切り", "ストップ", "逆指値", "SL"]

# 東証の証券コードは4桁数字が基本だが、2024年以降は数字3桁+英字1桁の新形式
# (例: 543A, 285A)も発行されているため、どちらも拾えるようにする。
CODE_PATTERN = re.compile(r"(?<![0-9A-Za-z])(\d{4}|\d{3}[A-Z])(?:\.T)?(?![0-9A-Za-z])")
PRICE_PATTERN = re.compile(r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*円?")
QTY_PATTERN = re.compile(r"(\d+)\s*株")

# メッセージ内で複数銘柄を区切るときの目印。
# 「・」は文中の列挙(例: "移動平均線・RSI")でも普通に使われるため、
# 銘柄の区切りとしては使わない(丸数字と改行のみを区切りとする)。
SPLIT_PATTERN = re.compile(r"\n+|(?:①|②|③|④|⑤)")


def _find_price_near(text: str, keywords: List[str]) -> Optional[float]:
    """キーワードの直後(全角30文字以内)にある価格を探す"""
    for kw in keywords:
        idx = text.find(kw)
        if idx == -1:
            continue
        window = text[idx: idx + 30]
        m = PRICE_PATTERN.search(window[len(kw):])
        if m:
            return float(m.group(1).replace(",", ""))
    return None


def _find_side(text: str) -> Optional[str]:
    for w in BUY_WORDS:
        if w in text:
            return "buy"
    for w in SELL_WORDS:
        if w in text:
            return "sell"
    return None


def _find_entry_type(text: str) -> Optional[str]:
    if "成行" in text:
        return "market"
    if "指値" in text or "エントリー" in text or "IN" in text:
        return "limit"
    return None


def parse_line(segment: str) -> Optional[ParsedOrder]:
    segment = segment.strip()
    if not segment:
        return None

    order = ParsedOrder(raw_text=segment)

    code_match = CODE_PATTERN.search(segment)
    if code_match:
        order.code = code_match.group(1)
    else:
        order.issues.append("証券コードが見つかりません")

    order.side = _find_side(segment)
    if order.side is None:
        order.issues.append("売買方向(買い/売り)が特定できません")

    order.entry_type = _find_entry_type(segment)

    order.entry_price = _find_price_near(segment, ENTRY_WORDS)
    if order.entry_price is None and order.entry_type != "market":
        # エントリー語が無くても、コードの直後にある価格をエントリーとみなす救済処置
        # (成行注文の場合は価格指定が無いのが正常なので救済しない)
        if code_match:
            tail = segment[code_match.end():code_match.end() + 20]
            m = PRICE_PATTERN.search(tail)
            if m:
                order.entry_price = float(m.group(1).replace(",", ""))
        if order.entry_price is None:
            order.issues.append("エントリー価格が特定できません")

    order.take_profit = _find_price_near(segment, TP_WORDS)
    order.stop_loss = _find_price_near(segment, SL_WORDS)

    qty_match = QTY_PATTERN.search(segment)
    if qty_match:
        order.quantity = int(qty_match.group(1))

    entry_ok = order.entry_price is not None or order.entry_type == "market"
    if order.code is None or order.side is None or not entry_ok:
        order.confidence = "needs_review"

    return order


# ---- 構造化JSON(推奨経路) -----------------------------------------
# Claudeに分析を生成させる際、本文の最後に ```json ...``` ブロックを
# 付けてもらうことで、以下のregexベースの解析を経由せず高精度に処理できる。
# (analysis_prompt_addendum.md 参照)

JSON_BLOCK_PATTERN = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json_signals(text: str) -> Optional[list]:
    # analysis_prompt_addendum.mdの指示は「本文の一番最後に1つだけ」JSONブロックを
    # 付けることなので、万一本文中に別の```json```ブロックが混ざっていても、
    # 最後に出てくるものを使う(search()の先頭一致だと誤って前のブロックを拾う恐れがある)。
    matches = list(JSON_BLOCK_PATTERN.finditer(text))
    if not matches:
        return None
    m = matches[-1]
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        logger.warning("JSONブロックの解析に失敗しました(自然文解析にフォールバック): %s", exc)
        return None
    return data.get("signals")


def _from_structured(s: dict) -> ParsedOrder:
    order = ParsedOrder(raw_text=json.dumps(s, ensure_ascii=False))
    order.code = str(s.get("code")) if s.get("code") else None
    order.name = s.get("name")
    order.side = s.get("side")
    order.entry_type = s.get("entry_type", "limit")

    entry_low = s.get("entry_low")
    entry_high = s.get("entry_high")
    if entry_low is not None and entry_high is not None:
        # レンジの中央値をデフォルトのエントリー価格とする(発注モジュール側で調整可能)
        order.entry_price = (float(entry_low) + float(entry_high)) / 2
    elif s.get("entry_price") is not None:
        order.entry_price = float(s["entry_price"])
    elif entry_low is not None or entry_high is not None:
        order.entry_price = float(entry_low if entry_low is not None else entry_high)

    if s.get("take_profit") is not None:
        order.take_profit = float(s["take_profit"])
    if s.get("take_profit_2") is not None:
        order.take_profit_2 = float(s["take_profit_2"])
    if s.get("stop_loss") is not None:
        order.stop_loss = float(s["stop_loss"])
    if s.get("ai_confidence") is not None:
        order.ai_confidence = float(s["ai_confidence"])
    order.reason = s.get("reason")
    if s.get("quantity") is not None:
        order.quantity = int(s["quantity"])

    if not order.code:
        order.issues.append("証券コードが見つかりません(JSON)")
    if not order.side:
        order.issues.append("売買方向が見つかりません(JSON)")
    if order.entry_price is None and order.entry_type != "market":
        order.issues.append("エントリー価格が見つかりません(JSON)")

    if order.issues:
        order.confidence = "needs_review"

    return order


def parse_message(text: str) -> List[ParsedOrder]:
    """1件のLINEメッセージを解析してParsedOrderのリストを返す。
    末尾に```json```ブロックがあれば、それを最優先で使う(高精度)。
    無ければ従来のregexベースの自然文解析にフォールバックする。
    """
    signals = extract_json_signals(text)
    if signals is not None:
        logger.info("JSONブロックから%d件のシグナルを抽出しました(構造化経路)", len(signals))
        return [_from_structured(s) for s in signals]

    logger.warning(
        "JSONブロックが見つからない/解析失敗のため、自然文の正規表現解析にフォールバックします"
        "(精度が大きく落ちるため、本来はここに来ないことを想定している)"
    )
    segments = [s for s in SPLIT_PATTERN.split(text) if s and s.strip()]
    results = []
    for seg in segments:
        parsed = parse_line(seg)
        if parsed:
            results.append(parsed)
    return results


def to_order_dict(p: ParsedOrder) -> dict:
    """発注モジュール(RSS/VBA側)に渡すための形式に変換"""
    return {
        "code": p.code,
        "name": p.name,
        "side": p.side,
        "entry_type": p.entry_type or "limit",
        "entry_price": p.entry_price,
        "take_profit": p.take_profit,
        "take_profit_2": p.take_profit_2,
        "stop_loss": p.stop_loss,
        "quantity": p.quantity,
        "order_validity": "day_only",  # 当日限り(本日中)固定
        "order_style": "IFO" if (p.take_profit is not None and p.stop_loss is not None) else "IFD",
        "ai_confidence": p.ai_confidence,
        "reason": p.reason,
        "confidence": p.confidence,
        "issues": p.issues,
    }


# ---- 動作確認用サンプル ---------------------------------------------
if __name__ == "__main__":
    samples = [
        "本日の注目銘柄:7203 トヨタ 買いエントリー2,850円 利確2,900円 損切2,820円",
        "①9984 ソフトバンクG 売りIN 8500円 TP8300円 SL8600円 200株\n"
        "②6758 ソニー 買い 指値 3200円 目標株価3300円",
        "8306 三菱UFJ 成行で買い 損切1180円",  # TP無し・情報一部欠落のテスト
    ]

    for i, s in enumerate(samples, 1):
        print(f"--- サンプル{i} ---")
        for order in parse_message(s):
            print(to_order_dict(order))
        print()
