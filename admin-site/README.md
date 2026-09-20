# Signal Filter（キーワード管理サイト）

本番URL: https://keywords-administration.c53gftun651-tiktok.workers.dev/

## 投稿一覧タブを反映する手順

このディレクトリの `index.html` を Cloudflare Worker（静的）に再デプロイしてください。

1. https://dash.cloudflare.com → Workers & Pages
2. `keywords-administration`（または該当Worker）を開く
3. **Edit code** / Assets の `index.html` を `admin-site/index.html` の内容で置き換え
4. **Deploy**

API（キーワード保存）は従来どおり:
`https://keywordadminworker.c53gftun651-tiktok.workers.dev/keywords`

投稿一覧は GitHub の `hits.json` を読みます（監視実行後に更新）。
