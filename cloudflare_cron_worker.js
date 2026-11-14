/**
 * cloudflare_cron_worker.js
 *
 * 15分おきに GitHub Actions の「SNS Buzz Monitor」(monitor.yml) を起動する
 * Cloudflare Worker。monitor.yml の GitHub schedule(毎時08分)は非常用で、
 * 主系はこの Worker の Cron Trigger。
 *
 * デプロイ方法(iPad の Safari だけで完結):
 *   1. https://dash.cloudflare.com → Workers & Pages → Create → Create Worker
 *   2. 名前は x-bunseki-monitor-cron(任意)→ Deploy
 *   3. Edit code → このファイルの中身を貼り付けて Deploy
 *   4. Settings → Variables and Secrets:
 *        GITHUB_TOKEN (Secret) : GitHub の Fine-grained PAT。
 *                                対象リポジトリ X-Bunseki、権限 Actions: Read and write
 *        GITHUB_REPO  (Text)   : 省略可。既定 mnaoki20081106-afk/X-Bunseki
 *        GITHUB_REF   (Text)   : 省略可。既定 main
 *   5. Settings → Triggers → Cron Triggers → "*\/15 * * * *" を追加
 *
 * 動作確認: Worker の URL を GET すると設定状況(トークン未設定なら警告)を返す。
 *          POST /run に Authorization: Bearer <RUN_SECRET> を付けると手動起動できる
 *          (RUN_SECRET を Secret として設定した場合のみ)。
 */

const DEFAULT_REPO = "mnaoki20081106-afk/X-Bunseki";
const DEFAULT_REF = "main";
const WORKFLOW_FILE = "monitor.yml";

export function resolveConfig(env) {
  const repo = (env.GITHUB_REPO || DEFAULT_REPO).trim();
  const ref = (env.GITHUB_REF || DEFAULT_REF).trim();
  const token = (env.GITHUB_TOKEN || "").trim();
  const problems = [];
  if (!/^[\w.-]+\/[\w.-]+$/.test(repo)) problems.push(`GITHUB_REPO が owner/repo 形式ではありません: ${repo}`);
  if (!ref) problems.push("GITHUB_REF が空です");
  if (!token) problems.push("GITHUB_TOKEN (Secret) が未設定です");
  return { repo, ref, token, problems };
}

export async function dispatchMonitor(env, fetchImpl = fetch) {
  const cfg = resolveConfig(env);
  if (cfg.problems.length) {
    return { ok: false, status: 0, reason: cfg.problems.join("; ") };
  }
  const url = `https://api.github.com/repos/${cfg.repo}/actions/workflows/${WORKFLOW_FILE}/dispatches`;
  const res = await fetchImpl(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${cfg.token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "x-bunseki-monitor-cron",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: cfg.ref }),
  });
  // GitHub は成功時 204 No Content を返す
  if (res.status === 204) return { ok: true, status: 204, reason: "dispatched" };
  let detail = "";
  try { detail = (await res.text()).slice(0, 300); } catch (_) { /* ignore */ }
  return { ok: false, status: res.status, reason: detail || `HTTP ${res.status}` };
}

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(
      dispatchMonitor(env).then((r) => {
        console.log(JSON.stringify({ at: new Date().toISOString(), ...r }));
      })
    );
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "POST" && url.pathname === "/run") {
      const auth = request.headers.get("Authorization") || "";
      if (!env.RUN_SECRET || auth !== `Bearer ${env.RUN_SECRET}`) {
        return new Response("Unauthorized", { status: 401 });
      }
      const r = await dispatchMonitor(env);
      return Response.json(r, { status: r.ok ? 200 : 502 });
    }

    const cfg = resolveConfig(env);
    const body = {
      worker: "x-bunseki-monitor-cron",
      repo: cfg.repo,
      ref: cfg.ref,
      workflow: WORKFLOW_FILE,
      token_configured: Boolean(cfg.token),
      problems: cfg.problems,
    };
    return Response.json(body, { status: cfg.problems.length ? 503 : 200 });
  },
};
