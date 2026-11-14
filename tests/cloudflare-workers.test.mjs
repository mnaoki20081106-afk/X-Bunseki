import test from "node:test";
import assert from "node:assert/strict";

import cron, { resolveConfig, dispatchMonitor } from "../cloudflare_cron_worker.js";
import ogp, { isCrawler, resolveTargets, handle } from "../cloudflare_ogp_redirect_worker.js";

// ---------- cron worker ----------

test("cron: token 未設定は problems に出て dispatch しない", async () => {
  const cfg = resolveConfig({});
  assert.equal(cfg.repo, "mnaoki20081106-afk/X-Bunseki");
  assert.equal(cfg.ref, "main");
  assert.match(cfg.problems.join(" "), /GITHUB_TOKEN/);

  let called = false;
  const r = await dispatchMonitor({}, async () => { called = true; return new Response(null, { status: 204 }); });
  assert.equal(called, false);
  assert.equal(r.ok, false);
});

test("cron: dispatch は monitor.yml に ref 付きで POST し 204 を成功扱い", async () => {
  let seen;
  const r = await dispatchMonitor(
    { GITHUB_TOKEN: "tok", GITHUB_REPO: "o/r", GITHUB_REF: "main" },
    async (url, init) => { seen = { url, init }; return new Response(null, { status: 204 }); }
  );
  assert.equal(r.ok, true);
  assert.equal(seen.url, "https://api.github.com/repos/o/r/actions/workflows/monitor.yml/dispatches");
  assert.equal(seen.init.method, "POST");
  assert.equal(seen.init.headers.Authorization, "Bearer tok");
  assert.deepEqual(JSON.parse(seen.init.body), { ref: "main" });
});

test("cron: GitHub がエラーを返したら ok=false と本文を返す", async () => {
  const r = await dispatchMonitor(
    { GITHUB_TOKEN: "tok" },
    async () => new Response('{"message":"Bad credentials"}', { status: 401 })
  );
  assert.equal(r.ok, false);
  assert.equal(r.status, 401);
  assert.match(r.reason, /Bad credentials/);
});

test("cron: fetch ハンドラは設定状況を返し、token なしは 503", async () => {
  const res = await cron.fetch(new Request("https://w.example/"), {});
  assert.equal(res.status, 503);
  const body = await res.json();
  assert.equal(body.token_configured, false);

  const ok = await cron.fetch(new Request("https://w.example/"), { GITHUB_TOKEN: "x" });
  assert.equal(ok.status, 200);
});

test("cron: /run は RUN_SECRET 一致時のみ起動", async () => {
  const denied = await cron.fetch(new Request("https://w.example/run", { method: "POST" }), { RUN_SECRET: "s" });
  assert.equal(denied.status, 401);
});

// ---------- OGP redirect worker ----------

const PAGE = "https://post-link.net/s/abc123";
const DEST = "https://example.com/item/1";
const mkReq = (ua, p = PAGE, u = DEST) =>
  new Request(`https://x-go.example/?p=${encodeURIComponent(p)}&u=${encodeURIComponent(u)}`, {
    headers: ua ? { "User-Agent": ua } : {},
  });

test("ogp: クローラ判定", () => {
  assert.equal(isCrawler("Twitterbot/1.0"), true);
  assert.equal(isCrawler("facebookexternalhit/1.1"), true);
  assert.equal(isCrawler("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1"), false);
  assert.equal(isCrawler(""), false);
});

test("ogp: 人間には 302 で埋め込みリンクへ", async () => {
  const res = await handle(mkReq("Mozilla/5.0 Safari"), {}, async () => { throw new Error("must not fetch"); });
  assert.equal(res.status, 302);
  assert.equal(res.headers.get("Location"), DEST);
  assert.equal(res.headers.get("Cache-Control"), "no-store");
});

test("ogp: Twitterbot には post-link.net の HTML を代理で返す", async () => {
  let fetched;
  const html = '<html><head><meta property="og:image" content="https://post-link.net/og.png"></head></html>';
  const res = await handle(mkReq("Twitterbot/1.0"), {}, async (url) => {
    fetched = url;
    return new Response(html, { status: 200, headers: { "Content-Type": "text/html; charset=utf-8" } });
  });
  assert.equal(fetched, PAGE);
  assert.equal(res.status, 200);
  assert.equal(await res.text(), html);
  assert.match(res.headers.get("Content-Type"), /text\/html/);
});

test("ogp: 上流が落ちていたら 502", async () => {
  const res = await handle(mkReq("Twitterbot/1.0"), {}, async () => new Response("x", { status: 500 }));
  assert.equal(res.status, 502);
});

test("ogp: p は post-link.net 以外・http を拒否", () => {
  assert.match(resolveTargets(new URLSearchParams({ p: "https://evil.example/", u: DEST })).error, /許可されていません/);
  assert.match(resolveTargets(new URLSearchParams({ p: "http://post-link.net/s/1", u: DEST })).error, /https/);
  assert.match(resolveTargets(new URLSearchParams({ u: DEST })).error, /p /);
});

test("ogp: u は javascript: / 認証情報付きを拒否、DEST_ALLOWLIST を尊重", () => {
  assert.match(resolveTargets(new URLSearchParams({ p: PAGE, u: "javascript:alert(1)" })).error, /http\/https/);
  assert.match(resolveTargets(new URLSearchParams({ p: PAGE, u: "https://a:b@example.com/" })).error, /認証情報/);

  const env = { DEST_ALLOWLIST: "shop.example, *.amazon.co.jp" };
  assert.ok(resolveTargets(new URLSearchParams({ p: PAGE, u: "https://shop.example/x" }), env).dest);
  assert.ok(resolveTargets(new URLSearchParams({ p: PAGE, u: "https://www.amazon.co.jp/dp/1" }), env).dest);
  assert.match(resolveTargets(new URLSearchParams({ p: PAGE, u: "https://evil.example/" }), env).error, /u のホスト/);
});

test("ogp: 不正パラメータは 400、/ 以外は 404", async () => {
  const bad = await handle(new Request("https://x-go.example/?p=nope&u=nope"), {});
  assert.equal(bad.status, 400);
  const nf = await handle(new Request("https://x-go.example/other"), {});
  assert.equal(nf.status, 404);
  assert.equal(typeof ogp.fetch, "function");
});
