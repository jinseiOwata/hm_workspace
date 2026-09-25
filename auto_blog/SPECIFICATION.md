# システム仕様書: 全自動更新ブログ「AI時短ラボ」

最終更新: 2026-09-25(PR #7 マージ・YouTube 告知の初回登録・Bluesky プロフィール設定後の状態)

使い方・立ち上げ手順は `README.md`、この文書は「今どういう仕様で動いているか」の記録です。
仕様を変えたときはこの文書も合わせて更新すること。

## 目的と収益モデル

- 生成AI・業務効率化の実践ガイド記事を、毎日1本 Claude API で自動生成して公開する
- ブログ記事と、YouTube チャンネルのショート動画(社会の裏側がわかる雑学)の新着を SNS で自動告知し、流入を増やす
- 収益源: Google AdSense(広告)+ アフィリエイトリンク
- 運用コスト: Claude API 利用料のみ(サーバー代なし。GitHub Actions / GitHub Pages を利用)

## 全体アーキテクチャ

```
[毎日 21:17 UTC = 06:17 JST]  GitHub Actions (.github/workflows/auto-blog.yml)
   │
   ├─ build ジョブ
   │   ① generate_article.py
   │        topics.txt 先頭のテーマを取り出す(空なら Claude に20件補充させる)
   │        → Claude API(Web検索付き)で記事を執筆
   │        → posts/YYYY-MM-DD-<slug>.md に保存、topics.txt から消費済みテーマを削除
   │   ② 生成物を main に commit & push(bot名義: auto-blog-bot)
   │   ③ build_site.py … posts/*.md → _site/ に静的サイトを出力
   │   ④ upload-pages-artifact … _site/ をアップロード
   │
   ├─ deploy ジョブ … GitHub Pages に公開
   │
   └─ announce ジョブ(記事を生成した実行のときだけ)
       ⑤ social_post.py … 未告知の新着記事を探し、Claude が紹介文を作成
            → Bluesky に自動投稿 / X 用の下書きを GitHub Issue にする
            → social_log.json に記録して main に commit & push
```

公開URL: https://jinseiowata.github.io/hm_workspace/

**YouTube 新着動画の告知**(別ワークフロー)

```
[3時間おき 毎時23分(UTC 0,3,6,…時)]  GitHub Actions (.github/workflows/youtube-announce.yml)
   youtube_announce.py … チャンネルの RSS を取得 → 未告知の新着動画を探す
       → Claude が紹介文を作成 → Bluesky に自動投稿(サムネイル付き)/ X 用の下書きを GitHub Issue にする
       → youtube_log.json に記録して main に commit & push
```

## ファイル構成

| パス | 役割 |
|---|---|
| `.github/workflows/auto-blog.yml` | 毎日の自動実行・手動実行・デプロイの定義 |
| `auto_blog/generate_article.py` | テーマ選択・Claude API 呼び出し・記事ファイル保存 |
| `auto_blog/build_site.py` | 静的サイト生成(HTML / sitemap / RSS / robots / ads.txt) |
| `auto_blog/social_post.py` | 新着記事の告知(Bluesky 自動投稿、X 用下書きの Issue 作成) |
| `auto_blog/social_log.json` | 告知済み記事の記録(初回の告知時に自動作成) |
| `auto_blog/youtube_announce.py` | YouTube 新着動画の告知(紹介文作成・投稿部分は social_post.py を共用) |
| `auto_blog/youtube_log.json` | 記録済み・告知済みの動画とチャンネルIDの記録(初回実行時に自動作成) |
| `.github/workflows/youtube-announce.yml` | 3時間おきの YouTube 新着チェック |
| `auto_blog/config.json` | サイト名・説明・AdSense ID・Search Console 確認コード・アフィリエイト設定 |
| `auto_blog/topics.txt` | 記事テーマのキュー(1行1テーマ、`#` 行はコメント) |
| `auto_blog/posts/` | 生成された記事(Markdown + front matter)。リポジトリに蓄積される |
| `auto_blog/static/style.css` | サイトのスタイル(ライト/ダーク対応、スマホ幅対応) |
| `auto_blog/requirements.txt` | 依存ライブラリ(`anthropic>=1.8.0`, `markdown>=3.5`) |
| `auto_blog/README.md` | 立ち上げ手順・収益化設定・費用・注意点 |

株シグナル関連のファイル(リポジトリ直下)とは独立しており、相互の依存はない。

## コンポーネント詳細

### ① 記事生成 `generate_article.py`

**実行条件**
- JST の本日日付の記事(`posts/YYYY-MM-DD-*.md`)が既にあればスキップ(二重生成防止)
- 環境変数 `BLOG_FORCE=1` のときはスキップせず追加生成(ファイル名に `-2`, `-3` … を付与)

**テーマ選択**
- `topics.txt` の先頭(コメント・空行を除く)を使用し、生成成功後にその行を削除
- 空の場合は Claude に既存記事タイトル(直近200件)と重複しない新テーマを20件生成させて補充
  (Web検索なし、`max_tokens=8000`)

**Claude API の呼び出し設定**

| 項目 | 値 |
|---|---|
| モデル | 既定 `claude-sonnet-5`。環境変数 `BLOG_MODEL` で上書き(Actions の Variables から渡す) |
| 呼び出し方式 | `client.beta.messages.stream(...)` → `get_final_message()` |
| thinking | `{"type": "adaptive"}` |
| effort | `high` |
| max_tokens | 32000(記事)/ 8000(テーマ補充) |
| Web検索 | `web_search_20260209`、1記事あたり最大3回。`BLOG_WEB_SEARCH=0` で無効化 |
| 拒否時のフォールバック | モデルが `claude-opus-5*` / `claude-fable-5*` のときだけ `fallbacks: "default"`(beta `server-side-fallback-2026-07-01`)を付与。Sonnet では付与しない |
| pause_turn | Web検索の途中停止時は応答を履歴に足して再開(最大5回) |
| エラー扱い | `refusal`(拒否)・`max_tokens`(打ち切り)・pause_turn 上限超過は例外で終了 → その日の記事は出ない |

**記事の執筆ルール(システムプロンプト `ARTICLE_SYSTEM`)**
- 読者がその場で真似できる具体的な手順、コピペできるプロンプト例をコードブロックで示す
- 料金・仕様など変わりやすい事実は Web検索で確認してから書き、確認できない数値は書かない
- 誇張・効果保証をしない。個人情報・機密情報をAIに入れる際の注意に必ず触れる
- 見出しは `##` / `###`(`#` は使わない)。冒頭に「この記事でわかること」、最後に「まとめ」
- **分量: 本文は全角6,000〜8,000字、最大9,000字。`##` 見出しは6〜8個、プロンプト例は3〜5個**

**出力形式と検証**
- Claude には次の形式で出力させ、`===` で頭部と本文を分割して解析する
  ```
  TITLE: …(32字前後)
  DESCRIPTION: …(100〜120字)
  SLUG: …(英小文字とハイフン)
  TAGS: …(カンマ区切り3〜5個)
  ===
  (Markdown 本文)
  ```
- `===` がない、TITLE がない、本文が1,500字未満 → 例外で終了(記事は保存しない)
- SLUG は英小文字・数字・ハイフン以外をハイフンに置換し60字で切る(空なら `post`)
- 本文が 11,000字(Markdown記号込み)を超えたら警告ログを出す(記事は保存・公開する)

### 記事ファイルの形式 `posts/YYYY-MM-DD-<slug>.md`

```
---
title: 記事タイトル
description: 検索結果用の説明文
slug: url-slug
tags: タグ1, タグ2, タグ3
date: 2026-09-25
topic: topics.txt から取り出した元テーマ
---
(Markdown 本文)
```

日付は JST。記事を手で直したいときはこのファイルを編集して main に push すれば、次のビルドで反映される。

### ② 静的サイト生成 `build_site.py`

**出力(`auto_blog/_site/`、リポジトリには含めない)**

| ファイル | 内容 |
|---|---|
| `index.html` | サイト説明+全記事の一覧(新しい順) |
| `<記事ファイル名>.html` | 記事ページ。PR表記、タグ、本文(途中にアフィリエイト枠)、末尾のアフィリエイト枠、AI生成の注記、関連記事(タグ一致・最大5件) |
| `tag-<名前>.html` | タグ別一覧。日本語タグは文字コードの16進表記でファイル名にする |
| `about.html` / `privacy.html` | 運営者情報 / プライバシーポリシー・免責事項(AdSense 審査対策) |
| `sitemap.xml` | 全ページ(記事は lastmod 付き)。公開URLが分かるときのみ出力 |
| `robots.txt` | 全許可+sitemap の場所 |
| `feed.xml` | RSS 2.0(新しい順に最大20件) |
| `ads.txt` | `config.json` に `adsense_client_id` があるときのみ出力 |
| `style.css` | `static/style.css` のコピー |

**URL の扱い**: 環境変数 `SITE_BASE_URL`(Actions では `configure-pages` の出力を使用)からパス部分を取り出し、
サイト内リンクに付与する(`/hm_workspace/…` のようなサブパス配下でも動く)。canonical・sitemap・RSS は絶対URL。

**アフィリエイト枠(1記事2か所)**
- 対象: `config.json` の `affiliates[]` のうち `html` か `url` が設定済みで、`keywords` のどれかが記事タイトルか本文に含まれるもの(設定順 = 優先順)
- 表示内容: `html`(ASP の広告コード)があれば **一切改変せずそのまま** 出す(A8.net の規約でコードの改変が禁止されているため。エスケープも属性の追加もしない)。
  `html` がなければ `url` と `label` からテキストリンク(`rel="sponsored nofollow noopener"`)を作る
- `slots`: その広告を出す枠(`"mid"` / `"end"`)。省略時は両方。大きいバナーは `["end"]` にする
- 記事途中: 本文の2つ目の `<h2>` 見出しの直前に、途中枠の対象の先頭2件を「PR」ラベル付きの小さい枠で表示。見出しが2つない記事には入れない
- 記事末尾: 「この記事に関連するおすすめ」として末尾枠の対象をすべて表示
- 対象がない記事(提携リンク未設定の現状を含む)には、どちらの枠も表示しない

**Markdown 変換**: `markdown` ライブラリ(拡張: fenced_code, tables, toc, sane_lists)

**法令・審査対応として自動で付くもの**
- 全記事の冒頭に「※本記事にはプロモーション(広告)が含まれる場合があります。」(ステマ規制対応)
- 全記事の末尾に「生成AIを活用して作成」「最新情報は公式サイトで確認を」の注記
- アフィリエイトリンクに `rel="sponsored nofollow noopener"`

### ③ SNS 告知 `social_post.py`

**対象記事**: `social_log.json` に記録がなく、日付が直近3日以内の記事(1回の実行で最大3件)。
記録がなくても古い記事はまとめて告知しない。

**紹介文の生成**: `claude-sonnet-5`(環境変数 `SOCIAL_MODEL` で変更可)に `messages.parse` で
`{"bluesky": ..., "x": ...}` を構造化出力させる。記事タイトル・説明・本文冒頭2,000字を渡す。
- bluesky: 180字以内、ハッシュタグなし
- x: 90字以内、末尾にハッシュタグ1〜2個
- 生成に失敗したときは「新着記事: <タイトル>」の定型文で続行する

**Bluesky への投稿**(`BLUESKY_HANDLE` と `BLUESKY_APP_PASSWORD` の両方があるときのみ)
- `com.atproto.server.createSession` でログイン → `com.atproto.repo.createRecord` で投稿
- 本文は「紹介文+改行+記事URL」。全体が300字を超える場合は紹介文を切り詰める
- URL はリンク facet(UTF-8 バイト位置で指定)を付けてクリック可能にし、リンクカード(`app.bsky.embed.external`)も付ける

**X 用の下書き**
- X API は有料(2026年時点でURL付き投稿1件あたり約$0.20)のため自動投稿はせず、
  `https://x.com/intent/post?text=…&url=…` のリンクを作る。タップすると投稿画面が開く
- 紹介文は110字で切り詰める
- 新着記事ごとの紹介文・記事URL・投稿リンク・Bluesky の結果を1件の GitHub Issue にまとめて作成する
  (タイトル: `[X投稿] YYYY-MM-DD の新着記事 N件`)。同じ内容を Actions の実行サマリーにも出す

**Bluesky のサムネイル**: `post_to_bluesky(record, thumb_url=...)` に画像URLを渡すと、
`com.atproto.repo.uploadBlob` でアップロードしてリンクカードに付ける(動画の告知で使用。失敗してもサムネイルなしで投稿を続ける)。

**記録と失敗時の扱い**
- 告知した記事は `social_log.json` に `announced_at`・Bluesky の結果・Issue URL を記録する
- Bluesky 投稿や Issue 作成が失敗しても、その記事は「告知済み」として記録される(再試行はしない)
- 告知ジョブの失敗は記事の公開に影響しない

### ③-2 YouTube 新着動画の告知 `youtube_announce.py`

**チャンネルの特定**: `config.json` の `youtube.channel_id` → `youtube_log.json` の `_channel_id`(前回調べた値)→
チャンネルページ(`youtube.com/@<handle>`)の HTML から `UC…` の ID を抽出、の順で決める。調べた ID は記録して使い回す。

**新着の取得**(上から順に試し、取れたものを使う。どれも APIキー不要)
1. チャンネルの RSS: `https://www.youtube.com/feeds/videos.xml?channel_id=<ID>`(直近15本程度)
2. アップロード再生リストの RSS: `…/feeds/videos.xml?playlist_id=UU<IDの3文字目以降>`
3. チャンネルの「ショート」「動画」タブの HTML から動画IDを抽出(最大30件)。未記録の動画だけ oEmbed
   (`youtube.com/oembed`)でタイトルとサムネイルを補う。説明文と公開日時は取れないため、紹介文はタイトルだけから作る
- 実行ログに使ったチャンネルIDと取得方法を出す。動画一覧が取れたときだけチャンネルIDを記録する

**告知対象**
- 初回(`youtube_log.json` に `_initialized` がない)は、既存動画をすべて「告知なし」で記録して終了する
- 2回目以降は、記録にない動画(= RSS に初めて現れた動画)を古い順に最大3本。公開日時では絞り込まない
  (予約公開の動画は公開まで RSS に載らず、載ったときの公開日時がアップロード日時になっている場合があるため)
- URL: `youtube.com/shorts/<id>` に HEAD リクエストし、200 ならショート動画としてその URL、リダイレクトされたら通常動画として `watch?v=<id>` を使う

**紹介文**: `social_post.generate_texts` を共用。チャンネル名(`youtube.name`)、動画タイトル、説明文(先頭1,500字)を渡す。
- 書き方: タイトルの内容を「〜って、なぜ?」のような問いかけにし、答え(オチ)は書かない
- bluesky は60〜120字、x は60字以内。ハッシュタグ・URL・チャンネル名・「新着動画公開中」のような定型句は書かせない
- x の文末には `youtube.hashtags`(既定 `#雑学 #大人の雑学 #社会の裏側`)をコードで付ける。モデルがハッシュタグを付けても外して付け直し、110字に収める
- タイトルや説明文にない事実は作らせない
- 失敗時は「<タイトル>、その理由とは?」の定型文(x は同じくハッシュタグ付き)で続行

**投稿と記録**: Bluesky(サムネイル付きリンクカード)と X 用下書きの Issue(タイトル `[X投稿] YYYY-MM-DD HH:MM YouTube 新着動画 N件`)は
記事の告知と同じ。告知した動画を `youtube_log.json` に記録する(失敗しても記録し、再試行はしない)。

### ④ 設定ファイル `config.json`

| キー | 用途 | 現在値 |
|---|---|---|
| `site_name` / `tagline` / `description` / `author` | サイト名・キャッチコピー・説明・運営者名 | AI時短ラボ ほか |
| `base_url` | 公開URL(Actions では `SITE_BASE_URL` が優先されるので空でよい) | 空 |
| `adsense_client_id` | AdSense のパブリッシャーID(`ca-pub-…`)。入れると広告タグと ads.txt を出力 | 未設定 |
| `google_site_verification` | Search Console の所有権確認コード | 未設定 |
| `youtube.handle` / `youtube.channel_id` / `youtube.name` | 告知する YouTube チャンネル。`channel_id` があればハンドルより優先。`name` は紹介文を書かせるときに渡すチャンネル名 | `@user-qc6hw6lm5k` / `UCWrRPO325df-U3L9tUGlDgQ` / `雑学アーカイブ` |
| `youtube.hashtags` | X 用紹介文の末尾に必ず付けるハッシュタグ | `#雑学` `#大人の雑学` `#社会の裏側` |
| `affiliates[]` | 広告の一覧(優先順)。項目は `name`(管理用の名前)、`keywords`、`slots`、`html`(ASP の広告コード)または `url`+`label` | 2件(下記) |

### ⑤ ワークフロー `.github/workflows/auto-blog.yml`

| トリガー | 動作 |
|---|---|
| `schedule`(毎日 21:17 UTC = 06:17 JST) | 記事生成 → commit → ビルド → デプロイ |
| `workflow_dispatch`(手動実行) | 同上。入力 `force` を ON にすると本日分があっても追加生成 |
| `push`(main の `auto_blog/**` か workflow 自体が変わったとき) | 記事生成・告知はせず、ビルド → デプロイのみ |

- bot の commit は `GITHUB_TOKEN` で push されるため、それ自体では push トリガーは再発火しない
- 同時実行は `concurrency: auto-blog` で1本に制限(後から来た実行は待つ)
- 実行環境: ubuntu-latest / Python 3.12
- 権限: `contents: write`(記事・記録の commit)、`pages: write` と `id-token: write`(公開)、`issues: write`(X 用下書き)

**必要な GitHub 側の設定**

| 種類 | 名前 | 状態 |
|---|---|---|
| Actions Secret | `ANTHROPIC_API_KEY` | 設定済み |
| Actions Secret(任意) | `BLUESKY_HANDLE` / `BLUESKY_APP_PASSWORD` | 設定済み(ブログと YouTube の告知で同じアカウントを使用) |
| Actions Variable(任意) | `BLOG_MODEL` | 未設定(= Sonnet 5)。`claude-opus-5-5` にすると品質重視 |
| Pages | Source = GitHub Actions | 設定済み |

## 掲載中の広告(A8.net)

| 優先 | 広告 | 種類 | 出す記事(keywords) | 枠 |
|---|---|---|---|---|
| 1 | PLAUD NOTE(AIボイスレコーダー、購入10〜13%) | テキスト「世界初ChatGPT-4連携AIボイスレコーダー PLAUD NOTE」 | 議事録・会議・文字起こし・録音・ボイスレコーダー | 途中 |
| 2 | PLAUD NOTE(同上) | テキスト「6ヶ月で全世界5万ユーザー＆12億円売り上げAIボイスレコーダー PLAUD NOTE」 | 同上 | 末尾 |
| 3 | Notta ZENCHORD1(AI議事録イヤホン、購入7%) | バナー 300×250 | 議事録・会議・文字起こし | 末尾のみ |

- `config.json` の `affiliate_spare_codes` に、未使用の広告コード(PLAUD NOTE の短いテキスト版)を控えとして保存している(表示はされない)

- A8.net のサイト登録: サイト名「AI時短ラボ」、URL はブログの公開URL、カテゴリ「ソフトウェア」
- 提携済みで今後追加する候補: アイディー(英文添削、英語系の記事)、Aiarty Image Enhancer(画像系の記事)、
  ExpressVPN(セキュリティ・旅行系の記事)、GMKtec(ミニPC)。合う記事が公開されたらテキスト広告のコードをもらって追加する
- 提携済みだが載せない: 株式投資の銘柄情報(投資系は表示ルールが厳しくテーマ外)、HUAWEI、ソファスタイル、RingConn、A8.net メディア会員募集。REN SIM は保留
- PLAUD は「提携から3か月以内に記事掲載が確認できないと提携解除」の条件あり(議事録の記事に掲載済み)

## SNS アカウントと告知先

| 媒体 | 方式 | 設定 |
|---|---|---|
| Bluesky | 自動投稿(ブログ記事・YouTube 動画とも同じアカウント) | Secrets `BLUESKY_HANDLE` / `BLUESKY_APP_PASSWORD`(アプリパスワード)登録済み |
| X | 手動投稿。GitHub Issue の「X で投稿する」リンクから投稿画面を開く | 設定不要(Issue 作成は `GITHUB_TOKEN`) |
| YouTube | 告知元「雑学アーカイブ」(`@user-qc6hw6lm5k`、チャンネルID `UCWrRPO325df-U3L9tUGlDgQ`) | `config.json` の `youtube` |

**Bluesky のプロフィール**(2026-09-25 設定)
- 表示名: `AI時短ラボ｜雑学ショート`
- 説明文: AIで仕事と暮らしを時短する方法と「社会の裏側」雑学を発信している旨、ブログ(毎朝更新、AIツールの使い方・プロンプト例)と
  YouTube ショート(値段・税金・お店のしくみの雑学)の紹介、ブログURLとYouTubeチャンネルURL
- ブログURLを独自ドメインに変えたとき、YouTube に独自ハンドルを付けたときは、説明文のリンクを差し替える

## 費用の目安(2026-09-25 時点、1ドル=150円換算の概算)

| 設定 | 1記事 | 月(30記事) |
|---|---|---|
| **現在: Sonnet 5 + 6,000〜8,000字** | 約35〜45円 | 約1,000〜1,400円 |
| Opus 5.5 + 同じ長さ | 約70〜90円 | 約2,000〜2,700円 |

- 実額は Anthropic Console(platform.claude.com)の Usage で確認する
- SNS 紹介文の生成は1記事あたり1円未満
- YouTube 動画の紹介文は1本あたり約1円。新着チェックは無料
- GitHub Actions はブログが1回3〜4分(月約120分)、YouTube チェックが1回約30秒×1日8回(月約250分)で、無料枠(月2,000分)内に収まる

## 現在の状態(2026-09-25)

**ブログ**
- 公開済み記事: 1本(「ChatGPTで議事録を5分で作る手順｜コピペで使えるプロンプト例」、Opus 5.5 で生成、約1.4万字)
- 残りテーマ: `topics.txt` に19件
- 分量指示と Sonnet 5 への切り替え(PR #3)は、次回(2026-09-26 06:17 JST)の生成から適用される
- SNS 告知(PR #4)も次回から動く。1本目の記事も直近3日以内のため、次回に2本目と一緒に告知される

**YouTube 告知**
- 2026-09-25 11:55 JST の手動実行で初回登録済み。公開済みの動画15本を「告知なし」で `youtube_log.json` に記録
  (最初の手動実行はハンドルから取り出したチャンネルIDが誤っていて RSS が 404 になったため、PR #7 で ID を明示設定)
- 動画一覧は RSS(チャンネルID指定)で取得できている
- 1週間分のショートを1日おきに予約公開中。各動画は公開後の最初のチェック(最大3時間後)で告知される

## 変更履歴

| PR | 内容 |
|---|---|
| #1 | ブログ一式を追加(当初の既定モデルは Opus 5) |
| #2 | 既定モデルを Opus 5.5 に変更。フォールバックを Opus / Fable 系のみに限定 |
| #3 | 記事を6,000〜8,000字に制限、Web検索を最大3回に削減、長さの警告ログ追加、既定モデルを Sonnet 5 に変更 |
| #4 | この仕様書を追加。公開後の SNS 告知(Bluesky 自動投稿、X 用下書きの Issue 作成)を追加 |
| #5 | YouTube 新着動画の告知(3時間おき、Bluesky 自動投稿+X 用下書き)を追加 |
| #6 | 新着動画の判定を「公開から48時間以内」から「RSS に初めて現れた動画」に変更(予約公開の動画を取りこぼさないため) |
| #7 | チャンネルIDを `config.json` で明示設定。RSS が使えないときの代替取得(アップロード再生リストの RSS → チャンネルページ+oEmbed)とログを追加 |
| #10 | 動画の紹介文を改善: チャンネル名「雑学アーカイブ」を設定し、ブログ名が入らないようにした。問いかけ型の文章、X はハッシュタグ固定 |
| #11 | アフィリエイト枠を記事途中(2つ目の見出しの前、最大2件)と末尾の2か所に |
| #12 | ASP の広告コードを改変せず掲載できるように(`html`)、広告ごとの枠指定(`slots`)。PLAUD NOTE(途中・末尾で別の文言)と Notta ZENCHORD1 を掲載 |

## 未対応・要検討事項

- **独自ドメイン**: `github.io` のサブパスでは AdSense の審査・`ads.txt` 設置が事実上できないため、独自ドメインの取得と Pages への設定が必要
- **AdSense 申請**: 記事が20〜30本たまってから(独自ドメイン設定後)
- **アフィリエイト提携**: A8.net 等で提携し、`config.json` の `affiliates[].url` を設定する
- **Search Console 登録**: 公開URLを登録し `sitemap.xml` を送信する
- **分量の実績確認**: Sonnet 5 が6,000〜8,000字の指示を守るか、次回以降の記事で確認する
- **APIキーの有効期限**: キー作成時に有効期限を設定した場合、期限前に作り直して Secret を更新する必要がある
- **Node.js 20 の非推奨警告**: `actions/checkout@v4` などが Node 20 向けで、現在は Node 24 で強制実行されている(動作に支障なし)。将来、各 action の新しいメジャー版に上げる
- **X への自動投稿**: 費用(月約900円)に見合うアクセスが出てから検討する
- **SNS 告知の実績確認**: Bluesky 投稿と X 用 Issue は本番でまだ一度も動いていない(ブログは 2026-09-26 朝、動画は予約公開の1本目で初回)
- **アカウントのテーマ**: ブログ(AI時短術)と動画(社会の裏側の雑学)でテーマが異なる。同じ Bluesky アカウントでの反応を見て、分けるか検討する
- **生成失敗時の通知**: 現状は Actions の実行が失敗するだけで、通知の仕組みはない(GitHub のメール通知に依存)
- **記事の品質管理**: 生成記事の誤り確認・人の手による追記の運用は未定
