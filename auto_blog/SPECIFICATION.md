# システム仕様書: 全自動更新ブログ「AI時短ラボ」

最終更新: 2026-09-25(PR #3 マージ後の状態)

使い方・立ち上げ手順は `README.md`、この文書は「今どういう仕様で動いているか」の記録です。
仕様を変えたときはこの文書も合わせて更新すること。

## 目的と収益モデル

- 生成AI・業務効率化の実践ガイド記事を、毎日1本 Claude API で自動生成して公開する
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
   └─ deploy ジョブ … GitHub Pages に公開
```

公開URL: https://jinseiowata.github.io/hm_workspace/

## ファイル構成

| パス | 役割 |
|---|---|
| `.github/workflows/auto-blog.yml` | 毎日の自動実行・手動実行・デプロイの定義 |
| `auto_blog/generate_article.py` | テーマ選択・Claude API 呼び出し・記事ファイル保存 |
| `auto_blog/build_site.py` | 静的サイト生成(HTML / sitemap / RSS / robots / ads.txt) |
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
| `<記事ファイル名>.html` | 記事ページ。PR表記、タグ、本文、アフィリエイト枠、AI生成の注記、関連記事(タグ一致・最大5件) |
| `tag-<名前>.html` | タグ別一覧。日本語タグは文字コードの16進表記でファイル名にする |
| `about.html` / `privacy.html` | 運営者情報 / プライバシーポリシー・免責事項(AdSense 審査対策) |
| `sitemap.xml` | 全ページ(記事は lastmod 付き)。公開URLが分かるときのみ出力 |
| `robots.txt` | 全許可+sitemap の場所 |
| `feed.xml` | RSS 2.0(新しい順に最大20件) |
| `ads.txt` | `config.json` に `adsense_client_id` があるときのみ出力 |
| `style.css` | `static/style.css` のコピー |

**URL の扱い**: 環境変数 `SITE_BASE_URL`(Actions では `configure-pages` の出力を使用)からパス部分を取り出し、
サイト内リンクに付与する(`/hm_workspace/…` のようなサブパス配下でも動く)。canonical・sitemap・RSS は絶対URL。

**Markdown 変換**: `markdown` ライブラリ(拡張: fenced_code, tables, toc, sane_lists)

**法令・審査対応として自動で付くもの**
- 全記事の冒頭に「※本記事にはプロモーション(広告)が含まれる場合があります。」(ステマ規制対応)
- 全記事の末尾に「生成AIを活用して作成」「最新情報は公式サイトで確認を」の注記
- アフィリエイトリンクに `rel="sponsored nofollow noopener"`

### ③ 設定ファイル `config.json`

| キー | 用途 | 現在値 |
|---|---|---|
| `site_name` / `tagline` / `description` / `author` | サイト名・キャッチコピー・説明・運営者名 | AI時短ラボ ほか |
| `base_url` | 公開URL(Actions では `SITE_BASE_URL` が優先されるので空でよい) | 空 |
| `adsense_client_id` | AdSense のパブリッシャーID(`ca-pub-…`)。入れると広告タグと ads.txt を出力 | 未設定 |
| `google_site_verification` | Search Console の所有権確認コード | 未設定 |
| `affiliates[]` | `keywords` のどれかがタイトルか本文に含まれる記事に、`url` が設定済みのものだけ「おすすめ」枠で表示 | 例2件(url 未設定のため非表示) |

### ④ ワークフロー `.github/workflows/auto-blog.yml`

| トリガー | 動作 |
|---|---|
| `schedule`(毎日 21:17 UTC = 06:17 JST) | 記事生成 → commit → ビルド → デプロイ |
| `workflow_dispatch`(手動実行) | 同上。入力 `force` を ON にすると本日分があっても追加生成 |
| `push`(main の `auto_blog/**` か workflow 自体が変わったとき) | 記事生成はせず、ビルド → デプロイのみ |

- bot の commit は `GITHUB_TOKEN` で push されるため、それ自体では push トリガーは再発火しない
- 同時実行は `concurrency: auto-blog` で1本に制限(後から来た実行は待つ)
- 実行環境: ubuntu-latest / Python 3.12

**必要な GitHub 側の設定**

| 種類 | 名前 | 状態 |
|---|---|---|
| Actions Secret | `ANTHROPIC_API_KEY` | 設定済み |
| Actions Variable(任意) | `BLOG_MODEL` | 未設定(= Sonnet 5)。`claude-opus-5-5` にすると品質重視 |
| Pages | Source = GitHub Actions | 設定済み |

## 費用の目安(2026-09-25 時点、1ドル=150円換算の概算)

| 設定 | 1記事 | 月(30記事) |
|---|---|---|
| **現在: Sonnet 5 + 6,000〜8,000字** | 約35〜45円 | 約1,000〜1,400円 |
| Opus 5.5 + 同じ長さ | 約70〜90円 | 約2,000〜2,700円 |

- 実額は Anthropic Console(platform.claude.com)の Usage で確認する
- GitHub Actions は1回3分程度で、無料枠内に収まる

## 現在の状態(2026-09-25)

- 公開済み記事: 1本(「ChatGPTで議事録を5分で作る手順｜コピペで使えるプロンプト例」、Opus 5.5 で生成、約1.4万字)
- 残りテーマ: `topics.txt` に19件
- 分量指示と Sonnet 5 への切り替え(PR #3)は、次回(2026-09-26 06:17 JST)の生成から適用される

## 変更履歴

| PR | 内容 |
|---|---|
| #1 | ブログ一式を追加(当初の既定モデルは Opus 5) |
| #2 | 既定モデルを Opus 5.5 に変更。フォールバックを Opus / Fable 系のみに限定 |
| #3 | 記事を6,000〜8,000字に制限、Web検索を最大3回に削減、長さの警告ログ追加、既定モデルを Sonnet 5 に変更 |

## 未対応・要検討事項

- **独自ドメイン**: `github.io` のサブパスでは AdSense の審査・`ads.txt` 設置が事実上できないため、独自ドメインの取得と Pages への設定が必要
- **AdSense 申請**: 記事が20〜30本たまってから(独自ドメイン設定後)
- **アフィリエイト提携**: A8.net 等で提携し、`config.json` の `affiliates[].url` を設定する
- **Search Console 登録**: 公開URLを登録し `sitemap.xml` を送信する
- **分量の実績確認**: Sonnet 5 が6,000〜8,000字の指示を守るか、次回以降の記事で確認する
- **APIキーの有効期限**: キー作成時に有効期限を設定した場合、期限前に作り直して Secret を更新する必要がある
- **Node.js 20 の非推奨警告**: `actions/checkout@v4` などが Node 20 向けで、現在は Node 24 で強制実行されている(動作に支障なし)。将来、各 action の新しいメジャー版に上げる
- **生成失敗時の通知**: 現状は Actions の実行が失敗するだけで、通知の仕組みはない(GitHub のメール通知に依存)
- **記事の品質管理**: 生成記事の誤り確認・人の手による追記の運用は未定
