# SNS Buzz Monitor

X(Twitter)の「超短時間で爆発的にバズった投稿」をジャンル別に検知し、LINEに即時通知するツール。

## 仕組み

```
[GitHub Actions] (1時間おき、無料枠内で自動実行)
    ↓
[Apify] Xの投稿データ収集 (いいね数・インプレッション数など)
    ↓
[detector.py] 「投稿後6時間以内に500万インプレッション」を判定
    ↓
[Groq] 該当投稿だけをジャンル分類 (スポーツ/芸能/政治/経済/エンタメ/事件事故/テック/その他)
    ↓
[LINE Messaging API] 即時プッシュ通知
    ↓
[SQLite] 通知済み投稿を記録(同じ投稿を二度通知しない)
```

全て無料枠内で完結する設計です(想定コスト: 実質0円/月)。

---

## セットアップ手順

### 1. 必要なアカウントを作る(全部無料)

| サービス | 用途 | URL |
|---|---|---|
| Apify | X投稿データ収集 | https://apify.com |
| Groq | ジャンル分類AI | https://console.groq.com |
| LINE Developers | 通知送信 | https://developers.line.biz/ja/ |
| GitHub | 定期実行(1時間おき) | https://github.com |

### 2. Apify: 2段階方式でActorを2つ使います

X関連のApify Actorを検証した結果、**検索・単一ツイート取得ではインプレッション数(viewCount)が取得できない**ことが判明しました(プロフィールのタイムライン取得エンドポイントだけがインプレッション数を返す仕様のためです)。そのため以下の2段階方式に変更しています。コード(`apify_collector.py`)は設定済みです。

1. **1段階目(発見)**: `apidojo/tweet-scraper` で検索し、いいね数を仮の急上昇シグナルとして候補を絞り込む
2. **2段階目(確定)**: 候補になった投稿の投稿者だけ `apidojo/twitter-profile-scraper` でプロフィールを取得し直し、本物のインプレッション数を突き合わせる

**唯一やってほしいこと(初回のみ)**:
1段階目のActor(`apidojo/tweet-scraper`)は既に動作確認済みです(あなたが実際にテストしてくれました)。2段階目の`apidojo/twitter-profile-scraper`についても、Apify Console上で数件だけ試験実行し、出力に`viewCount`フィールドが実際に含まれているか確認してください。含まれていない場合は教えてください、コードを調整します。

調整可能なパラメータ(`apify_collector.py`内):

```python
SEARCH_QUERIES = ["lang:ja min_faves:5000"]   # 1段階目の検索条件
PRELIMINARY_LIKE_THRESHOLD = 30_000           # 2段階目に進める「仮の」いいね数閾値
```

`PRELIMINARY_LIKE_THRESHOLD`は「500万インプレッションに到達しそうな投稿」を粗く絞り込むためのいいね数の目安です。実際に運用しながら、インプレッション数といいね数の相関を見て調整することをおすすめします(投稿のジャンルによって比率はかなり変わります)。

### 3. Groq APIキー取得

1. https://console.groq.com でサインアップ(クレジットカード不要)
2. APIキーを発行
3. 控えておく

### 4. LINE Messaging API設定

1. https://developers.line.biz/ja/ でログイン
2. 「新規プロバイダー作成」→「Messaging APIチャネル作成」
3. チャネル基本設定から「チャネルアクセストークン(長期)」を発行 → 控える
4. 作成されたLINE公式アカウントのQRコードを、**自分のLINEアプリで友だち追加**
5. 自分の `userId` を取得する(少し手間です):
   - 一時的にWebhook URLを受け取れる環境が必要(例: [Glitch](https://glitch.com) や [Replit](https://replit.com) の無料プランで簡易Flaskサーバーを立て、Webhookを有効化)
   - 自分から公式アカウントに何かメッセージを送ると、Webhookイベントに `source.userId` が含まれてくるのでそれを控える
   - この作業は最初の1回だけでOK(以降は固定のuserIdを使い回す)

### 5. GitHubリポジトリ作成 & Secrets設定

1. このフォルダの中身をGitHubリポジトリにpush
2. リポジトリの Settings → Secrets and variables → Actions で以下を登録:
   - `APIFY_TOKEN`
   - `GROQ_API_KEY`
   - `LINE_CHANNEL_ACCESS_TOKEN`
   - `LINE_USER_ID`
3. `.github/workflows/monitor.yml` が自動的に1時間おきに実行されます(手動実行もActionsタブから可能)

---

## ローカルでテストする場合

```bash
pip install -r requirements.txt
cp .env.example .env   # 値を埋める
export $(cat .env | xargs)  # 環境変数として読み込む(簡易的な方法)
python main.py
```

## 設定値の調整

`detector.py` の冒頭にある以下の値を変更すれば、検知条件を調整できます。

```python
IMPRESSION_THRESHOLD = 5_000_000   # インプレッション閾値
THRESHOLD_HOURS = 6.0              # 「数時間」の目安時間
```

## 既知の制約・注意点

- **GitHub Actionsのcacheによる状態保存は完全ではありません**。`monitor.yml` では `actions/cache` でSQLite DBを疑似的に永続化していますが、GitHubのキャッシュはLRUで自動削除されることがあるため、まれに通知の重複や欠落が起きる可能性があります。安定運用したい場合は、DBを外部サービス(Turso, Supabaseの無料枠など)に置き換えることをおすすめします。
- Apify Actorの選定・出力フィールドのマッピングは前述の通り手動対応が必要です。
- Xの利用規約・robots.txt等はActor提供元の責任範囲に依存します。非公式スクレイピングである以上、規約変更やアカウント制限のリスクはゼロではありません。
- 各サービスの無料枠仕様(特にX関連のサードパーティAPI)は変動が激しいため、運用開始前に最新の料金ページを確認してください。

## ファイル構成

```
sns-monitor/
├── main.py                  # メイン実行(全体オーケストレーション)
├── detector.py               # 急上昇判定ロジック
├── apify_collector.py        # Apify連携(投稿収集)
├── genre_classifier.py       # Groq連携(ジャンル分類)
├── line_notifier.py          # LINE通知
├── db.py                     # SQLite(重複通知防止)
├── requirements.txt
├── .env.example
└── .github/workflows/monitor.yml   # 1時間おき自動実行
```
