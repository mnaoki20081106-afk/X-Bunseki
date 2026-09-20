# Signal Filter（GitHub Pages）

公開URL（Pages有効化後）:

**https://mnaoki20081106-afk.github.io/X-Bunseki/**

## 初回セットアップ

1. リポジトリ **Settings → Pages**
2. Source を **GitHub Actions** にする（`pages.yml` がデプロイ）
3. Actions の **Deploy GitHub Pages** を一度手動実行

## 機能

| タブ | 内容 |
|---|---|
| キーワード | `keywords.txt` の閲覧・編集（保存には GitHub PAT が必要） |
| 投稿一覧 | `hits.json` / `status.json`（監視実行で更新） |

キーワード保存用 PAT: `repo` スコープの fine-grained または classic token をブラウザに保存（localStorage）。サーバーには送りません。
