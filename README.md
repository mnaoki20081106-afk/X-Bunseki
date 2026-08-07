# SNS Buzz Monitor

X(Twitter)の「超短時間で爆発的にバズった投稿」をジャンル別に検知し、LINEに即時通知するツール。

## 仕組み

```
[GitHub Actions] (1時間おき、無料枠内で自動実行)
    ↓
[Playwright + ログイン済みダミーXアカウント] X検索で投稿データ収集
    ↓  (いいね・引用・ブックマーク・返信数)
[detector.py] 投稿後1時間以内に、引用50+・ブックマーク100+・いいね500+ を判定
    ↓
[Groq] 該当投稿だけをジャンル分類 (スポーツ/芸能/政治/経済/エンタメ/事件事故/テック/その他)
    ↓
[LINE Messaging API] 即時プッシュ通知
    ↓
[SQLite] 通知済み投稿を記録(同じ投稿を二度通知しない)
```

全て無料枠内で完結する設計です(想定コスト: 実質0円/月)。

## なぜApifyではなくPlaywrightなのか

調査の結果、以下が判明しました。

- Xは未ログイン状態だと検索エンドポイント自体が使えない(インプレッション数も返さない)
- 検索やインプレッション数を返すApify Actorは、無料プランではAPI経由の自動実行が禁止されている(コンソールでの手動実行のみ許可)
- 無料でAPI自動実行できるActorは、逆に検索機能自体をサポートしていない(特定アカウント監視限定)

「完全無料」と「投稿全体を検索」を両立させる唯一の方法として、**自前でログイン済みのダミーXアカウントを使い、Playwright(ヘッドレスブラウザ)でX検索結果を直接取得する**方式を採用しています。GitHub Actionsの無料枠内で完結します。

## 判定基準

投稿後**1時間以内**に、以下を**全て**満たす投稿を「爆発」と判定します。

- 引用(quotes) 50以上
- ブックマーク数 100以上
- いいね数 500以上

ランキングは「引用の伸び速度 > ブックマークの伸び速度 > いいね数」の優先順位でスコアリングします。閾値は `detector.py` の冒頭で調整可能です。

インプレッション数は技術的な制約で使っていません(無料では取得不可能なため)。

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
- `GROQ_API_KEY`
- `LINE_CHANNEL_ACCESS_TOKEN`
- `LINE_USER_ID`

### 5. GitHub Actionsで実行

Actionsタブから「SNS Buzz Monitor」→「Run workflow」で手動実行して動作確認してください。以降は1時間おきに自動実行されます。

---

## セッションの定期更新について

ログイン状態(`storage_state.json`)には有効期限があります。数週間〜数ヶ月に一度、Xから再ログインを求められるようになったら、手順2〜3をもう一度実行してSecretsを更新してください(エラーログで気づけるようにしてあります)。

## 既知の制約・注意点

- **DOM構造への依存**: `playwright_collector.py` はXの現在の画面構造を前提にしたコードです。Xの仕様変更でセレクタが合わなくなることがあります。エラーが出たり0件しか取れなくなったりしたら教えてください、調整します。
- **アカウント凍結リスク**: 自動化されたアクセスなので、ゼロではありません。ダミーアカウント運用を強く推奨します。
- **GitHub Actionsのcacheによる状態保存は完全ではありません**: `monitor.yml` では `actions/cache` でSQLite DBを疑似的に永続化していますが、GitHubのキャッシュはLRUで自動削除されることがあるため、まれに通知の重複や欠落が起きる可能性があります。

## ファイル構成

```
sns-monitor/
├── main.py                   # メイン実行(全体オーケストレーション)
├── detector.py                # 急上昇判定ロジック(引用・ブックマーク基準)
├── playwright_collector.py    # X検索・データ収集(Playwright)
├── login_helper.py            # 【手元で1回だけ実行】ログイン状態の作成
├── genre_classifier.py        # Groq連携(ジャンル分類)
├── line_notifier.py           # LINE通知
├── db.py                      # SQLite(重複通知防止)
├── requirements.txt
└── .github/workflows/monitor.yml   # 1時間おき自動実行
```
