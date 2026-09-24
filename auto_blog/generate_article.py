"""
記事自動生成スクリプト(GitHub Actions から毎日1回実行される想定)

処理の流れ:
    1. topics.txt の先頭から未使用テーマを1つ取り出す
       (空なら既存記事タイトルと重複しない新テーマを Claude に補充させる)
    2. Claude API(Web検索付き)で記事本文を生成
    3. posts/YYYY-MM-DD-<slug>.md として保存し、topics.txt から消費済みテーマを削除

必要な環境変数:
    ANTHROPIC_API_KEY   Anthropic の APIキー
    BLOG_MODEL          使用モデル(省略時 claude-sonnet-5。品質重視なら claude-opus-5)
    BLOG_WEB_SEARCH     "0" で Web検索を無効化(省略時 有効)
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic

BASE_DIR = Path(__file__).resolve().parent
POSTS_DIR = BASE_DIR / "posts"
TOPICS_FILE = BASE_DIR / "topics.txt"
CONFIG_FILE = BASE_DIR / "config.json"

JST = timezone(timedelta(hours=9))
MODEL = os.environ.get("BLOG_MODEL") or "claude-sonnet-5"
USE_WEB_SEARCH = os.environ.get("BLOG_WEB_SEARCH", "1") != "0"
TOPIC_REFILL_COUNT = 20
MAX_CONTINUATIONS = 5  # Web検索で pause_turn になった場合の再開上限

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

client = anthropic.Anthropic()


def _fallback_kwargs() -> dict:
    """Opus / Fable は拒否時に別モデルで自動再実行するサーバー側フォールバックを使う。"""
    if MODEL.startswith(("claude-opus-5", "claude-fable-5")):
        return {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    return {}


def _call_claude(system: str, user: str, *, web_search: bool, max_tokens: int = 32000) -> str:
    """Claude を呼び出してテキスト部分を連結して返す。拒否時は例外。"""
    messages: list[dict] = [{"role": "user", "content": user}]
    tools = (
        [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}]
        if web_search
        else anthropic.NOT_GIVEN
    )
    texts: list[str] = []
    for _ in range(MAX_CONTINUATIONS + 1):
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            **_fallback_kwargs(),
        ) as stream:
            response = stream.get_final_message()

        if response.stop_reason == "refusal":
            raise RuntimeError(f"Claude がリクエストを拒否しました: {response.stop_details}")
        texts.extend(b.text for b in response.content if b.type == "text")
        if response.stop_reason == "pause_turn":
            # サーバーツール(Web検索)の途中で一時停止 → そのまま続きを依頼する
            messages.append({"role": "assistant", "content": response.content})
            continue
        if response.stop_reason == "max_tokens":
            raise RuntimeError("出力が max_tokens で打ち切られました")

        # 検索前の「調べます」等の前置きも混ざるが、呼び出し側は TITLE: 等の行だけを拾う
        return "".join(texts).strip()

    raise RuntimeError("pause_turn が上限回数を超えました")


# ------------------------------------------------------------------
# テーマ管理
# ------------------------------------------------------------------
def load_topics() -> list[str]:
    if not TOPICS_FILE.exists():
        return []
    return [
        line.strip()
        for line in TOPICS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def save_topics(topics: list[str]) -> None:
    header = [
        line
        for line in TOPICS_FILE.read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    ] if TOPICS_FILE.exists() else []
    TOPICS_FILE.write_text("\n".join(header + topics) + "\n", encoding="utf-8")


def existing_titles() -> list[str]:
    titles = []
    for path in sorted(POSTS_DIR.glob("*.md")):
        meta, _ = parse_post(path.read_text(encoding="utf-8"))
        if meta.get("title"):
            titles.append(meta["title"])
    return titles


def refill_topics(config: dict) -> list[str]:
    """テーマ切れ時に、既存記事と被らない新テーマを Claude に考えさせる。"""
    titles = existing_titles()
    system = "あなたは日本語ブログの編集長です。検索需要があり、読者の具体的な悩みを解決する記事テーマを企画します。"
    user = (
        f"ブログ名: {config['site_name']}\nコンセプト: {config['description']}\n\n"
        f"既存記事タイトル(これらと重複・類似しないこと):\n"
        + "\n".join(f"- {t}" for t in titles[-200:])
        + f"\n\n新しい記事テーマを{TOPIC_REFILL_COUNT}個、1行に1つずつ、番号や記号を付けずに出力してください。"
        "検索されやすい具体的なキーワード(ツール名・作業名・悩み)を含め、30〜45字程度にしてください。"
        "テーマ以外の文章は一切出力しないでください。"
    )
    text = _call_claude(system, user, web_search=False, max_tokens=8000)
    topics = [re.sub(r"^[\s\-・*\d.)]+", "", line).strip() for line in text.splitlines()]
    topics = [t for t in topics if t and t not in titles]
    logger.info("テーマを %d 件補充しました", len(topics))
    return topics


# ------------------------------------------------------------------
# 記事ファイル形式(シンプルな front matter)
# ------------------------------------------------------------------
def parse_post(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    meta = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta, text[end + 5 :]


def dump_post(meta: dict, body: str) -> str:
    header = "\n".join(f"{k}: {v}" for k, v in meta.items())
    return f"---\n{header}\n---\n{body.strip()}\n"


# ------------------------------------------------------------------
# 記事生成
# ------------------------------------------------------------------
ARTICLE_SYSTEM = """あなたは生成AI・業務効率化に詳しい日本語テクニカルライターです。
読者が記事を読みながらその場で真似できる、具体的で正確な実践記事を書きます。

守ること:
- 手順は番号付きで具体的に書き、そのまま使えるプロンプト例やテンプレートをコードブロックで示す
- 料金・機能・仕様など変わりやすい事実は、Web検索で最新情報を確認してから書く。確認できない数値や統計は書かない
- 誇張・断定的な効果保証(「必ず稼げる」「100%」など)をしない
- 個人情報や機密情報をAIに入力する際の注意など、読者のリスクになる点は必ず触れる
- 見出しは Markdown の ## と ### を使う(# は使わない)
- 本文は3000〜5000字程度。冒頭に「この記事でわかること」を箇条書きで、最後に「まとめ」を置く
"""


def generate_article(topic: str, config: dict) -> tuple[dict, str]:
    today = datetime.now(JST).strftime("%Y年%m月%d日")
    user = f"""今日は{today}です。次のテーマでブログ記事を1本書いてください。

テーマ: {topic}
ブログ名: {config['site_name']}({config['tagline']})

出力形式(厳守。この形式以外の前置き・後書きは出力しない):
TITLE: 検索されやすい記事タイトル(32字前後)
DESCRIPTION: 検索結果に表示される説明文(100〜120字)
SLUG: 英小文字とハイフンだけのURL用スラッグ(例: chatgpt-meeting-minutes)
TAGS: カンマ区切りのタグ3〜5個
===
(ここから Markdown 本文)
"""
    text = _call_claude(ARTICLE_SYSTEM, user, web_search=USE_WEB_SEARCH)

    if "===" not in text:
        raise ValueError(f"出力形式が不正です(=== 区切りなし):\n{text[:500]}")
    head, body = text.split("===", 1)
    meta: dict[str, str] = {}
    for m in re.finditer(r"(TITLE|DESCRIPTION|SLUG|TAGS)\s*[:：]\s*(.+)", head):
        meta[m.group(1).lower()] = m.group(2).strip()
    if not meta.get("title") or len(body.strip()) < 1500:
        raise ValueError(f"タイトル欠落または本文が短すぎます(本文{len(body.strip())}字)")

    slug = re.sub(r"[^a-z0-9-]+", "-", meta.get("slug", "").lower()).strip("-")[:60] or "post"
    meta["slug"] = slug
    meta["date"] = datetime.now(JST).strftime("%Y-%m-%d")
    meta["topic"] = topic
    return meta, body


def main() -> int:
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    POSTS_DIR.mkdir(exist_ok=True)

    today = datetime.now(JST).strftime("%Y-%m-%d")
    if any(POSTS_DIR.glob(f"{today}-*.md")) and os.environ.get("BLOG_FORCE") != "1":
        logger.info("本日分の記事は既に存在するためスキップします")
        return 0

    topics = load_topics()
    if not topics:
        topics = refill_topics(config)
        if not topics:
            logger.error("テーマを用意できませんでした")
            return 1

    topic = topics[0]
    logger.info("テーマ: %s (モデル: %s, Web検索: %s)", topic, MODEL, USE_WEB_SEARCH)
    meta, body = generate_article(topic, config)

    path = POSTS_DIR / f"{meta['date']}-{meta['slug']}.md"
    n = 2
    while path.exists():
        path = POSTS_DIR / f"{meta['date']}-{meta['slug']}-{n}.md"
        n += 1
    path.write_text(dump_post(meta, body), encoding="utf-8")
    save_topics(topics[1:])
    logger.info("記事を保存しました: %s (%s)", path.name, meta["title"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
