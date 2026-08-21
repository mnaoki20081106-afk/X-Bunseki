/**
 * cloudflare_worker.js
 *
 * 1. LINEからのWebhookを受け取り、「動作確認」というメッセージが来たら
 *    GitHubリポジトリのstatus.jsonを読みに行って、その内容を返信する。
 * 2. iPhoneのショートカット(位置情報オートメーション)から「学校にいる/
 *    いない」の状態を受け取り、Cloudflare KVに保存する(/school-status)。
 *    GitHub Actions側(pushover_notifier.py)がこれを読みに来て、学校に
 *    いる間だけPushoverの通知優先度を下げる(おやすみモードに従わせる)。
 *
 * デプロイ方法(iPadのSafariだけで完結します):
 *   1. https://dash.cloudflare.com で無料アカウント作成
 *   2. 左メニューから「Workers & Pages」→「Create」→「Create Worker」
 *   3. 適当な名前をつけて「Deploy」(一旦空のWorkerが作られる)
 *   4. 作成されたWorkerの「Edit code」(コードを編集)を開く
 *   5. デフォルトのコードを全部消して、このファイルの中身を貼り付けて「Deploy」
 *   6. Workerの「Settings」→「Variables」で以下の環境変数(Secret)を設定:
 *        LINE_CHANNEL_ACCESS_TOKEN : LINEのチャネルアクセストークン(既存のものと同じ)
 *        STATUS_JSON_URL           : 例 https://raw.githubusercontent.com/{ユーザー名}/{リポジトリ名}/main/status.json
 *        SCHOOL_STATUS_SECRET      : 好きな適当な長い文字列(ショートカット側とGitHub Actions側で共有する合言葉)
 *   7. Workerの「Settings」→「Bindings」→「Add binding」→「KV Namespace」で
 *      新規KV Namespace(名前は何でもいい、例: school-status)を作成し、
 *      変数名 SCHOOL_STATUS としてこのWorkerに紐付ける
 *   8. デプロイ後に表示される Worker の URL(https://xxxx.workers.dev)をコピー
 *   9. LINE Developers Console → 該当チャネルの「Messaging API」タブ →
 *      「Webhook URL」にその URL を貼り付けて保存
 *   10. 「Webhookの利用」トグルをONにする
 */

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/school-status") {
      return handleSchoolStatus(request, env);
    }

    if (request.method !== "POST") {
      return new Response("OK", { status: 200 });
    }

    let body;
    try {
      body = await request.json();
    } catch (e) {
      return new Response("Bad Request", { status: 400 });
    }

    const events = body.events || [];

    for (const event of events) {
      if (
        event.type === "message" &&
        event.message &&
        event.message.type === "text"
      ) {
        const text = event.message.text.trim();

        if (text === "動作確認") {
          const replyText = await buildStatusReply(env);
          await replyToLine(env, event.replyToken, replyText);
        }
      }
    }

    // LINEには常に200 OKを返す(仕様上必須)
    return new Response("OK", { status: 200 });
  },
};

const SCHOOL_STATUS_KV_KEY = "at_school";

async function handleSchoolStatus(request, env) {
  const authHeader = request.headers.get("Authorization") || "";
  const expected = `Bearer ${env.SCHOOL_STATUS_SECRET}`;
  if (!env.SCHOOL_STATUS_SECRET || authHeader !== expected) {
    return new Response("Unauthorized", { status: 401 });
  }

  if (request.method === "POST") {
    let body;
    try {
      body = await request.json();
    } catch (e) {
      return new Response("Bad Request", { status: 400 });
    }
    const record = {
      at_school: Boolean(body.at_school),
      updated_at: new Date().toISOString(),
    };
    await env.SCHOOL_STATUS.put(SCHOOL_STATUS_KV_KEY, JSON.stringify(record));
    return new Response(JSON.stringify(record), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  if (request.method === "GET") {
    const stored = await env.SCHOOL_STATUS.get(SCHOOL_STATUS_KV_KEY);
    const record = stored ? JSON.parse(stored) : { at_school: false, updated_at: null };
    return new Response(JSON.stringify(record), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  return new Response("Method Not Allowed", { status: 405 });
}

async function buildStatusReply(env) {
  try {
    const url = `${env.STATUS_JSON_URL}?t=${Date.now()}`;
    const resp = await fetch(url, { cf: { cacheTtl: 0 } });

    if (!resp.ok) {
      return `⚠️ status.jsonの取得に失敗しました(HTTP ${resp.status})。まだ一度も実行されていないか、リポジトリの設定を確認してください。`;
    }

    const status = await resp.json();
    return formatStatus(status);
  } catch (e) {
    return `⚠️ 動作確認中にエラーが発生しました: ${e.message}`;
  }
}

function formatStatus(status) {
  const updatedAt = new Date(status.updated_at);
  const now = new Date();
  const diffMinutes = Math.round((now - updatedAt) / 60000);

  const lines = [];

  if (status.status === "success") {
    lines.push("✅ 正常に動作しています");
  } else if (status.status === "session_expired") {
    lines.push("⚠️ ログインセッションが切れています");
  } else if (status.status === "error") {
    lines.push("❌ 直近の実行でエラーが発生しました");
  } else {
    lines.push("❓ 状態不明");
  }

  lines.push(`最終実行: ${diffMinutes}分前`);

  if (status.status === "success") {
    lines.push(`収集件数: ${status.posts_scanned}件`);
    lines.push(`通知件数: ${status.notified_count}件`);

    if (status.top5_by_likes) {
      lines.push("");
      lines.push("【いいね数 上位(キーワード一致のみ)】");
      if (status.top5_by_likes.length > 0) {
        for (const p of status.top5_by_likes) {
          const keywordNote = p.matched_keyword ? ` [${p.matched_keyword}]` : "";
          lines.push(
            `・@${p.author} いいね${p.likes} 引用${p.quotes} BM${p.bookmarks}${keywordNote}`
          );
        }
      } else {
        lines.push("(キーワードに一致する投稿は今回ありませんでした)");
      }
    }

    if (status.top5_by_progress) {
      lines.push("");
      lines.push("【通知条件への到達度 上位(キーワード一致のみ)】");
      if (status.top5_by_progress.length > 0) {
        for (const p of status.top5_by_progress) {
          const keywordNote = p.matched_keyword ? ` [${p.matched_keyword}]` : "";
          lines.push(
            `・@${p.author} 達成度${p.progress_percent}% (いいね${p.likes} 引用${p.quotes} BM${p.bookmarks})${keywordNote}`
          );
        }
      } else {
        lines.push("(キーワードに一致する投稿は今回ありませんでした)");
      }
    }
  }

  if (status.error_message) {
    lines.push("");
    lines.push(`エラー内容: ${status.error_message}`);
  }

  return lines.join("\n");
}

async function replyToLine(env, replyToken, text) {
  await fetch("https://api.line.me/v2/bot/message/reply", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${env.LINE_CHANNEL_ACCESS_TOKEN}`,
    },
    body: JSON.stringify({
      replyToken,
      messages: [{ type: "text", text }],
    }),
  });
}
