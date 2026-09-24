// Compatibility boundary for X's undocumented web responses. Never infer a
// missing measurement as zero, and never traverse quoted/retweeted posts.
export class CollectorError extends Error {
  constructor(code) { super(code); this.code = code; }
}

export function responseError(status, body) {
  const codes = (Array.isArray(body?.errors) ? body.errors : []).map(e => Number(e.code));
  if (status === 401 || codes.some(c => [32, 89, 215].includes(c))) return 'session_expired';
  if (codes.some(c => [64, 326].includes(c))) return 'account_restricted';
  if (status === 429 || codes.includes(88)) return 'rate_limited';
  if (status === 403) return 'access_denied';
  if ([400, 404, 410, 422].includes(status)) return 'schema_changed';
  if (status >= 500) return 'upstream_error';
  if (status >= 400 || codes.length || body?.errors?.length) return 'request_failed';
  return null;
}

function count(value) {
  if (value == null || value === '' || typeof value === 'boolean') return null;
  const n = Number(value);
  return Number.isSafeInteger(n) && n >= 0 ? n : null;
}

export function normalizeTweet(value) {
  let r = value;
  for (let i = 0; i < 6 && r && !r.legacy; i++) r = r.tweet ?? r.result;
  const lg = r?.legacy;
  const id = r?.rest_id ?? lg?.id_str;
  if (!lg || typeof id !== 'string' || !/^\d+$/.test(id)) return null;
  const user = r.core?.user_results?.result;
  const postedAt = lg.created_at;
  if (!postedAt || !Number.isFinite(Date.parse(postedAt))) return null;
  const metrics = {
    likes: count(lg.favorite_count), retweets: count(lg.retweet_count),
    replies: count(lg.reply_count), quotes: count(lg.quote_count),
    bookmarks: count(lg.bookmark_count), impressions: count(r.views?.count),
  };
  // The pipeline uses all six metrics for velocity and learning. An incomplete
  // observation must be skipped, rather than poisoning its time series.
  if (Object.values(metrics).some(n => n === null)) return null;
  return {
    post_id: id,
    author_handle: user?.legacy?.screen_name || user?.core?.screen_name || '',
    author_name: user?.legacy?.name || user?.core?.name || '',
    posted_at: new Date(postedAt).toISOString(),
    text_snippet: (lg.full_text || '').slice(0, 280),
    url: `https://x.com/i/status/${id}`,
    ...metrics,
  };
}

export function parseTimeline(body, operation) {
  const root = operation === 'SearchTimeline'
    ? body?.data?.search_by_raw_query?.search_timeline?.timeline
    : body?.data?.threaded_conversation_with_injections_v2;
  if (!Array.isArray(root?.instructions)) throw new CollectorError('schema_changed');
  const tweets = new Map();
  let candidates = 0, invalid = 0, unavailable = 0;
  function visit(node, depth = 0) {
    if (!node || typeof node !== 'object' || depth > 12) return;
    if (node.tweet_results) {
      const result = node.tweet_results.result;
      if (['TweetTombstone', 'TweetUnavailable'].includes(result?.__typename)) {
        unavailable++;
        return;
      }
      candidates++;
      const tweet = normalizeTweet(result);
      if (tweet) tweets.set(tweet.post_id, tweet);
      else invalid++;
      return;
    }
    // Only timeline containers: do not recursively collect quoted tweets,
    // user metadata, recommendations, or other unrelated payload fields.
    for (const key of ['entries', 'entry', 'content', 'itemContent', 'items', 'moduleItems', 'item']) {
      const value = node[key];
      if (Array.isArray(value)) value.forEach(v => visit(v, depth + 1));
      else visit(value, depth + 1);
    }
  }
  for (const instruction of root.instructions) visit(instruction);
  return { tweets: [...tweets.values()], candidates, invalid, unavailable };
}

// A rate limit stops only that operation. Authentication/account failures stop
// all operations. Search schema failures stop SearchTimeline, while a single
// TweetDetail schema failure remains post-local so later watchlist items can run.
// A fresh scheduled run can retry; no rapid retry loop here.
export function createRequestGuard({ schemaFailureThreshold = 3 } = {}) {
  let globalError = null;
  const blocked = new Map();
  const schemaStreak = new Map();
  const failures = {};
  return {
    failures,
    check(operation) {
      const code = globalError || blocked.get(operation);
      if (code) throw new CollectorError(code);
    },
    succeed(operation) {
      // A valid response between mismatches is strong evidence that X has not
      // globally changed the operation schema. Only consecutive mismatches are
      // allowed to trip the circuit breaker.
      schemaStreak.set(operation, 0);
    },
    fail(operation, code) {
      failures[code] = (failures[code] || 0) + 1;
      if (['session_expired', 'account_restricted', 'access_denied'].includes(code)) {
        globalError = code;
        return;
      }
      if (code === 'rate_limited') {
        blocked.set(operation, code);
        return;
      }
      if (code === 'schema_changed' && operation === 'SearchTimeline') {
        const streak = (schemaStreak.get(operation) || 0) + 1;
        schemaStreak.set(operation, streak);
        if (streak >= schemaFailureThreshold) blocked.set(operation, code);
        return;
      }
      if (code !== 'schema_changed') schemaStreak.set(operation, 0);
    },
    get primaryError() {
      if (globalError) return globalError;
      const blockedCodes = [...blocked.values()];
      if (blockedCodes.includes('rate_limited')) return 'rate_limited';
      if (blockedCodes.includes('schema_changed')) return 'schema_changed';
      const ordinary = ['upstream_error', 'request_failed', 'network_error']
        .find(code => failures[code]);
      if (ordinary) return ordinary;
      // One isolated shape mismatch is reported as a generic partial request
      // failure. Calling it a global X schema change after a single response is
      // misleading when later SearchTimeline responses still parse normally.
      return failures.schema_changed ? 'request_failed' : null;
    },
  };
}
