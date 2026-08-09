/**
 * cloudflare_worker.js
 *
 * LINEからのWebhookを受け取り、「動作確認」というメッセージが来たら
 * GitHubリポジトリのstatus.jsonを読みに行って、その内容を返信する。
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
 *   7. デプロイ後に表示される Worker の URL(https://xxxx.workers.dev)をコピー
 *   8. LINE Developers Console → 該当チャネルの「Messaging API」タブ →
 *      「Webhook URL」にその URL を貼り付けて保存
 *   9. 「Webhookの利用」トグルをONにする
 */

export default {
  async fetch(request, env) {
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

async function buildStatusReply(env) {
  try {
    // GitHubのraw URLはCDNキャッシュされることがあるため、
    // キャッシュを回避するためのダミーパラメータを付与する
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

    if (status.top5_by_likes && status.top5_by_likes.length > 0) {
      lines.push("");
      lines.push("直近のいいね数上位:");
      for (const p of status.top5_by_likes) {
        lines.push(
          `・@${p.author} いいね${p.likes} 引用${p.quotes} BM${p.bookmarks}`
        );
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
