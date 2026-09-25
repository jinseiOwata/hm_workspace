"""
YouTube 新着動画の告知(GitHub Actions から3時間おきに実行される想定)

処理の流れ:
    1. config.json の youtube.handle(または channel_id)からチャンネルの RSS を取得
       (YouTube の API キーは不要)
    2. youtube_log.json にない新着動画を探す(予約公開の動画は公開された後に RSS に現れた時点で対象になる)
       - 初回はチャンネルの既存動画をすべて「告知済み」として記録するだけで投稿しない
         (過去動画をまとめて投稿してしまわないため)
    3. Claude が紹介文を書き、Bluesky に自動投稿(サムネイル付きリンクカード)
    4. X 用の紹介文を GitHub Issue にする(タップで投稿画面が開くリンク付き)
    5. 告知した動画を youtube_log.json に記録

必要な環境変数: social_post.py と同じ(SITE_BASE_URL は不要)
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import social_post as sp
from generate_article import JST

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "youtube_log.json"

MAX_VIDEOS_PER_RUN = 3
USER_AGENT = "Mozilla/5.0 (compatible; auto-blog/1.0)"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

logger = logging.getLogger(__name__)


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ja"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read()


def resolve_channel_id(youtube_config: dict) -> str:
    """設定の channel_id を優先し、なければハンドル(@xxx)のチャンネルページから UC... の ID を取り出す。"""
    if youtube_config.get("channel_id"):
        return youtube_config["channel_id"]
    handle = youtube_config["handle"].lstrip("@")
    html = _get(f"https://www.youtube.com/@{handle}").decode("utf-8", errors="replace")
    for pattern in (
        r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
        r'"externalId":"(UC[\w-]{22})"',
        r'"channelId":"(UC[\w-]{22})"',
    ):
        m = re.search(pattern, html)
        if m:
            return m.group(1)
    raise RuntimeError(f"チャンネルIDを取得できませんでした(@{handle})。config.json の youtube.channel_id に直接設定してください")


def parse_feed(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    videos = []
    for entry in root.findall("atom:entry", NS):
        video_id = entry.findtext("yt:videoId", default="", namespaces=NS)
        group = entry.find("media:group", NS)
        thumb = group.find("media:thumbnail", NS) if group is not None else None
        videos.append(
            {
                "id": video_id,
                "title": entry.findtext("atom:title", default="", namespaces=NS),
                "published": entry.findtext("atom:published", default="", namespaces=NS),
                "description": (group.findtext("media:description", default="", namespaces=NS) if group is not None else ""),
                "thumbnail": thumb.get("url") if thumb is not None else f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            }
        )
    return videos


def _oembed(video_id: str) -> dict:
    """APIキー不要の oEmbed でタイトルとサムネイルを取る(RSS が使えないときの代替)。"""
    try:
        data = json.loads(_get(f"https://www.youtube.com/oembed?format=json&url=https://www.youtube.com/shorts/{video_id}"))
        return {"title": data.get("title", ""), "thumbnail": data.get("thumbnail_url", "")}
    except Exception as e:
        logger.warning("oEmbed で動画情報を取得できませんでした(%s): %s", video_id, e)
        return {"title": "", "thumbnail": ""}


def scrape_video_ids(channel_id: str) -> list[str]:
    """チャンネルのショート・動画タブの HTML から動画IDを新しい順に取り出す。"""
    ids: list[str] = []
    for tab in ("shorts", "videos"):
        try:
            html = _get(f"https://www.youtube.com/channel/{channel_id}/{tab}").decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning("チャンネルの %s タブを取得できませんでした: %s", tab, e)
            continue
        for vid in re.findall(r'"videoId":"([\w-]{11})"', html):
            if vid not in ids:
                ids.append(vid)
    return ids[:30]


def fetch_videos(channel_id: str, log: dict) -> list[dict]:
    """新着動画の一覧を取る。RSS(チャンネル → アップロード再生リスト)が 404 などで使えないときは、
    チャンネルページから動画IDを取り出し、未記録の動画だけ oEmbed でタイトルを補う。"""
    feeds = [
        f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
        f"https://www.youtube.com/feeds/videos.xml?playlist_id=UU{channel_id[2:]}",
    ]
    for url in feeds:
        try:
            videos = parse_feed(_get(url))
            logger.info("RSS から %d 本の動画を取得しました: %s", len(videos), url)
            return videos
        except Exception as e:
            logger.warning("RSS を取得できませんでした(%s): %s", url, e)

    ids = scrape_video_ids(channel_id)
    if not ids:
        raise RuntimeError(f"動画一覧を取得できませんでした(チャンネルID: {channel_id})。ID が正しいか確認してください")
    logger.info("チャンネルページから %d 本の動画IDを取得しました", len(ids))
    videos = []
    for vid in ids:
        info = _oembed(vid) if (vid not in log and log.get("_initialized")) else {"title": "", "thumbnail": ""}
        videos.append(
            {
                "id": vid,
                "title": info["title"],
                "published": "",
                "description": "",
                "thumbnail": info["thumbnail"] or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            }
        )
    return list(reversed(videos))  # 古い順(published がないため取得順の逆で代用)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def video_url(video_id: str) -> str:
    """ショート動画なら /shorts/ の URL、通常動画なら /watch の URL を返す。
    /shorts/<id> は通常動画だとリダイレクトされるので、それで見分ける。"""
    shorts = f"https://www.youtube.com/shorts/{video_id}"
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        req = urllib.request.Request(shorts, headers={"User-Agent": USER_AGENT}, method="HEAD")
        with opener.open(req, timeout=15) as res:
            if res.status == 200:
                return shorts
    except urllib.error.HTTPError:
        pass
    except Exception as e:
        logger.warning("ショート判定に失敗したため /shorts/ の URL を使います: %s", e)
        return shorts
    return f"https://www.youtube.com/watch?v={video_id}"


def load_log() -> dict:
    return json.loads(LOG_FILE.read_text(encoding="utf-8")) if LOG_FILE.exists() else {}


def save_log(log: dict) -> None:
    LOG_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def select_new_videos(videos: list[dict], log: dict) -> list[dict]:
    """RSS に初めて現れた動画を新着とみなす。
    予約公開の動画は公開されるまで RSS に載らず、載ったときの published がアップロード日時のことも
    あるため、日付では絞り込まない(過去動画の一斉告知は初回登録で防いでいる)。"""
    new = [v for v in videos if v["id"] and v["id"] not in log]
    if all(v["published"] for v in new):
        new.sort(key=lambda v: v["published"])  # 古い順に告知する
    return new[-MAX_VIDEOS_PER_RUN:]


def _strip_hashtags(text: str) -> str:
    return re.sub(r"\s*#\S+", "", text).strip()


def with_hashtags(text: str, hashtags: list[str], limit: int) -> str:
    """本文のハッシュタグを外し、設定のハッシュタグを末尾に付ける。上限を超える分は本文を切り詰める。"""
    tags = " ".join(hashtags)
    body = _strip_hashtags(text)
    room = limit - len(tags) - 1 if tags else limit
    if len(body) > room:
        body = body[: room - 1] + "…"
    return f"{body} {tags}".strip()


def write_video_texts(video: dict, channel_name: str) -> sp.SocialTexts:
    prompt = f"""YouTube チャンネル「{channel_name}」(社会や身近なものの「裏側」がわかる雑学ショート動画)に
投稿された新しい動画を SNS で紹介する文章を2つ書いてください。

動画タイトル: {video['title']}
動画の説明文:
{video['description'][:1500] or '(説明文なし。タイトルだけを手がかりにし、内容を推測で作らないこと)'}

- 書き方: タイトルの内容を「〜って、なぜ?」「〜の本当の理由、知っていますか?」のような問いかけにして、
  答えが気になって動画を見たくなる文章にする。答え(オチ)は書かない
- bluesky: 60〜120字。問いかけ+動画でわかることを一言
- x: 60字以内。問いかけ中心で短く
- どちらも、ハッシュタグ・URL・チャンネル名・「新着動画公開中」のような定型句は書かない(ハッシュタグは後から自動で付ける)
- 誇張表現(「必ず」「最強」など)や絵文字の多用は避け、説明文やタイトルにない事実を作らない
"""
    return sp.generate_texts(prompt)


def main() -> int:
    config = json.loads(sp.CONFIG_FILE.read_text(encoding="utf-8"))
    yt = config.get("youtube") or {}
    if not (yt.get("handle") or yt.get("channel_id")):
        logger.info("config.json に youtube の設定がないため終了します")
        return 0

    log = load_log()
    # 一度調べたチャンネルIDは記録して使い回す(毎回チャンネルページを読みに行かない)
    channel_id = yt.get("channel_id") or log.get("_channel_id") or resolve_channel_id(yt)
    logger.info("チャンネルID: %s", channel_id)
    videos = fetch_videos(channel_id, log)
    log["_channel_id"] = channel_id  # 動画一覧が取れた ID だけ記録する
    now = datetime.now(JST).isoformat(timespec="seconds")

    if not log.get("_initialized"):
        # 初回: 既存動画は告知せず記録だけする
        log["_initialized"] = now
        for v in videos:
            log[v["id"]] = {"title": v["title"], "announced_at": None, "note": "初回登録(告知なし)"}
        save_log(log)
        logger.info("初回のため既存動画 %d 本を記録しました(告知はしません)", len(videos))
        return 0

    new_videos = select_new_videos(videos, log)
    if not new_videos:
        logger.info("告知する新着動画はありません")
        save_log(log)
        return 0

    use_bluesky = bool(os.environ.get("BLUESKY_HANDLE") and os.environ.get("BLUESKY_APP_PASSWORD"))
    channel_name = yt.get("name") or "YouTube チャンネル"
    hashtags = yt.get("hashtags") or []
    items = []
    for v in new_videos:
        url = video_url(v["id"])
        try:
            texts = write_video_texts(v, channel_name)
        except Exception as e:
            logger.warning("紹介文の生成に失敗したため定型文を使います: %s", e)
            texts = sp.SocialTexts(bluesky=f"{_strip_hashtags(v['title'])}、その理由とは?", x=f"{_strip_hashtags(v['title'])}、その理由とは?")
        texts.x = with_hashtags(texts.x, hashtags, sp.X_TEXT_CHARS)

        bluesky_status = "未設定のためスキップ"
        if use_bluesky:
            try:
                record = sp.build_bluesky_record(texts.bluesky, url, v["title"], v["description"])
                uri = sp.post_to_bluesky(record, thumb_url=v["thumbnail"])
                bluesky_status = "投稿済み"
                logger.info("Bluesky に投稿しました: %s", uri)
            except Exception as e:
                bluesky_status = f"投稿失敗({e})"
                logger.error("Bluesky への投稿に失敗しました: %s", e)

        items.append(
            {
                "id": v["id"],
                "title": v["title"],
                "url": url,
                "x_text": texts.x,
                "intent": sp.x_intent_url(texts.x, url),
                "bluesky_status": bluesky_status,
            }
        )

    body = sp.build_issue_body(items, kind="動画")
    issue_url = ""
    if os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPOSITORY"):
        try:
            issue_url = sp.create_github_issue(
                f"[X投稿] {datetime.now(JST).strftime('%Y-%m-%d %H:%M')} YouTube 新着動画 {len(items)}件", body
            )
            logger.info("X 用の下書きを Issue にしました: %s", issue_url)
        except Exception as e:
            logger.error("Issue の作成に失敗しました: %s", e)
    else:
        print(body)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(body + "\n")

    for item in items:
        log[item["id"]] = {"title": item["title"], "announced_at": now, "bluesky": item["bluesky_status"], "x_issue": issue_url}
    save_log(log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
