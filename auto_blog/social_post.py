"""
新着記事の告知(GitHub Actions の deploy 完了後に実行される想定)

処理の流れ:
    1. posts/ から「まだ告知していない直近の記事」を探す(social_log.json で管理)
    2. Claude に Bluesky 用と X 用の紹介文を書かせる
    3. Bluesky に自動投稿(BLUESKY_HANDLE / BLUESKY_APP_PASSWORD が未設定ならスキップ)
    4. X 用の紹介文を「タップするだけで投稿画面が開くリンク」付きで GitHub Issue にする
       (X API は有料のため、X への投稿は手動)
    5. 告知済みの記事を social_log.json に記録

必要な環境変数:
    ANTHROPIC_API_KEY      Anthropic の APIキー
    SITE_BASE_URL          公開URL(例: https://<user>.github.io/<repo>/)
    BLUESKY_HANDLE         Bluesky のハンドル(例: example.bsky.social)。任意
    BLUESKY_APP_PASSWORD   Bluesky のアプリパスワード。任意
    GITHUB_TOKEN / GITHUB_REPOSITORY  Issue 作成用(Actions では自動で渡す)
    SOCIAL_MODEL           紹介文の生成モデル(省略時 claude-sonnet-5)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
from pydantic import BaseModel

from generate_article import JST, POSTS_DIR, parse_post

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "social_log.json"
CONFIG_FILE = BASE_DIR / "config.json"

MODEL = os.environ.get("SOCIAL_MODEL") or "claude-sonnet-5"
LOOKBACK_DAYS = 3  # 告知漏れを拾う範囲。古い記事をまとめて告知しないよう制限する
MAX_POSTS_PER_RUN = 3
BLUESKY_TEXT_LIMIT = 300  # Bluesky の本文上限(書記素数)
X_TEXT_CHARS = 110  # X 用紹介文の最大文字数(日本語は1字=2カウント、URLは23カウント)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


class SocialTexts(BaseModel):
    bluesky: str
    x: str


# ------------------------------------------------------------------
# 告知対象の記事
# ------------------------------------------------------------------
def load_log() -> dict:
    if LOG_FILE.exists():
        return json.loads(LOG_FILE.read_text(encoding="utf-8"))
    return {}


def save_log(log: dict) -> None:
    LOG_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def find_new_posts(log: dict, today: datetime | None = None) -> list[dict]:
    today = (today or datetime.now(JST)).date()
    since = today - timedelta(days=LOOKBACK_DAYS)
    posts = []
    for path in sorted(POSTS_DIR.glob("*.md")):
        if path.stem in log:
            continue
        meta, body = parse_post(path.read_text(encoding="utf-8"))
        try:
            post_date = datetime.strptime(meta.get("date", ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        if meta.get("title") and post_date >= since:
            meta["stem"] = path.stem
            meta["body"] = body
            posts.append(meta)
    return posts[-MAX_POSTS_PER_RUN:]


def post_url(base_url: str, stem: str) -> str:
    return f"{base_url.rstrip('/')}/{stem}.html"


# ------------------------------------------------------------------
# 紹介文の生成
# ------------------------------------------------------------------
def write_texts(post: dict, site_name: str) -> SocialTexts:
    client = anthropic.Anthropic()
    prompt = f"""ブログ「{site_name}」の新着記事を SNS で紹介する文章を2つ書いてください。

記事タイトル: {post['title']}
記事の説明: {post.get('description', '')}
記事本文の冒頭:
{post['body'][:2000]}

- bluesky: 180字以内。読者が「自分の悩みが解決しそう」と感じる一言+記事で得られることを1〜2点。ハッシュタグは付けない。URLは書かない
- x: {X_TEXT_CHARS - 20}字以内。要点を短く。末尾に関連ハッシュタグを1〜2個。URLは書かない
- どちらも誇張表現(「必ず」「最強」など)や絵文字の多用は避ける
"""
    response = client.messages.parse(
        model=MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
        output_format=SocialTexts,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError(f"紹介文を生成できませんでした (stop_reason={response.stop_reason})")
    return response.parsed_output


def fallback_texts(post: dict) -> SocialTexts:
    """Claude が使えないときの最低限の紹介文。"""
    return SocialTexts(bluesky=f"新着記事: {post['title']}", x=f"新着記事: {post['title']}")


# ------------------------------------------------------------------
# Bluesky
# ------------------------------------------------------------------
def _http_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode("utf-8"))


def build_bluesky_record(text: str, url: str, title: str, description: str, now: datetime | None = None) -> dict:
    """本文+URL(リンクとして認識させる facet 付き)+リンクカードの投稿レコードを作る。"""
    room = BLUESKY_TEXT_LIMIT - len(url) - 1
    if len(text) > room:
        text = text[: room - 1] + "…"
    full_text = f"{text}\n{url}"
    start = len(f"{text}\n".encode("utf-8"))  # facet の位置は UTF-8 のバイト単位で指定する
    return {
        "$type": "app.bsky.feed.post",
        "text": full_text,
        "createdAt": (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"),
        "langs": ["ja"],
        "facets": [
            {
                "index": {"byteStart": start, "byteEnd": start + len(url.encode("utf-8"))},
                "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}],
            }
        ],
        "embed": {
            "$type": "app.bsky.embed.external",
            "external": {"uri": url, "title": title, "description": description[:300]},
        },
    }


def post_to_bluesky(record: dict) -> str:
    session = _http_json(
        "https://bsky.social/xrpc/com.atproto.server.createSession",
        {"identifier": os.environ["BLUESKY_HANDLE"], "password": os.environ["BLUESKY_APP_PASSWORD"]},
    )
    result = _http_json(
        "https://bsky.social/xrpc/com.atproto.repo.createRecord",
        {"repo": session["did"], "collection": "app.bsky.feed.post", "record": record},
        headers={"Authorization": f"Bearer {session['accessJwt']}"},
    )
    return result.get("uri", "")


# ------------------------------------------------------------------
# X 用の下書き(GitHub Issue)
# ------------------------------------------------------------------
def x_intent_url(text: str, url: str) -> str:
    if len(text) > X_TEXT_CHARS:
        text = text[: X_TEXT_CHARS - 1] + "…"
    return "https://x.com/intent/post?" + urllib.parse.urlencode({"text": text, "url": url})


def build_issue_body(items: list[dict]) -> str:
    lines = ["新着記事の X 用紹介文です。「X で投稿する」をタップすると、文章とリンクが入った投稿画面が開きます。", ""]
    for item in items:
        lines += [
            f"## {item['title']}",
            "",
            item["x_text"],
            "",
            f"記事: {item['url']}",
            "",
            f"👉 [X で投稿する]({item['intent']})",
            "",
            f"Bluesky: {item['bluesky_status']}",
            "",
        ]
    lines.append("投稿が終わったらこの Issue は閉じてかまいません。")
    return "\n".join(lines)


def create_github_issue(title: str, body: str) -> str:
    repo = os.environ["GITHUB_REPOSITORY"]
    result = _http_json(
        f"https://api.github.com/repos/{repo}/issues",
        {"title": title, "body": body},
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
        },
    )
    return result.get("html_url", "")


# ------------------------------------------------------------------
def main() -> int:
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    base_url = os.environ.get("SITE_BASE_URL") or config.get("base_url") or ""
    if not base_url:
        logger.error("SITE_BASE_URL が未設定のため告知できません")
        return 1

    log = load_log()
    posts = find_new_posts(log)
    if not posts:
        logger.info("告知する新着記事はありません")
        return 0

    use_bluesky = bool(os.environ.get("BLUESKY_HANDLE") and os.environ.get("BLUESKY_APP_PASSWORD"))
    if not use_bluesky:
        logger.info("Bluesky の認証情報が未設定のため、Bluesky への投稿はスキップします")

    items = []
    for post in posts:
        url = post_url(base_url, post["stem"])
        try:
            texts = write_texts(post, config["site_name"])
        except Exception as e:  # 紹介文の生成失敗で告知自体を止めない
            logger.warning("紹介文の生成に失敗したため定型文を使います: %s", e)
            texts = fallback_texts(post)

        bluesky_status = "未設定のためスキップ"
        if use_bluesky:
            try:
                record = build_bluesky_record(texts.bluesky, url, post["title"], post.get("description", ""))
                uri = post_to_bluesky(record)
                bluesky_status = "投稿済み"
                logger.info("Bluesky に投稿しました: %s", uri)
            except Exception as e:
                bluesky_status = f"投稿失敗({e})"
                logger.error("Bluesky への投稿に失敗しました: %s", e)

        items.append(
            {
                "stem": post["stem"],
                "title": post["title"],
                "url": url,
                "x_text": texts.x,
                "intent": x_intent_url(texts.x, url),
                "bluesky_status": bluesky_status,
            }
        )

    issue_url = ""
    if os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPOSITORY"):
        try:
            issue_url = create_github_issue(
                f"[X投稿] {datetime.now(JST).strftime('%Y-%m-%d')} の新着記事 {len(items)}件", build_issue_body(items)
            )
            logger.info("X 用の下書きを Issue にしました: %s", issue_url)
        except Exception as e:
            logger.error("Issue の作成に失敗しました: %s", e)
    else:
        print(build_issue_body(items))

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(build_issue_body(items) + "\n")

    now = datetime.now(JST).isoformat(timespec="seconds")
    for item in items:
        log[item["stem"]] = {"announced_at": now, "bluesky": item["bluesky_status"], "x_issue": issue_url}
    save_log(log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
