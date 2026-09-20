# iPadだけでセッションを作る方法

## なぜ noVNC が弾かれるか

Codespaces 上の Playwright Chromium は、X から見て自動化ブラウザと判定されやすいです。
「しばらくお待ちください」のままログインできない・ホームが空、は典型症状です。

## なぜ Safari だけで Cookie が取れないか

X の `auth_token` は **HttpOnly** です。
ページ上の JavaScript やブックマークレットでは読めません。
Mac の「開発」メニューなしで iPhone/iPad Safari から取るのも事実上できません。

---

## 現実的な選択肢（優先順）

### A. 一度だけ PC を借りる（最も確実・所要10分）

学校・図書館・友人の PC で次だけやる:

```bash
pip install playwright
playwright install chromium
git clone https://github.com/mnaoki20081106-afk/X-Bunseki.git
cd X-Bunseki
python login_helper.py
```

ログイン → Enter → 表示される base64 をメモ → iPad から GitHub Secrets の `X_SESSION_STATE` に貼る。

または PC の Chrome で x.com にログインし:

1. F12 → Application → Cookies → `auth_token` と `ct0`
2. Codespaces で `python3 cookie_to_session.py` に貼る

### B. Codespaces で「本物の Google Chrome」を使う（再挑戦）

Chromium ではなく google-chrome-stable を入れると通ることがあります。

Codespaces ターミナル:

```bash
# Chrome 安定版を入れる
wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | sudo gpg --dearmor -o /usr/share/keyrings/google.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google.gpg] http://dl.google.com/linux/chrome/deb/ stable main" | sudo tee /etc/apt/sources.list.d/google-chrome.list
sudo apt-get update -qq
sudo apt-get install -y -qq google-chrome-stable

# 既存の noVNC 手順
bash codespace_login.sh
```

`login_helper.py` は可能なら `channel="chrome"` で起動するよう更新済みです。
それでも「しばらくお待ちください」なら、Codespaces 経路は諦めた方が早いです。

### C. クラウド PC を iPad ブラウザから使う

無料枠があるサービス例（登録・カードが必要なものあり）:

- GitHub Codespaces 以外の Linux デスクトップ（Chrome 入り）
- 一時的なクラウド VM + Chrome Remote Desktop

そこで通常の Chrome を開き、ダミーXにログイン → Cookie 取得 → `cookie_to_session.py`。

### D. 収集方式そのものを変える（最終手段）

ログイン必須の Playwright 検索をやめ、有料 API / Apify 等に切り替える。
月額コストが発生します。必要なら相談してください。

---

## やっても無駄なこと

- noVNC を何度もリトライするだけ
- iPad Safari のブックマークレットで Cookie を抜こうとする（HttpOnly で取れない）
- 本アカのセッションを使う（凍結リスク）
