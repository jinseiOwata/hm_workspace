"""
朝のpush送信フロー(system_specification.md の未実装コンポーネント①)

処理の流れ:
    1. yfinance で日経225全銘柄の株価データを一括取得
    2. 移動平均線・RSI からローカルでシグナルの分かりやすさをスコアリングし、
       上位候補(TOP_CANDIDATES_COUNT件)に絞り込む
    3. 候補銘柄のデータ + analysis_prompt_addendum.md の指示を Claude API に送り、
       本日IFD/IFO発注に向いている3〜5銘柄を選ばせる(人間可読の文章 + 末尾JSON)
    4. line_analysis_parser.parse_message() でレスポンスを解析し、
       to_order_dict() でスコア計算用の辞書に変換
    5. 通知フィルタリング(買いシグナルのみ・期待値スコア上位3〜5件)を適用
    6. line_selection.save_today_signals() で保存(通し番号付与)
    7. line_selection.format_signals_message() でテキスト生成し、LINEにbroadcast送信

必要ライブラリ (事前に実行してください):
    pip install yfinance pandas anthropic requests python-dotenv

事前に設定する環境変数 (.env):
    ANTHROPIC_API_KEY          Anthropic の APIキー
    LINE_CHANNEL_ACCESS_TOKEN  LINE公式アカウント(Messaging API)のチャネルアクセストークン

注記:
    Webhookサーバー(line_webhook.py)がまだ常時稼働しておらず、followイベント経由の
    userIdが未取得のため、当面は特定ユーザー宛のpushではなく、公式アカウント
    「HamaLines」の友だち全員に送るbroadcast APIを使って通知する
    (system_specification.md 記載の設計とは異なる暫定対応。Webhook常時稼働の
    目処が立ったら、保存済みuserId宛のpushに切り替えることを検討する)。
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import anthropic
import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types

from topix500_tickers import TOPIX500_TICKERS
from line_analysis_parser import parse_message, to_order_dict
from line_selection import save_today_signals, format_signals_message

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 設定値 (必要に応じて書き換えてください)
# ------------------------------------------------------------------
HISTORY_PERIOD = "3mo"
SHORT_MA_WINDOW = 5
LONG_MA_WINDOW = 25
RSI_WINDOW = 14
TOP_CANDIDATES_COUNT = 30  # ローカルスコアで絞り込んでAIに渡す候補数(母集団はTOPIX500相当)
CROSSCHECK_TOP_N = 10  # 各AIに「おすすめ度順で最大何件」出させるか(上限であり目標ではない)
FINAL_SIGNALS_COUNT = 5  # 最終的にLINEへ通知する最大件数(①両AI一致→②Claude単独→③Gemini単独の順で埋める)

# ------------------------------------------------------------------
# ⚠️ 暫定ポリシー値 (要見直し) ⚠️
# system_specification.md で「未決定事項」として残っていた項目に、
# 運用しながら調整する前提で暫定的に入れている数値。他の設定値と違い、
# 実際の相場でのパフォーマンスを見ながら定期的に見直すこと。
# ------------------------------------------------------------------
# risk_rewardがこの値を超えるシグナルは、分母(entry_price - stop_loss)が
# 極端に小さいことによる外れ値とみなして候補から除外する。
RISK_REWARD_MAX = 10.0

# 単価上限(v2.0改訂: 3,800円→10,000円)。
# 総資金50万円・100株単位という前提では理論上の購入上限は5,000円だが、
# GMO PG相当(単価7,800円=100株78万円)は購入可能として扱う方針としたため、
# 実際に確保する資金も単価に応じて最大約100万円まで柔軟に見る前提で引き上げた。
MAX_UNIT_PRICE = 10000.0

# RSI過熱感ルール(v2.0改訂: 「70以上は一律除外」を廃止し、なだらかな減点方式に変更)。
# 根拠: 実績データ(7/13〜7/24, 全30件)でLoss/Trap4件中、RSI過熱が明確な原因だったのは
# キヤノン(7/21)の1件のみ(25%)。一律除外はトレンド追随狙いの戦略と矛盾するため、
# 65〜70でなだらかに減点・70以上でより強く減点する方式に変更した。
RSI_CAUTION_THRESHOLD = 65.0
RSI_OVERHEAT_THRESHOLD = 70.0

# 両AI一致銘柄への期待値スコア倍率(v2.0で新規追加)。
# 根拠: 実績データで「両AI一致」明記の5件が全て5/5でWin(全体勝率63.2%を大幅に上回る)。
# 倍率は暫定値。件数が増えた段階で妥当性を再検証すること。
AI_AGREEMENT_MULTIPLIER = 1.3

# 節目(ラウンドナンバー)手前の利確オフセット(v2.0で新規追加)。
# 根拠: 7/24に同一の節目(3,300円)でJR東海はブレイク成功・楽天銀行は手前で失速、という
# 対照的な結果が同日に発生。強いトレンド(短期線が長期線を大きく上回る)場合はブレイクを
# 狙うためオフセットせず、それ以外は節目の手前で利確する条件付きルールとした。
ROUND_NUMBER_STEP = 100  # 「節目」とみなす価格間隔(100円単位)
ROUND_NUMBER_OFFSET = 5  # 節目手前で利確する場合のオフセット(円)
ROUND_NUMBER_TOLERANCE = 5  # take_profitがこの範囲内にあれば「節目付近」とみなす
MOMENTUM_MA_GAP_THRESHOLD = 0.02  # 短期線が長期線をこの比率以上上回っていれば強いトレンドとみなす

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = "claude-sonnet-5"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-2.5-flash"

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_BROADCAST_API_URL = "https://api.line.me/v2/bot/message/broadcast"
LINE_MESSAGE_MAX_LENGTH = 4900

BASE_ANALYSIS_INSTRUCTIONS = (
    "あなたはプロの株式トレーダーです。複数銘柄の株価データとテクニカル指標が"
    "与えられます。この中から、本日『買い』のIFD/IFO(逆指値付き)発注に向いている"
    f"銘柄を、おすすめ度(確信度)が高い順に最大{CROSSCHECK_TOP_N}件選んでください。\n\n"
    f"{CROSSCHECK_TOP_N}件は上限であって目標ではありません。無理に件数を埋めるための"
    "弱い推奨は含めないでください。本当に買いと言える銘柄が3件しかなければ3件で"
    "構いません。\n\n"
    "売りシグナルは今回対象外です。買い方向でしか説得力がない状況なので、"
    "売り目線でしか語れない銘柄は選ばないでください。判断が微妙な銘柄や、"
    "様子見にしかならない銘柄も選ばないでください。\n\n"
    f"また、現在値が{int(MAX_UNIT_PRICE):,}円を超える銘柄は資金枠上100株の購入が"
    "難しいため、選定対象から除外してください。\n\n"
    "選んだ銘柄ごとに、次の内容を人間が読める文章で説明してください。\n"
    "- 銘柄コード・会社名・現在値\n"
    "- 買いと判断した根拠を3ポイントで具体的に\n"
    "- エントリー価格帯(上限・下限)、利益確定の目安株価、損切りライン\n"
    "- この判断に対するあなた自身の確信度とその理由\n"
)


@dataclass
class StockSnapshot:
    ticker: str
    company_name: str
    latest_date: str
    latest_close: float
    change_pct: float
    short_ma: float
    long_ma: float
    rsi: float
    recent_summary: str
    signal_score: float


def _calculate_rsi(close: pd.Series, window: int) -> pd.Series:
    """RSI (Relative Strength Index) を算出する。

    (このロジックは D:\\workspace\\stock_line_notifier\\stock_analysis.py にも
    同内容がコピーされている。ここを直すときは向こうにも同じ修正を入れること)
    """
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=window).mean()
    avg_loss = loss.rolling(window=window).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    # avg_loss=0の期間はrsがNaNになるため個別に補正する:
    #   値上がりのみ(avg_gain>0)なら過熱の極限としてRSI=100
    #   値動きが全く無い(avg_gainも0)場合はRSI=50(中立)とする
    zero_loss = avg_loss == 0
    rsi = rsi.mask(zero_loss & (avg_gain > 0), 100.0)
    rsi = rsi.mask(zero_loss & (avg_gain == 0), 50.0)
    return rsi.fillna(50)


def _build_snapshot(ticker: str, df: pd.DataFrame) -> StockSnapshot:
    """1銘柄分のOHLCVデータからテクニカル指標とシグナルスコアを計算する。"""
    df = df.dropna(subset=["Close"])
    required_rows = max(LONG_MA_WINDOW, RSI_WINDOW) + 1
    if len(df) < required_rows:
        raise ValueError(f"データ量が不足しています ({len(df)}行 < 必要な{required_rows}行)")

    df = df.copy()
    df["SMA_short"] = df["Close"].rolling(window=SHORT_MA_WINDOW).mean()
    df["SMA_long"] = df["Close"].rolling(window=LONG_MA_WINDOW).mean()
    df["RSI"] = _calculate_rsi(df["Close"], RSI_WINDOW)

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    if pd.isna(latest["SMA_long"]) or pd.isna(latest["RSI"]):
        raise ValueError("指標を計算できませんでした(データ不足)")

    close = float(latest["Close"])
    short_ma = float(latest["SMA_short"])
    long_ma = float(latest["SMA_long"])
    rsi = float(latest["RSI"])
    change_pct = (close - float(prev["Close"])) / float(prev["Close"]) * 100

    rsi_score = abs(rsi - 50) / 50
    ma_gap_score = abs(short_ma - long_ma) / close if close else 0
    signal_score = rsi_score + ma_gap_score

    recent = df.tail(10)[["Close", "SMA_short", "SMA_long", "RSI"]].round(2)

    return StockSnapshot(
        ticker=ticker,
        company_name="",
        latest_date=str(latest.name.date()),
        latest_close=round(close, 2),
        change_pct=round(change_pct, 2),
        short_ma=round(short_ma, 2),
        long_ma=round(long_ma, 2),
        rsi=round(rsi, 2),
        recent_summary=recent.to_string(),
        signal_score=round(signal_score, 4),
    )


def fetch_all_stock_data(tickers: list[str]) -> list[StockSnapshot]:
    """複数銘柄の株価データを一括取得し、指標を計算する。"""
    logger.info("%d銘柄の株価データを一括取得中...", len(tickers))
    try:
        raw = yf.download(
            tickers=tickers,
            period=HISTORY_PERIOD,
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
    except Exception as exc:
        raise RuntimeError("株価データの一括取得に失敗しました") from exc

    if raw is None or raw.empty:
        raise RuntimeError("株価データが取得できませんでした")

    snapshots: list[StockSnapshot] = []
    for ticker in tickers:
        try:
            df = raw[ticker]
        except KeyError:
            logger.warning("スキップ: %s (データが取得できませんでした)", ticker)
            continue
        try:
            snapshots.append(_build_snapshot(ticker, df))
        except ValueError as exc:
            logger.warning("スキップ: %s (%s)", ticker, exc)

    if not snapshots:
        raise RuntimeError("有効な株価データを1件も取得できませんでした")

    logger.info("%d/%d銘柄のデータを取得できました", len(snapshots), len(tickers))
    return snapshots


def select_candidates(
    snapshots: list[StockSnapshot], top_n: int
) -> list[StockSnapshot]:
    """ローカルの簡易スコアでシグナルが明確そうな上位銘柄に絞り込む。"""
    ranked = sorted(snapshots, key=lambda s: s.signal_score, reverse=True)
    return ranked[:top_n]


def fetch_company_names(candidates: list[StockSnapshot]) -> None:
    """候補銘柄(絞り込み後の少数)の会社名を取得し、StockSnapshotに設定する。"""
    for s in candidates:
        try:
            info = yf.Ticker(s.ticker).info
            s.company_name = info.get("shortName") or info.get("longName") or s.ticker
        except Exception as exc:
            logger.warning("会社名の取得に失敗しました: %s (%s)", s.ticker, exc)
            s.company_name = s.ticker


def build_analysis_prompt(candidates: list[StockSnapshot]) -> str:
    """Claudeに渡す候補銘柄のデータ要約テキストを組み立てる。"""
    blocks = [
        (
            f"■ 銘柄コード: {s.ticker} / 会社名: {s.company_name}\n"
            f"最新日付: {s.latest_date} / 現在値: {s.latest_close} (前日比 {s.change_pct}%)\n"
            f"{SHORT_MA_WINDOW}日移動平均: {s.short_ma} / "
            f"{LONG_MA_WINDOW}日移動平均: {s.long_ma}\n"
            f"RSI({RSI_WINDOW}日): {s.rsi}\n"
            f"直近10営業日:\n{s.recent_summary}\n"
        )
        for s in candidates
    ]
    return "\n".join(blocks)


def load_addendum() -> str:
    path = BASE_DIR / "analysis_prompt_addendum.md"
    return path.read_text(encoding="utf-8")


def generate_analysis(data_summary: str) -> str:
    """Claude APIに候補銘柄のデータを送信し、分析結果(本文+JSON)を取得する。"""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("環境変数 ANTHROPIC_API_KEY が設定されていません。")

    system_prompt = BASE_ANALYSIS_INSTRUCTIONS + "\n\n" + load_addendum()
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    try:
        # 候補数・選定件数が増えて出力が長くなりやすいため、ストリーミングで
        # 送信しHTTPタイムアウトを避ける(max_tokensも余裕を持たせる)。
        with client.messages.stream(
            model=ANTHROPIC_MODEL,
            max_tokens=16000,
            system=system_prompt,
            messages=[{"role": "user", "content": data_summary}],
        ) as stream:
            response = stream.get_final_message()
    except anthropic.AuthenticationError as exc:
        raise RuntimeError("Anthropic APIキーが無効です。") from exc
    except anthropic.RateLimitError as exc:
        raise RuntimeError(
            "Anthropic APIのレート制限に達しました。時間をおいて再試行してください。"
        ) from exc
    except anthropic.APIStatusError as exc:
        raise RuntimeError(f"Anthropic APIエラー ({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise RuntimeError("Anthropic APIへの接続に失敗しました。") from exc

    if response.stop_reason == "refusal":
        raise RuntimeError("Claudeが分析リクエストを拒否しました(安全性の理由)。")
    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            "Claudeの応答がmax_tokens上限に達し、途中で切れました"
            "(JSON部分が壊れて解析できないため異常終了とする)。max_tokensを増やしてください。"
        )

    text_blocks = [block.text for block in response.content if block.type == "text"]
    if not text_blocks:
        raise RuntimeError("Claudeから有効なテキスト応答が得られませんでした。")

    return "\n".join(text_blocks)


GEMINI_SYSTEM_INSTRUCTION = (
    "あなたはプロの株式トレーダーです。複数銘柄の株価データとテクニカル指標が"
    "与えられます。この中から、本日『買い』のIFD/IFO(逆指値付き)発注に向いている"
    f"銘柄を、おすすめ度(確信度)が高い順に最大{CROSSCHECK_TOP_N}件選んでください。\n\n"
    f"{CROSSCHECK_TOP_N}件は上限であって目標ではありません。無理に件数を埋めるための"
    "弱い推奨は含めないでください。売り目線でしか語れない銘柄、判断が微妙な銘柄、"
    "様子見にしかならない銘柄は選ばないでください。選定基準を甘くしないでください。\n\n"
    f"また、現在値が{int(MAX_UNIT_PRICE):,}円を超える銘柄は資金枠上100株の購入が"
    "難しいため、選定対象から除外してください。\n\n"
    "選んだ銘柄ごとに、次の項目をすべて埋めてください(他のAIとの突合・単独表示"
    "どちらにも使われるため、省略しないこと)。\n"
    "- name: 会社名(必須)\n"
    "- entry_low / entry_high: エントリー価格帯の下限・上限\n"
    "- take_profit: 最初の目標株価を1つ\n"
    "- take_profit_2: 2つ目の目標株価があれば(無ければ省略可)\n"
    "- stop_loss: 損切りライン\n"
    "- ai_confidence: このシグナルへの自信度・期待値を0.0〜1.0で(1.0が最高)\n"
    "- reason: 選定理由を全角40〜60字程度の一文で"
)

GEMINI_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "signals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "証券コードのみ。'.T'などのサフィックスは付けないこと(例: '6367.T'ではなく'6367')",
                    },
                    "name": {"type": "string", "description": "会社名"},
                    "entry_low": {"type": "number", "description": "エントリー価格帯の下限"},
                    "entry_high": {"type": "number", "description": "エントリー価格帯の上限"},
                    "take_profit": {"type": "number", "description": "最初の目標株価"},
                    "take_profit_2": {"type": "number", "description": "2つ目の目標株価(あれば)"},
                    "stop_loss": {"type": "number", "description": "損切りライン"},
                    "ai_confidence": {
                        "type": "number",
                        "description": "このシグナルへの自信度・期待値(0.0〜1.0、1.0が最高)",
                    },
                    "reason": {"type": "string", "description": "選定理由を一文で(全角40〜60字程度)"},
                },
                "required": [
                    "code", "name", "entry_low", "entry_high", "take_profit",
                    "stop_loss", "ai_confidence", "reason",
                ],
            },
        },
    },
    "required": ["signals"],
}


def generate_gemini_analysis(data_summary: str) -> list[dict]:
    """Gemini APIに同じ候補データを送り、Claudeとは独立に買いシグナル判定を得る。"""
    if not GEMINI_API_KEY:
        raise RuntimeError("環境変数 GEMINI_API_KEY が設定されていません。")

    client = genai.Client(api_key=GEMINI_API_KEY)

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=data_summary,
            config=genai_types.GenerateContentConfig(
                system_instruction=GEMINI_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=GEMINI_RESPONSE_SCHEMA,
                max_output_tokens=16000,
            ),
        )
    except Exception as exc:
        raise RuntimeError(f"Gemini APIの呼び出しに失敗しました: {exc}") from exc

    if response.candidates:
        finish_reason = response.candidates[0].finish_reason
        if finish_reason == genai_types.FinishReason.MAX_TOKENS:
            raise RuntimeError(
                "Geminiの応答がmax_output_tokens上限に達し、途中で切れました。"
                "max_output_tokensを増やしてください。"
            )

    if not response.text:
        raise RuntimeError("Geminiから有効なテキスト応答が得られませんでした。")

    # Claude側のlast_analysis_raw.txtと同じ理由で、生レスポンスを毎回保存しておく
    # (パース結果がおかしいときに実際の応答と突き合わせて調査できるようにするため)
    (BASE_DIR / "last_gemini_raw.txt").write_text(response.text, encoding="utf-8")

    try:
        data = json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Geminiの応答(JSON)を解析できませんでした: {exc}") from exc

    return data.get("signals", [])


def _normalize_code(code: Optional[str]) -> Optional[str]:
    """銘柄コードの表記ゆれ(末尾の.T、前後の空白)を吸収して比較できるようにする。"""
    if not code:
        return None
    code = code.strip().upper()
    if code.endswith(".T"):
        code = code[:-2]
    return code


def _score_risk_reward(entry, tp, sl, ai_conf) -> tuple[Optional[float], Optional[float]]:
    """risk_rewardとexpected_value_scoreを計算する。計算不能/外れ値なら(None, None)。"""
    if entry is None or tp is None or sl is None or ai_conf is None:
        return None, None
    denominator = entry - sl
    if denominator <= 0:
        return None, None
    risk_reward = (tp - entry) / denominator
    if risk_reward <= 0 or risk_reward > RISK_REWARD_MAX:
        return None, None
    return round(risk_reward, 4), round(risk_reward * ai_conf, 4)


def _rsi_penalty_factor(rsi: Optional[float]) -> float:
    """RSI過熱度に応じた期待値スコアの減点係数(v2.0で一律除外から変更)。

    RSI_CAUTION_THRESHOLD未満は減点なし、そこからRSI_OVERHEAT_THRESHOLDまでは
    なだらかに、それ以降はより強く減点する。rsiが不明な場合は減点しない
    (Gemini単独候補などRSIを引けないケースで不当に不利にしないため)。
    """
    if rsi is None:
        return 1.0
    if rsi < RSI_CAUTION_THRESHOLD:
        return 1.0
    if rsi < RSI_OVERHEAT_THRESHOLD:
        span = RSI_OVERHEAT_THRESHOLD - RSI_CAUTION_THRESHOLD
        return 1.0 - 0.15 * (rsi - RSI_CAUTION_THRESHOLD) / span
    return max(0.4, 0.85 - 0.045 * (rsi - RSI_OVERHEAT_THRESHOLD))


def _apply_round_number_offset(
    take_profit: Optional[float], ma_gap_ratio: Optional[float]
) -> Optional[float]:
    """節目(ラウンドナンバー)付近のtake_profitを手前にずらす(v2.0で新規追加)。

    強いトレンド(短期線が長期線をMOMENTUM_MA_GAP_THRESHOLD以上上回る)場合は
    ブレイクを狙うためオフセットしない。ma_gap_ratioが不明な場合は安全側
    (オフセットする側)に倒す。
    """
    if take_profit is None:
        return take_profit
    if ma_gap_ratio is not None and ma_gap_ratio >= MOMENTUM_MA_GAP_THRESHOLD:
        return take_profit

    remainder = take_profit % ROUND_NUMBER_STEP
    near_round_number = remainder == 0 or remainder >= (ROUND_NUMBER_STEP - ROUND_NUMBER_TOLERANCE)
    if not near_round_number:
        return take_profit

    rounded_up = math.ceil(take_profit / ROUND_NUMBER_STEP) * ROUND_NUMBER_STEP
    candidate = rounded_up - ROUND_NUMBER_OFFSET
    return candidate if candidate < take_profit else take_profit


def _passes_price_filter(entry_price: Optional[float]) -> bool:
    return entry_price is not None and entry_price <= MAX_UNIT_PRICE


def _gemini_entry_price(s: dict) -> Optional[float]:
    """Geminiの生シグナル(entry_low/entry_high)からエントリー価格(中央値)を求める。"""
    entry_low = s.get("entry_low")
    entry_high = s.get("entry_high")
    if entry_low is not None and entry_high is not None:
        return (float(entry_low) + float(entry_high)) / 2
    if entry_low is not None or entry_high is not None:
        return float(entry_low if entry_low is not None else entry_high)
    return None


def _gemini_own_score(
    s: dict, ma_gap_ratio: Optional[float] = None, rsi: Optional[float] = None
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Gemini自身のentry/TP/SL/ai_confidenceからrisk_reward・期待値スコアを計算する。

    戻り値はrisk_reward, expected_value_score, オフセット適用後のtake_profit。
    """
    entry_price = _gemini_entry_price(s)
    take_profit = _apply_round_number_offset(s.get("take_profit"), ma_gap_ratio)
    stop_loss = s.get("stop_loss")
    ai_confidence = s.get("ai_confidence")
    risk_reward, expected_value_score = _score_risk_reward(
        entry_price, take_profit, stop_loss, ai_confidence
    )
    if expected_value_score is not None:
        expected_value_score = round(expected_value_score * _rsi_penalty_factor(rsi), 4)
    return risk_reward, expected_value_score, take_profit


def gemini_signal_to_order_dict(s: dict, candidate_info: Optional[dict] = None) -> dict:
    """Geminiが独自に出したシグナルを、Claude側と同じ表示用スキーマに変換する。

    両AI不一致で、Gemini単独の上位を③の補完材料として表示する場合に使う。
    v2.0追加分: 単価フィルター・RSI減点・節目手前オフセットをClaude側と揃えて適用する。
    """
    candidate_info = candidate_info or {}
    rsi = candidate_info.get("rsi")
    ma_gap_ratio = candidate_info.get("ma_gap_ratio")

    entry_price = _gemini_entry_price(s)

    take_profit = float(s["take_profit"]) if s.get("take_profit") is not None else None
    take_profit_2 = float(s["take_profit_2"]) if s.get("take_profit_2") is not None else None
    stop_loss = float(s["stop_loss"]) if s.get("stop_loss") is not None else None
    ai_confidence = float(s["ai_confidence"]) if s.get("ai_confidence") is not None else None

    take_profit = _apply_round_number_offset(take_profit, ma_gap_ratio)

    od = {
        "code": _normalize_code(s.get("code")),
        "name": s.get("name"),
        "side": "buy",
        "entry_type": "limit",
        "entry_price": entry_price,
        "take_profit": take_profit,
        "take_profit_2": take_profit_2,
        "stop_loss": stop_loss,
        "quantity": None,
        "order_validity": "day_only",
        "order_style": "IFO" if (take_profit is not None and stop_loss is not None) else "IFD",
        "ai_confidence": ai_confidence,
        "reason": s.get("reason"),
        "confidence": "ok",
        "issues": [],
        "rsi": rsi,
    }
    # このスコアはGemini自身のentry/TP/SLから計算したものなので、Claude側の
    # expected_value_scoreと混同しないよう gemini_expected_value_score に入れる
    # (③はClaudeの数値を一切持たないため、表示側で「Gemini」と明示させる)。
    risk_reward, expected_value_score = _score_risk_reward(
        entry_price, take_profit, stop_loss, ai_confidence
    )
    if risk_reward is not None:
        od["gemini_risk_reward"] = risk_reward
        od["gemini_expected_value_score"] = round(
            expected_value_score * _rsi_penalty_factor(rsi), 4
        )
    return od


def cross_check_with_gemini(
    claude_candidates: list[dict],
    data_summary: str,
    candidate_info_by_code: Optional[dict] = None,
) -> list[dict]:
    """ClaudeとGeminiが独立に判断した「買い」候補から、最大FINAL_SIGNALS_COUNT件を
    次の優先順位で決定する:
      ① ClaudeとGeminiが一致した銘柄(両AI確信度平均の降順、AI_AGREEMENT_MULTIPLIERで加点)
      ② 足りなければClaudeの単独上位(期待値スコア順)で補完
      ③ まだ足りなければGeminiの単独上位(確信度順)で補完
    Gemini呼び出しに失敗した場合はClaude単独の結果にフォールバックする
    (Geminiは補助的な確認であり、単一障害点にしない)。
    """
    candidate_info_by_code = candidate_info_by_code or {}
    try:
        gemini_signals = generate_gemini_analysis(data_summary)
    except RuntimeError as exc:
        logger.warning(
            "Geminiとの突合に失敗したため、Claude単独の結果を使用します: %s", exc
        )
        result = []
        for od in claude_candidates[:FINAL_SIGNALS_COUNT]:
            od = dict(od)
            od["gemini_agreed"] = None  # 突合できなかったことを示す
            od["gemini_confidence"] = None
            od["combined_confidence"] = None
            result.append(od)
        return result

    gemini_by_code = {}
    for s in gemini_signals:
        code = _normalize_code(s.get("code"))
        if not code or code in gemini_by_code:  # 同一コードの重複が来ても最初の1件だけ使う
            continue
        entry_price = _gemini_entry_price(s)
        if not _passes_price_filter(entry_price):
            logger.info("除外(単価上限%s円超・Gemini): %s entry=%s", MAX_UNIT_PRICE, code, entry_price)
            continue
        gemini_by_code[code] = s

    # Gemini自身のentry/TP/SLからの期待値スコアを、コードごとに一度だけ計算しておく
    # (①の一致判定・③の単独補完どちらでも同じ値を使う)。RSI減点・節目オフセットは
    # Claude側で使ったのと同じcandidate_info_by_codeを参照して揃える。
    gemini_score_by_code = {}
    for code, s in gemini_by_code.items():
        info = candidate_info_by_code.get(code, {})
        risk_reward, expected_value_score, offset_tp = _gemini_own_score(
            s, ma_gap_ratio=info.get("ma_gap_ratio"), rsi=info.get("rsi")
        )
        gemini_score_by_code[code] = (risk_reward, expected_value_score, offset_tp)

    gemini_ranked_codes = sorted(
        gemini_by_code,
        key=lambda c: gemini_score_by_code[c][1] or 0,  # expected_value_score降順
        reverse=True,
    )
    logger.info("Geminiが買いと判断した銘柄: %s", gemini_ranked_codes)

    used_codes: set[str] = set()
    result: list[dict] = []

    # ① 両AI一致(重複コード対策として、既にagreedに入れたコードは2回拾わない)
    agreed = []
    agreed_codes: set[str] = set()
    for od in claude_candidates:
        code = _normalize_code(od.get("code"))
        if code not in gemini_by_code or code in agreed_codes:
            continue
        agreed_codes.add(code)
        od = dict(od)
        od["issues"] = list(od.get("issues") or [])  # issuesリストの参照共有を断つ
        gemini_signal = gemini_by_code[code]
        gemini_conf = gemini_signal.get("ai_confidence")
        claude_conf = od.get("ai_confidence")
        od["gemini_agreed"] = True
        od["gemini_confidence"] = gemini_conf
        od["combined_confidence"] = (
            (claude_conf + gemini_conf) / 2
            if claude_conf is not None and gemini_conf is not None
            else claude_conf
        )
        # 期待値スコアは両AIそれぞれのentry/TP/SLを使って個別に計算し、両方表示する。
        # 並び順は両AIの期待値スコアの平均を使う(片方が計算不能ならもう片方だけで代用)。
        claude_score = od.get("expected_value_score")
        gemini_risk_reward, gemini_expected_value_score, _gemini_tp = gemini_score_by_code[code]
        od["gemini_risk_reward"] = gemini_risk_reward
        od["gemini_expected_value_score"] = gemini_expected_value_score
        if claude_score is not None and gemini_expected_value_score is not None:
            base_score = (claude_score + gemini_expected_value_score) / 2
        else:
            base_score = claude_score if claude_score is not None else gemini_expected_value_score
        # v2.0: 両AI一致銘柄はAI_AGREEMENT_MULTIPLIERで加点する
        # (根拠: 実績データで両AI一致5件が全て5/5でWin、全体勝率63.2%を大幅に上回った)
        od["combined_expected_value_score"] = (
            round(base_score * AI_AGREEMENT_MULTIPLIER, 4) if base_score is not None else None
        )
        agreed.append(od)

    agreed.sort(
        key=lambda od: (
            od["combined_expected_value_score"] is not None,
            od["combined_expected_value_score"] or 0,
        ),
        reverse=True,
    )
    for od in agreed:
        if len(result) >= FINAL_SIGNALS_COUNT:
            break
        result.append(od)
        used_codes.add(_normalize_code(od.get("code")))

    # ② Claude単独上位で補完(claude_candidatesは既にexpected_value_score順)
    if len(result) < FINAL_SIGNALS_COUNT:
        for od in claude_candidates:
            if len(result) >= FINAL_SIGNALS_COUNT:
                break
            code = _normalize_code(od.get("code"))
            if code in used_codes:
                continue
            od = dict(od)
            od["issues"] = list(od.get("issues") or [])
            od["gemini_agreed"] = False
            od["gemini_confidence"] = None
            od["combined_confidence"] = None
            od["combined_expected_value_score"] = None
            result.append(od)
            used_codes.add(code)

    # ③ Gemini単独上位で補完(gemini_ranked_codesは既に期待値スコア順)
    if len(result) < FINAL_SIGNALS_COUNT:
        for code in gemini_ranked_codes:
            if len(result) >= FINAL_SIGNALS_COUNT:
                break
            if code in used_codes:
                continue
            od = gemini_signal_to_order_dict(
                gemini_by_code[code], candidate_info=candidate_info_by_code.get(code)
            )
            od["gemini_agreed"] = "gemini_only"  # Claudeが挙げていない、Gemini単独の候補
            od["gemini_confidence"] = od["ai_confidence"]
            od["combined_confidence"] = None
            od["combined_expected_value_score"] = None
            result.append(od)
            used_codes.add(code)

    return result


def apply_notification_filter(
    order_dicts: list[dict], candidate_info_by_code: Optional[dict] = None
) -> list[dict]:
    """通知フィルタリングロジック(system_specification.md 参照):
    買いシグナルのみ・期待値スコア(risk_reward × ai_confidence)上位CROSSCHECK_TOP_N件に絞る。
    (この後さらにcross_check_with_geminiでGeminiとの一致分だけに絞られる)

    v2.0追加分: 単価フィルター(MAX_UNIT_PRICE)、RSI段階的減点、節目手前オフセットを適用する。
    """
    candidate_info_by_code = candidate_info_by_code or {}
    scored = []
    for od in order_dicts:
        if od.get("side") != "buy":
            logger.info("除外(買いシグナルではない): %s side=%s", od.get("code"), od.get("side"))
            continue
        if od.get("confidence") != "ok":
            logger.info(
                "除外(要確認扱い): %s issues=%s", od.get("code"), od.get("issues")
            )
            continue

        entry = od.get("entry_price")
        tp = od.get("take_profit")
        sl = od.get("stop_loss")
        ai_conf = od.get("ai_confidence")
        if entry is None or tp is None or sl is None or ai_conf is None:
            logger.warning("スコア計算に必要な項目が不足のため除外: %s", od.get("code"))
            continue

        if not _passes_price_filter(entry):
            logger.info(
                "除外(単価上限%s円超): %s entry=%s", MAX_UNIT_PRICE, od.get("code"), entry
            )
            continue

        info = candidate_info_by_code.get(_normalize_code(od.get("code")), {})
        rsi = info.get("rsi")
        ma_gap_ratio = info.get("ma_gap_ratio")

        tp = _apply_round_number_offset(tp, ma_gap_ratio)

        # 実際の合否判定は_score_risk_reward()に一本化する(Gemini側と計算式が
        # 分岐しないように)。以下はNoneが返った場合に理由をログへ出すための診断。
        risk_reward, expected_value_score = _score_risk_reward(entry, tp, sl, ai_conf)
        if risk_reward is None:
            denominator = entry - sl
            if denominator <= 0:
                logger.warning("損切ラインがエントリー価格以上のため除外: %s", od.get("code"))
            else:
                candidate_rr = (tp - entry) / denominator
                if candidate_rr <= 0:
                    logger.warning("risk_rewardが0以下のため除外: %s", od.get("code"))
                else:
                    logger.warning(
                        "risk_rewardが閾値(%s)を超えるため除外: %s (risk_reward=%.2f)",
                        RISK_REWARD_MAX, od.get("code"), candidate_rr,
                    )
            continue

        rsi_factor = _rsi_penalty_factor(rsi)
        expected_value_score = round(expected_value_score * rsi_factor, 4)

        od = dict(od)
        od["issues"] = list(od.get("issues") or [])  # issuesリストの参照共有を断つ
        od["take_profit"] = tp
        od["rsi"] = rsi
        od["rsi_penalty_factor"] = rsi_factor
        od["risk_reward"] = risk_reward
        od["expected_value_score"] = expected_value_score
        scored.append(od)

    scored.sort(key=lambda od: od["expected_value_score"], reverse=True)
    return scored[:CROSSCHECK_TOP_N]


def broadcast_line_message(message: str) -> None:
    """LINE Messaging API (broadcast) で公式アカウントの友だち全員に通知する。"""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise RuntimeError("環境変数 LINE_CHANNEL_ACCESS_TOKEN が設定されていません。")

    if len(message) > LINE_MESSAGE_MAX_LENGTH:
        message = message[:LINE_MESSAGE_MAX_LENGTH] + "\n...(省略)"

    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"messages": [{"type": "text", "text": message}]}

    try:
        response = requests.post(
            LINE_BROADCAST_API_URL, headers=headers, json=payload, timeout=10
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        detail = ""
        if exc.response is not None:
            detail = f" (レスポンス: {exc.response.text})"
        raise RuntimeError(f"LINEへの通知送信に失敗しました: {exc}{detail}") from exc

    logger.info("LINE通知を送信しました。")


def notify_failure(reason: str) -> None:
    """Claude API呼び出し等の失敗時に、その旨をLINEに通知する(サイレント失敗を防ぐ)。"""
    try:
        broadcast_line_message(f"【株価分析Bot】本日は分析取得に失敗しました。\n理由: {reason}")
    except Exception:
        logger.exception("失敗通知の送信自体にも失敗しました。")


def main() -> None:
    try:
        snapshots = fetch_all_stock_data(TOPIX500_TICKERS)
        candidates = select_candidates(snapshots, TOP_CANDIDATES_COUNT)

        # v2.0追加分: RSI減点・節目手前オフセットの判定に使うRSI・移動平均乖離率を
        # 証券コードごとに引けるようにしておく(Claude/Gemini双方のシグナルに適用)。
        candidate_info_by_code = {
            _normalize_code(s.ticker): {
                "rsi": s.rsi,
                "ma_gap_ratio": (
                    (s.short_ma - s.long_ma) / s.long_ma if s.long_ma else None
                ),
            }
            for s in candidates
        }

        logger.info("候補銘柄の会社名を取得中...")
        fetch_company_names(candidates)

        logger.info("Claudeによる分析を実行中... (候補%d銘柄)", len(candidates))
        prompt = build_analysis_prompt(candidates)
        analysis_text = generate_analysis(prompt)
        (BASE_DIR / "last_analysis_raw.txt").write_text(analysis_text, encoding="utf-8")

        parsed_orders = parse_message(analysis_text)
        order_dicts = [to_order_dict(p) for p in parsed_orders]
        logger.info("Claudeが提示したシグナル数: %d", len(order_dicts))
        for od in order_dicts:
            logger.debug(
                "  受信シグナル: %s side=%s entry=%s tp=%s sl=%s ai_confidence=%s confidence=%s",
                od.get("code"), od.get("side"), od.get("entry_price"),
                od.get("take_profit"), od.get("stop_loss"),
                od.get("ai_confidence"), od.get("confidence"),
            )

        filtered = apply_notification_filter(order_dicts, candidate_info_by_code)
        logger.info("Claude側フィルタリング後の候補: %d件", len(filtered))

        logger.info("Geminiによる突合分析を実行中...")
        final_signals = cross_check_with_gemini(filtered, prompt, candidate_info_by_code)
        gemini_failed_count = sum(1 for od in final_signals if od.get("gemini_agreed") is None)
        if gemini_failed_count == len(final_signals) and final_signals:
            logger.info(
                "最終通知対象: %d件 (Gemini突合に失敗したため全件Claude単独)",
                len(final_signals),
            )
        else:
            logger.info(
                "最終通知対象: %d件 (①両AI一致: %d件 / ②Claude単独: %d件 / ③Gemini単独: %d件)",
                len(final_signals),
                sum(1 for od in final_signals if od.get("gemini_agreed") is True),
                sum(1 for od in final_signals if od.get("gemini_agreed") is False),
                sum(1 for od in final_signals if od.get("gemini_agreed") == "gemini_only"),
            )

        if not final_signals:
            broadcast_line_message(
                "【本日の売買シグナル】\n本日は基準を満たす買いシグナルがありませんでした。"
            )
            logger.info("通知対象なし。処理を終了します。")
            return

        numbered = save_today_signals(final_signals)
        message = format_signals_message(numbered)
        broadcast_line_message(message)

        logger.info("処理が完了しました。")

    except RuntimeError as exc:
        logger.error("処理中にエラーが発生しました: %s", exc)
        notify_failure(str(exc))
        sys.exit(1)
    except Exception as exc:  # 想定外のエラーもログに残し、LINEにも通知する
        logger.exception("予期しないエラーが発生しました: %s", exc)
        notify_failure(f"予期しないエラー: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
