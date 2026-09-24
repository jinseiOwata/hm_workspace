"""
静的サイトビルダー: posts/*.md → _site/ (GitHub Pages で公開)

生成物:
    index.html / 各記事ページ / tags ページ / about・privacy(AdSense審査で必須) /
    sitemap.xml / robots.txt / feed.xml(RSS) / ads.txt(AdSense設定時) / style.css

環境変数:
    SITE_BASE_URL  公開URL(例: https://<user>.github.io/<repo>)。config.json の base_url より優先
"""

from __future__ import annotations

import html
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import markdown

from generate_article import parse_post

BASE_DIR = Path(__file__).resolve().parent
POSTS_DIR = BASE_DIR / "posts"
STATIC_DIR = BASE_DIR / "static"
OUT_DIR = BASE_DIR / "_site"


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


class Site:
    def __init__(self, config: dict):
        self.c = config
        self.base_url = (os.environ.get("SITE_BASE_URL") or config.get("base_url") or "").rstrip("/")

    def url(self, path: str) -> str:
        """サイト内リンク。サブパス(/<repo>/)配下でも動くよう base_url のパス部分を付ける。"""
        prefix = urlparse(self.base_url).path.rstrip("/")
        return f"{prefix}/{path.lstrip('/')}"

    def abs_url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def page(self, *, title: str, description: str, body: str, path: str, og_type: str = "website") -> str:
        c = self.c
        full_title = title if title == c["site_name"] else f"{title} | {c['site_name']}"
        adsense = (
            f'<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={esc(c["adsense_client_id"])}" crossorigin="anonymous"></script>'
            if c.get("adsense_client_id")
            else ""
        )
        verification = (
            f'<meta name="google-site-verification" content="{esc(c["google_site_verification"])}">'
            if c.get("google_site_verification")
            else ""
        )
        canonical = f'<link rel="canonical" href="{esc(self.abs_url(path))}">' if self.base_url else ""
        year = datetime.now().year
        return f"""<!doctype html>
<html lang="{esc(c.get('language', 'ja'))}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(full_title)}</title>
<meta name="description" content="{esc(description)}">
<meta property="og:title" content="{esc(full_title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:type" content="{og_type}">
<meta property="og:site_name" content="{esc(c['site_name'])}">
<meta name="twitter:card" content="summary">
{canonical}
{verification}
<link rel="alternate" type="application/rss+xml" title="{esc(c['site_name'])}" href="{self.url('feed.xml')}">
<link rel="stylesheet" href="{self.url('style.css')}">
{adsense}
</head>
<body>
<header class="site-header"><div class="wrap">
  <a class="brand" href="{self.url('')}">{esc(c['site_name'])}</a>
  <span class="tagline">{esc(c['tagline'])}</span>
</div></header>
<main class="wrap">
{body}
</main>
<footer class="site-footer"><div class="wrap">
  <nav><a href="{self.url('about.html')}">運営者情報</a> · <a href="{self.url('privacy.html')}">プライバシーポリシー・免責事項</a> · <a href="{self.url('feed.xml')}">RSS</a></nav>
  <p>&copy; {year} {esc(c['site_name'])}</p>
</div></footer>
</body>
</html>
"""


def load_posts() -> list[dict]:
    posts = []
    for path in sorted(POSTS_DIR.glob("*.md"), reverse=True):
        meta, body = parse_post(path.read_text(encoding="utf-8"))
        if not meta.get("title"):
            continue
        meta["file"] = path.stem + ".html"
        meta["tags_list"] = [t.strip() for t in meta.get("tags", "").split(",") if t.strip()]
        meta["html"] = markdown.markdown(body, extensions=["fenced_code", "tables", "toc", "sane_lists"])
        meta["text"] = body
        posts.append(meta)
    return posts


def affiliate_box(post: dict, config: dict) -> str:
    """記事本文にキーワードが含まれる提携済み(url設定済み)の広告だけを表示する。"""
    items = []
    for a in config.get("affiliates", []):
        if a.get("url") and any(k in post["text"] or k in post["title"] for k in a.get("keywords", [])):
            items.append(
                f'<li><a href="{esc(a["url"])}" rel="sponsored nofollow noopener" target="_blank">{esc(a["label"])}</a></li>'
            )
    if not items:
        return ""
    return f'<aside class="affiliate"><h2>この記事に関連するおすすめ</h2><ul>{"".join(items)}</ul></aside>'


def tag_slug(tag: str) -> str:
    return "tag-" + "".join(f"{ord(ch):x}" if not ch.isascii() or not ch.isalnum() else ch.lower() for ch in tag)


def post_card(site: Site, p: dict) -> str:
    return f"""<article class="card">
  <a href="{site.url(p['file'])}"><h2>{esc(p['title'])}</h2></a>
  <p class="meta">{esc(p['date'])}</p>
  <p>{esc(p.get('description', ''))}</p>
</article>"""


def build() -> None:
    config = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    site = Site(config)
    posts = load_posts()

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir()
    shutil.copy(STATIC_DIR / "style.css", OUT_DIR / "style.css")

    # 記事ページ
    for i, p in enumerate(posts):
        tags = " ".join(f'<a class="tag" href="{site.url(tag_slug(t) + ".html")}">#{esc(t)}</a>' for t in p["tags_list"])
        related = [q for q in posts if q is not p and set(q["tags_list"]) & set(p["tags_list"])][:5]
        related_html = (
            "<section class=\"related\"><h2>関連記事</h2><ul>"
            + "".join(f'<li><a href="{site.url(q["file"])}">{esc(q["title"])}</a></li>' for q in related)
            + "</ul></section>"
            if related
            else ""
        )
        body = f"""<article class="post">
  <p class="pr">※本記事にはプロモーション(広告)が含まれる場合があります。</p>
  <h1>{esc(p['title'])}</h1>
  <p class="meta">{esc(p['date'])} · {tags}</p>
  {p['html']}
  {affiliate_box(p, config)}
  <p class="ai-note">この記事は生成AIを活用して作成し、公開しています。料金や仕様は変更されることがあるため、最新情報は各サービスの公式サイトをご確認ください。</p>
</article>
{related_html}"""
        (OUT_DIR / p["file"]).write_text(
            site.page(title=p["title"], description=p.get("description", ""), body=body, path=p["file"], og_type="article"),
            encoding="utf-8",
        )

    # トップページ
    cards = "\n".join(post_card(site, p) for p in posts) or "<p>記事を準備中です。</p>"
    intro = f'<section class="intro"><p>{esc(config["description"])}</p></section>'
    (OUT_DIR / "index.html").write_text(
        site.page(title=config["site_name"], description=config["description"], body=intro + cards, path=""),
        encoding="utf-8",
    )

    # タグページ
    tags: dict[str, list[dict]] = {}
    for p in posts:
        for t in p["tags_list"]:
            tags.setdefault(t, []).append(p)
    for t, tp in tags.items():
        body = f"<h1>#{esc(t)} の記事</h1>" + "\n".join(post_card(site, p) for p in tp)
        (OUT_DIR / f"{tag_slug(t)}.html").write_text(
            site.page(title=f"#{t} の記事一覧", description=f"{t}に関する記事一覧", body=body, path=f"{tag_slug(t)}.html"),
            encoding="utf-8",
        )

    # 運営者情報・プライバシーポリシー(AdSense審査・アフィリエイト審査で求められる)
    about = f"""<article class="post"><h1>運営者情報</h1>
<p>サイト名: {esc(config['site_name'])}</p><p>運営: {esc(config['author'])}</p>
<p>{esc(config['description'])}</p>
<p>当サイトの記事は生成AIを活用して作成しています。内容の誤りにお気づきの際はお知らせください。</p></article>"""
    privacy = f"""<article class="post"><h1>プライバシーポリシー・免責事項</h1>
<h2>広告について</h2>
<p>当サイトは第三者配信の広告サービス(Google AdSense 等)およびアフィリエイトプログラムを利用する場合があります。広告配信事業者は、ユーザーの興味に応じた広告を表示するために Cookie を使用することがあります。Cookie を無効にする方法や Google AdSense に関する詳細は「<a href="https://policies.google.com/technologies/ads?hl=ja" rel="nofollow noopener">広告 – ポリシーと規約 – Google</a>」をご確認ください。</p>
<h2>アクセス解析について</h2>
<p>当サイトはアクセス解析ツールを利用する場合があります。これらはトラフィックデータ収集のために Cookie を使用しますが、個人を特定するものではありません。</p>
<h2>免責事項</h2>
<p>当サイトの情報は正確性に努めていますが、生成AIを活用して作成しているため誤りを含む可能性があります。掲載情報の利用により生じた損害について、当サイトは責任を負いかねます。各サービスの最新情報は公式サイトをご確認ください。</p>
<h2>著作権</h2>
<p>当サイトの文章の無断転載を禁止します。</p></article>"""
    for name, title, body in [("about.html", "運営者情報", about), ("privacy.html", "プライバシーポリシー・免責事項", privacy)]:
        (OUT_DIR / name).write_text(site.page(title=title, description=title, body=body, path=name), encoding="utf-8")

    # SEO 関連ファイル
    if site.base_url:
        urls = [""] + [p["file"] for p in posts] + [f"{tag_slug(t)}.html" for t in tags] + ["about.html", "privacy.html"]
        lastmod = {p["file"]: p["date"] for p in posts}
        entries = "".join(
            f"<url><loc>{esc(site.abs_url(u))}</loc>{f'<lastmod>{lastmod[u]}</lastmod>' if u in lastmod else ''}</url>"
            for u in urls
        )
        (OUT_DIR / "sitemap.xml").write_text(
            f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>',
            encoding="utf-8",
        )
    robots = "User-agent: *\nAllow: /\n" + (f"Sitemap: {site.abs_url('sitemap.xml')}\n" if site.base_url else "")
    (OUT_DIR / "robots.txt").write_text(robots, encoding="utf-8")

    items = "".join(
        f"<item><title>{esc(p['title'])}</title><link>{esc(site.abs_url(p['file']))}</link>"
        f"<description>{esc(p.get('description', ''))}</description>"
        f"<pubDate>{datetime.strptime(p['date'], '%Y-%m-%d').strftime('%a, %d %b %Y 00:00:00 +0900')}</pubDate>"
        f"<guid>{esc(site.abs_url(p['file']))}</guid></item>"
        for p in posts[:20]
    )
    (OUT_DIR / "feed.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{esc(config["site_name"])}</title>'
        f'<link>{esc(site.abs_url(""))}</link><description>{esc(config["description"])}</description>{items}</channel></rss>',
        encoding="utf-8",
    )

    if config.get("adsense_client_id"):
        pub = config["adsense_client_id"].replace("ca-", "")
        (OUT_DIR / "ads.txt").write_text(f"google.com, {pub}, DIRECT, f08c47fec0942fa0\n", encoding="utf-8")

    print(f"built {len(posts)} posts, {len(tags)} tags -> {OUT_DIR}")


if __name__ == "__main__":
    build()
