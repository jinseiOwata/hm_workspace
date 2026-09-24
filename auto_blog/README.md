# AI時短ラボ — 全自動更新ブログ

GitHub Actions が毎朝 Claude API で記事を1本書き、GitHub Pages に自動公開するブログです。
収益源は **Google AdSense(広告)** と **アフィリエイトリンク** です。サーバー代はかからず、
かかる費用は Claude API の利用料だけです。

```
[毎日 06:17 JST] GitHub Actions (.github/workflows/auto-blog.yml)
   ↓ generate_article.py  … topics.txt から1テーマ取り出し → Claude(Web検索付き)が記事を執筆
   ↓                        → posts/YYYY-MM-DD-slug.md をリポジトリに commit
   ↓ build_site.py        … posts/*.md → _site/(HTML, sitemap.xml, RSS, robots.txt, ads.txt)
   ↓ deploy-pages         … GitHub Pages に公開
```

テーマ(`topics.txt`)を使い切ると、Claude が既存記事と重複しない新テーマを20件ずつ自動補充します。

## 立ち上げ手順(初回だけ・約10分)

1. **このブランチを `main` にマージする**(スケジュール実行はデフォルトブランチでしか動きません)
2. **APIキーを登録**: リポジトリの Settings → Secrets and variables → Actions → New repository secret
   - Name: `ANTHROPIC_API_KEY` / Value: Anthropic Console で発行したキー
3. **Pages を有効化**: Settings → Pages → Build and deployment → Source を **GitHub Actions** に変更
   - ⚠️ 非公開(private)リポジトリで Pages を使うには GitHub Pro 以上が必要です。
     無料プランの場合はリポジトリを public にするか、ブログ用の public リポジトリに `auto_blog/` と workflow を移してください
     (このリポジトリには株の自動売買コードも入っているので、**public にするならブログ部分だけを別リポジトリへ移す**のがおすすめです)
4. **初回実行**: Actions タブ → `auto-blog` → Run workflow。数分で1記事目が公開されます
5. 公開URL(`https://<ユーザー名>.github.io/<リポジトリ名>/`)を [Google Search Console](https://search.google.com/search-console) に登録し、`sitemap.xml` を送信
   - 所有権確認の HTML タグの `content` の値を `config.json` の `google_site_verification` に入れれば確認できます

以降は何もしなくても毎日1記事ずつ増えていきます。

## 収益化の設定

| やること | 設定場所 |
|---|---|
| アフィリエイト(A8.net / もしもアフィリエイト / Amazonアソシエイト等)に登録し、提携した広告リンクを入れる | `config.json` の `affiliates[].url`。`keywords` のどれかを本文に含む記事の末尾に自動で表示されます |
| 記事が20〜30本たまったら Google AdSense に申請 | 合格後、`config.json` の `adsense_client_id` に `ca-pub-xxxxxxxx` を入れる(自動広告タグと `ads.txt` が出力されます) |
| サイト名・コンセプト・ジャンルを変える | `config.json` と `topics.txt`、`generate_article.py` の `ARTICLE_SYSTEM` |

AdSense 審査で必要な「運営者情報」「プライバシーポリシー」ページ、ステマ規制(2023年10月施行)対応の
「プロモーションを含みます」表記、アフィリエイトリンクの `rel="sponsored"` は自動で付きます。

## コストと設定

- モデルは既定で `claude-opus-5`。1記事あたり50〜100円程度、月1,500〜3,000円程度が目安です
  (Web検索の回数や記事の長さで変動するので、Anthropic Console の Usage で実額を確認してください)
- 安くしたい場合: Settings → Secrets and variables → Actions → **Variables** に `BLOG_MODEL` = `claude-sonnet-5` を追加(費用はおよそ4割に)
- 投稿時刻は workflow の `cron`(UTC 表記)で変更できます

## 手元での動作確認

```bash
cd auto_blog
pip install -r requirements.txt
ANTHROPIC_API_KEY=... python generate_article.py   # 記事を1本生成(本日分があればスキップ。BLOG_FORCE=1 で追加生成)
python build_site.py && python -m http.server -d _site 8000   # http://localhost:8000 で確認
```

## 正直な注意点

- **すぐには稼げません。** 新規ブログが検索流入を得るまで通常3〜6か月かかり、収益は記事数と流入に比例します
- Google は「検索順位操作を主目的とした大量の低品質な自動生成コンテンツ」をスパムとして扱います。
  AI生成であること自体は問題とされていませんが、**読者の役に立つ内容かどうか**が評価されます。
  ときどき記事を読んで、誤りの修正や自分の体験の追記(posts/*.md を直接編集して push)をすると評価と信頼性が上がります
- 記事内容の正確性は保証されません。明らかな誤りに気付いたら修正してください
