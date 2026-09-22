# Signal Filter legacy viewer

The old standalone keyword editor is retired.

`admin-site/index.html` is now **read-only** and only displays public monitoring results. It no longer contains the keyword Worker URL or any browser-side POST path.

Keyword administration now belongs to the authenticated parent application:

- parent repository: `mnaoki20081106-afk/Tiktok-generater-Public`
- route: `/admin/x-monitor`
- authorization: existing Supabase session + server-side `ADMIN_EMAILS`
- write path: server-only GitHub Contents API to the existing `keywords*.txt` files

## Required external cleanup

The legacy Cloudflare write Worker was deployed outside this repository, so repository changes cannot disable that deployed endpoint. After the parent application has `X_BUNSEKI_GITHUB_TOKEN` configured, disable/delete the old `keywordadminworker` Worker (or remove its public POST route) in Cloudflare.

If this viewer is still deployed separately, redeploy this read-only `index.html` so the old editor UI disappears.
