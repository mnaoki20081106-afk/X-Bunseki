/**
 * cloudflare_ogp_redirect_worker.js  — 「直接発火」Worker
 *
 * X に貼ったカード(OGP 画像)をタップしたら、自サイト(post-link.net の
 * サイト作成ページ)を経由せず、埋め込んだリンクに直接飛ばす。
 *
 * X のカードのクリック先は「ツイートに貼った URL」に固定で、og:url では
 * 変えられない。そこでこの Worker の URL を X に貼り、
 *   - X / Facebook / Discord などのクローラ(User-Agent 判定)には
 *     post-link.net のページ HTML をそのまま返す → カードの画像・タイトルは今まで通り
 *   - 人間のブラウザには 302 で埋め込みリンクへ転送 → ワンタップで到達
 *
 * 貼る URL の形:
 *   https://<worker>/?p=<post-link.net のページURL>&u=<埋め込んだリンク>
 *   例: https://x-go.example.workers.dev/?p=https://post-link.net/s/abc123&u=https://example.com/item/1
 *
 * 環境変数(Settings → Variables):
 *   SOURCE_HOSTS   : OGP を取りに行ってよいホスト(カンマ区切り)。
 *                    既定 "post-link.net,www.post-link.net"
 *   DEST_ALLOWLIST : 転送先として許可するホスト(カンマ区切り)。
 *                    未設定なら http/https の任意ホストを許可。
 *                    オープンリダイレクタ扱いされないよう、運用では設定推奨。
 *                    "*.example.com" のようにサブドメイン一括指定可。
 *
 * デプロイ: Workers & Pages → Create Worker → このファイルを貼り付け → Deploy
 */

const DEFAULT_SOURCE_HOSTS = "post-link.net,www.post-link.net";

// OGP を読みに来る代表的なクローラ。JS を実行しないので 302 ではなく HTML を返す。
export const CRAWLER_UA = /Twitterbot|facebookexternalhit|Facebot|Discordbot|Slackbot|LinkedInBot|line-poker|TelegramBot|WhatsApp|Pinterest|Applebot|Googlebot|bingbot/i;

export function isCrawler(userAgent) {
  return CRAWLER_UA.test(userAgent || "");
}

function parseHostList(value, fallback = "") {
  return (value || fallback)
    .split(",")
    .map((h) => h.trim().toLowerCase())
    .filter(Boolean);
}

function hostAllowed(hostname, allow) {
  const h = hostname.toLowerCase();
  return allow.some((pat) => {
    if (pat.startsWith("*.")) {
      const base = pat.slice(2);
      return h === base || h.endsWith("." + base);
    }
    return h === pat;
  });
}

/**
 * クエリ p / u を検証して { source, dest } を返す。問題があれば { error } を返す。
 */
export function resolveTargets(searchParams, env = {}) {
  const sourceHosts = parseHostList(env.SOURCE_HOSTS, DEFAULT_SOURCE_HOSTS);
  const destAllow = parseHostList(env.DEST_ALLOWLIST);

  const rawSource = (searchParams.get("p") || "").trim();
  const rawDest = (searchParams.get("u") || "").trim();
  if (!rawSource) return { error: "p (OGP 元ページ URL) がありません" };
  if (!rawDest) return { error: "u (転送先 URL) がありません" };

  let source;
  let dest;
  try { source = new URL(rawSource); } catch (_) { return { error: "p が URL として不正です" }; }
  try { dest = new URL(rawDest); } catch (_) { return { error: "u が URL として不正です" }; }

  if (source.protocol !== "https:") return { error: "p は https のみ許可" };
  if (!hostAllowed(source.hostname, sourceHosts)) return { error: `p のホストが許可されていません: ${source.hostname}` };

  if (dest.protocol !== "https:" && dest.protocol !== "http:") return { error: "u は http/https のみ許可" };
  if (dest.username || dest.password) return { error: "u に認証情報は含められません" };
  if (destAllow.length && !hostAllowed(dest.hostname, destAllow)) {
    return { error: `u のホストが許可されていません: ${dest.hostname}` };
  }
  return { source, dest };
}

export async function handle(request, env = {}, fetchImpl = fetch) {
  const url = new URL(request.url);

  if (url.pathname !== "/") return new Response("Not Found", { status: 404 });

  const targets = resolveTargets(url.searchParams, env);
  if (targets.error) {
    return new Response(targets.error, {
      status: 400,
      headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" },
    });
  }

  const ua = request.headers.get("User-Agent") || "";

  if (!isCrawler(ua)) {
    // 人間: 自サイトを表示せず即転送。302 にしてブラウザに永続キャッシュさせない。
    return new Response(null, {
      status: 302,
      headers: {
        Location: targets.dest.toString(),
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
      },
    });
  }

  // クローラ: post-link.net のページ HTML を代理で返す(OGP はそのまま)。
  let upstream;
  try {
    upstream = await fetchImpl(targets.source.toString(), {
      headers: { "User-Agent": ua, Accept: "text/html" },
      redirect: "follow",
      cf: { cacheTtl: 300 },
    });
  } catch (e) {
    return new Response(`upstream fetch failed: ${String(e).slice(0, 200)}`, { status: 502 });
  }
  if (!upstream.ok) {
    return new Response(`upstream HTTP ${upstream.status}`, { status: 502 });
  }
  const html = await upstream.text();
  return new Response(html, {
    status: 200,
    headers: {
      "Content-Type": upstream.headers.get("Content-Type") || "text/html; charset=utf-8",
      "Cache-Control": "public, max-age=300",
      "X-Robots-Tag": "noindex",
    },
  });
}

export default {
  async fetch(request, env) {
    return handle(request, env);
  },
};
