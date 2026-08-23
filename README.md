# SNS Buzz Monitor (v4)

X(Twitter)で**これから伸びる投稿**を早期に検知し、iPhoneに即時通知するツール。
検知した投稿をTikTokのスクープ速報・解説動画にすることが目的です。

> **v4での大幅な見直しについて**
> 実行履歴49回分を分析したところ、v3は「伸び率」を一度も計算しておらず、
> 通知が出た実行は49回中6回、しかも17件の通知のうち14件が同一の地震でした。
> 原因と対応の詳細は **[docs/REVIEW.md](docs/REVIEW.md)** にまとめてあります。
> Grokの使い方は **[docs/grok_prompts.md](docs/grok_prompts.md)** です。

## 仕組み

```
[GitHub Actions] 15分おきに自動実行 (Publicリポジトリなので無料・無制限)
    ↓
[playwright_collector] keywords.txt から検索クエリを自動生成してX検索
    ↓  (いいね・リポスト・返信・ブックマーク・表示回数)
[db.observations]     ★収集した「全投稿」を時系列で記録する
    ↓
[growth.py]           前回観測との差分から「いいね/分」と「加速度」を実測
    ↓
[detector.py]         0〜100点の総合スコアを算出 (閾値は1つだけ)
    ↓
[clustering.py]       同じ話題の投稿をまとめる (1話題1通知)
    ↓
[content_scorer.py]   上位数件だけLLMで「動画ネタとして使えるか」を評価
    ↓
[Pushover / LINE]     本文・伸び率・タイトル案つきで即時通知
```

全て無料枠内で完結します(想定コスト: 実質0円/月)。

## 判定の考え方

**「今どれだけ速く伸びているか」と「加速しているか」**で判定します。

v3は「いいね総数 ÷ 投稿からの経過時間」を速度として使っていましたが、
これは平均速度であって伸び率ではありません。
**投稿直後に跳ねて既に失速した投稿でも高いまま**になるため、
早期発見ではなく「終わったバズ」を掴んでしまっていました。

v4は同じ投稿を15分おきに観測し、**前回との差分**を取ります。

```
いいね/分 = (今回のいいね - 前回のいいね) ÷ 経過分
加速度    = 今の区間の速度 ÷ その前の区間の速度     # 1.0超なら加速中
```

スコアの配点(合計100点):

| 項目 | 配点 | 意味 |
|---|---|---|
| 伸び率 | 40 | いいね/分(対数スケール) |
| 加速度 | 15 | まだ伸びているか、失速しているか |
| 議論量 | 15 | 返信÷いいね。賛否が割れている＝解説需要 |
| 保存率 | 10 | ブックマーク÷いいね。後で見返したい情報 |
| 拡散率 | 10 | リポスト÷いいね。ニュース性 |
| 関連度 | 10 | keywords.txt への当たり具合 |

投稿が古いほどスコアは減衰します(発見が遅い＝動画にする価値が下がるため)。
既定では **55点以上**で通知します。

## 感度の調整(コード変更不要)

通知が多すぎる/少なすぎるときは、GitHubの
**Settings → Secrets and variables → Actions → Variables** タブで設定します。

| 変数名 | 既定値 | 意味 |
|---|---|---|
| `NOTIFY_SCORE` | 55 | この点数以上で通知。**まずここを調整** |
| `MIN_LIKES_FLOOR` | 150 | これ未満のいいねは無視 |
| `MAX_AGE_MINUTES` | 180 | これより古い投稿は追わない |
| `NOTIFY_MAX_PER_RUN` | 2 | 1回の実行で鳴らす上限 |
| `NOTIFY_MAX_PER_DAY` | 12 | 24時間の上限 |
| `SEARCH_MIN_FAVES` | 50 | ジャンル特化検索の最低いいね数 |
| `BROAD_MIN_FAVES` | 800 | 広域検索の最低いいね数 |

- **通知が多い** → `NOTIFY_SCORE` を 60〜65 に
- **通知が少ない** → `NOTIFY_SCORE` を 50 に

## 監視するジャンルの変更

`keywords.txt` に書いたワードが、そのままX検索クエリのOR条件になります
(`(地震 OR 台風 OR 速報 OR ...) lang:ja -filter:retweets min_faves:50`)。
GitHub上で直接編集すれば、次の実行から反映されます。

`keywords_combo.txt` は、AND条件を書ける場所です。1行がそのまま1本の検索になります。

```
(逮捕 OR 書類送検 OR 起訴) (俳優 OR 女優 OR タレント OR 歌手 OR アイドル)
```

「逮捕」のようなワードは単独で検索すると一般ニュースの速報で埋まってしまいますが、
「芸能人の文脈」をAND条件で掛ければ、TikTokで最も需要の高い部分だけを拾えます。

`keywords_ng.txt` は逆で、ここに書いたワードを含む投稿は通知しません。
懸賞・プレゼント企画など「エンゲージメントは伸びるがネタにならない」投稿を除外します。
**「なんでこれが通知されたんだ」という投稿が来たら、そこに特有のワードを1行足してください。**

⚠️ ただしNGワードは「1つでも含まれたら問答無用で捨てる」拒否権です。
本物のニュースにも出てきうる一般語(「ありがとうございました」など)を入れると、
拾いたい投稿まで消えます。迷ったら入れないでください。

## 動画の結果を記録してください ★重要

`data/feedback.csv` に、TikTok動画を出すたび1行足してください。
これが無いと、検知精度が上がったかどうかを判定できません。
詳しくは [data/README.md](data/README.md) を参照。

## なぜApifyではなくPlaywrightなのか

調査の結果、以下が判明しました。

- Xは未ログイン状態だと検索エンドポイント自体が使えない(インプレッション数も返さない)
- 検索やインプレッション数を返すApify Actorは、無料プランではAPI経由の自動実行が禁止されている(コンソールでの手動実行のみ許可)
- 無料でAPI自動実行できるActorは、逆に検索機能自体をサポートしていない(特定アカウント監視限定)

「完全無料」と「投稿全体を検索」を両立させる唯一の方法として、**自前でログイン済みのダミーXアカウントを使い、Playwright(ヘッドレスブラウザ)でX検索結果を直接取得する**方式を採用しています。GitHub Actionsの無料枠内で完結します。

---

## セットアップ手順

### 1. ダミーのXアカウントを1つ新規作成する

**重要**: 本アカウントは使わないでください。このツールは自動でX検索ページに定期的にアクセスするため、アカウント凍結のリスクがゼロではありません。何かあってもいい、監視専用のアカウントを新しく作ってください。

### 2. ログイン状態を保存する

この手順だけは自動化できません(2段階認証や画像認証が入ることがあるため、人間の操作が必要)。**PC/Macがあるか、iPadしか無いか**で手順が変わります。

#### PC/Macがある場合

```bash
pip install playwright
playwright install chromium
python login_helper.py
```

ブラウザが起動するので、画面の指示に従ってダミーアカウントでログインしてください。ログイン完了後、ターミナルでEnterキーを押すと `storage_state.json` というファイルが生成されます。

#### iPadだけの場合

**GitHub Codespaces**(GitHubが無料で提供する、ブラウザだけで使えるクラウド開発環境)を使います。Safari上で全て完結します。

1. GitHubのリポジトリページを開く
2. 緑色の「Code」ボタン → 「Codespaces」タブ → 「Create codespace on main」をタップ
3. 数十秒待つと、ブラウザ上にVS Code風の画面(Codespaces)が開く
4. 画面下部の「TERMINAL」タブをタップしてターミナルを開く
5. 以下を1行だけ入力してEnter:
   ```
   bash codespace_login.sh
   ```
6. 自動的にセットアップが進み、最後に案内が表示される
7. 案内に従って、画面下部の「PORTS」タブ → ポート6080の行にある地球儀アイコンをタップ
8. 新しいタブでnoVNCの画面が開くので、「Connect」ボタンをタップ
9. 画面内にXのログインページが表示されるので、ダミーアカウントでログイン
10. ログインできたら、最初のターミナルのタブに戻って **Enter キーを押す**
11. `session_base64.txt` の中身が自動的にターミナルに表示されるので、それをコピー

この方式は最初のセットアップ時に1回だけ行えばOKです(以降の毎時実行はGitHub Actionsが自動でやるので、Codespacesは不要になります)。

**Codespacesの無料枠**: 個人アカウントで月60時間まで無料です。今回の作業は数分で終わるので、無料枠を使い切る心配はありません。作業が終わったら、Codespacesの一覧画面から今回作ったCodespaceを削除しておくと、より安全です(自動で一定期間後に停止もされます)。

### 3. storage_state.json (またはコピーした文字列) をGitHub Secretsに登録

- PC/Macの場合: 生成された `storage_state.json` をbase64化してからコピー
  ```bash
  base64 -i storage_state.json | tr -d '\n'
  ```
- iPad(Codespaces)の場合: 手順2の最後で表示された `session_base64.txt` の中身をそのままコピー

GitHubリポジトリの **Settings → Secrets and variables → Actions** で:
- Name: `X_SESSION_STATE`
- Secret: コピーした文字列を貼り付け

**重要**: `storage_state.json` や `session_base64.txt` はログイン情報そのものなので、GitHubのリポジトリには絶対にアップロードしないでください(`.gitignore`に追加済みです)。Codespacesを使った場合も、作業が終わったらそのCodespaceごと削除しておくと安全です。

### 4. Groq・LINEの設定(前回までと同じ)

- Groq APIキー: https://console.groq.com
- LINE Messaging API: チャネルアクセストークン・userIdを取得

GitHub Secretsに以下を登録:
- `X_SESSION_STATE`(手順3で取得)
- `GROQ_API_KEY`(未設定でも動きます。その場合はタイトル案・要約が付かないだけ)
- `LINE_CHANNEL_ACCESS_TOKEN`
- `LINE_USER_ID`

### 5. GitHub Actionsで実行

Actionsタブから「SNS Buzz Monitor」→「Run workflow」で手動実行して動作確認してください。以降は15分おきに自動実行されます。

---

## セッションの定期更新について

ログイン状態(`storage_state.json`)には有効期限があります。数週間〜数ヶ月に一度、Xから再ログインを求められるようになったら、手順2〜3をもう一度実行してSecretsを更新してください(エラーログで気づけるようにしてあります)。

## 既知の制約・注意点

- **DOM構造への依存**: `playwright_collector.py` はXの現在の画面構造を前提にしたコードです。Xの仕様変更でセレクタが合わなくなることがあります。エラーが出たり0件しか取れなくなったりしたら教えてください、調整します。
- **アカウント凍結リスク**: 自動化されたアクセスなので、ゼロではありません。ダミーアカウント運用を強く推奨します。
- **アクセス頻度が4倍になりました**: v4で毎時 → 15分おきに変更したため、Xへのアクセス量も増えています。伸び率の計算に必要な変更ですが、凍結が心配なら `.github/workflows/monitor.yml` の cron を `"7,37 * * * *"`(30分おき)などに下げてください。
- **GitHub Actionsのcacheによる状態保存は完全ではありません**: `monitor.yml` では `actions/cache` でSQLite DBを疑似的に永続化しています。キャッシュが消えると伸び率が一時的に「推定値」に戻りますが、数回の実行で自動的に復旧します。
- **LINEの無料枠は月200通です**: 1日12通の上限でも月360通で超過します。メインの通知はPushoverにして、LINEは「動作確認」用に留めるのが安全です。気になる場合は `NOTIFY_MAX_PER_DAY` を6に下げてください。
- **v4の閾値はまだ実データで検証していません**: 55点・150いいね・120いいね/分といった具体的な数値は根拠のある推定値ですが、あなたの環境で検証したものではありません。`data/log/` が数日分たまってから調整してください(→ [docs/grok_prompts.md](docs/grok_prompts.md) のプロンプト④)。

## ファイル構成

```
X-Bunseki/
├── main.py                    # メイン実行(全体オーケストレーション)
├── playwright_collector.py    # X検索・データ収集(Playwright)
├── growth.py                  # ★伸び率・加速度の実測
├── detector.py                # 判定・採点(0〜100点)
├── clustering.py              # 同じ話題の投稿をまとめる
├── keyword_filter.py          # 検索クエリ生成・NGワード・関連度スコア
├── content_scorer.py          # LLMで「動画ネタとして使えるか」を評価
├── notification_text.py       # 通知本文の組み立て(LINE/Pushover共通)
├── line_notifier.py           # LINE通知
├── pushover_notifier.py       # Pushover通知
├── db.py                      # SQLite(観測履歴・重複通知防止)
├── login_helper.py            # 【手元で1回だけ実行】ログイン状態の作成
├── codespace_login.sh         # 【iPadのみの場合】同上
├── cloudflare_worker.js       # LINEの「動作確認」応答・学校判定
├── keywords.txt               # 監視するワード(＝OR検索クエリになる)
├── keywords_combo.txt         # 組み合わせ検索(AND条件。1行=1検索)
├── keywords_ng.txt            # 除外するワード
├── data/
│   ├── feedback.csv           # ★動画の結果を手で記録するファイル
│   ├── log/                   # 分析用ログ(自動生成)
│   └── grok_report.md         # Grokに貼る要約(自動生成)
├── tools/
│   └── make_grok_report.py    # 分析用ログ → Grokに貼れる要約への変換
├── docs/
│   ├── REVIEW.md              # v3の問題点の診断レポート
│   └── grok_prompts.md        # Grok活用ガイド(プロンプト集)
├── tests/test_pipeline.py     # 判定ロジックのテスト(24件)
└── .github/workflows/
    ├── monitor.yml            # 15分おき自動実行
    ├── grok_report.yml        # 【手動】Grok用レポート作成
    └── test.yml               # push毎にロジックテスト
```

## 閾値の調整のしかた

閾値は推測で決めるものではなく、実測ログから決めます。

1. **Actions** タブ → **「Grok用レポート作成」** → **Run workflow**
2. 完了後 `data/grok_report.md` を開いて全文コピー
3. [docs/grok_prompts.md](docs/grok_prompts.md) の**プロンプト④**に貼ってGrokに分析させる
4. 返ってきた数値を **Settings → Variables** に設定する

レポートには各投稿の「15分おきの伸びの軌跡」と「スコア」「最終的にどれだけ伸びたか」が
対応付けて入っているので、**スコアが当たっていたかどうかの答え合わせ**ができます。

## テストの実行

Xにはアクセスしないので、数秒で終わります。

```bash
python3 tests/test_pipeline.py
```

ここが全部通っていれば、判定ロジックは正常です。
それでも通知が来ない場合は、原因は収集側(Xの画面構造の変化など)です。
