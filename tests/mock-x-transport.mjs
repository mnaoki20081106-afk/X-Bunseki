// Offline only: keep the actual SDK's searchPage/getTweet and parsers, replacing
// its transaction bootstrap/HTTP boundary. No credentials or live X requests.
import { XClient, QID } from 'x-agent-sdk';
import { appendFileSync } from 'node:fs';
import { tweet, timeline } from './collector-fixtures.mjs';
XClient.prototype.request = async function(method, action, variables) {
  if(process.env.TEST_SCENARIO === 'bootstrap') throw new Error('could not locate ondemand.s index in home HTML');
  const url = new URL(`https://x.com/i/api/graphql/${QID[action]}/${action}`);
  // Mirror the SDK: POST sends a JSON body, GET sends URL parameters.
  if (method === 'POST') {
    const body = JSON.stringify({ variables, queryId: QID[action] });
    return (await this.fetchWithTracking(url.toString(), { method, body })).json();
  }
  url.searchParams.set('variables', JSON.stringify(variables));
  return (await this.fetchWithTracking(url.toString(), { method })).json();
};
globalThis.fetch = async (url, init = {}) => {
  const parsed = new URL(url);
  const operation = parsed.pathname.split('/').at(-1);
  const method = init.method || 'GET';
  const variables = parsed.searchParams.has('variables')
    ? JSON.parse(parsed.searchParams.get('variables'))
    : JSON.parse(init.body).variables;
  const queryId = parsed.pathname.split('/').at(-2);
  appendFileSync('requests.jsonl', JSON.stringify({ operation, method, queryId, variables }) + '\n');
  const mode = process.env.TEST_SCENARIO;
  // X now 404s SearchTimeline over GET; the reverse case checks the fallback.
  if (operation === 'SearchTimeline' &&
      ((mode === 'search_get_404' && method === 'GET') || (mode === 'search_post_404' && method === 'POST') ||
       (mode === 'search_qid_rotated' && !(method === 'POST' && queryId === '3fEctOs2-FvTB7_mvm1hrQ')) ||
       mode === 'search_all_404')) {
    return new Response('', { status: 404 });
  }
  if (mode === 'search_suspended' && operation === 'SearchTimeline') {
    return Response.json({ errors: [{ message: 'InvalidArgument: ProductDisabled: Closed gate stopped execution of the pipeline (LatestTabSrpDenySuspendedUsers)' }] });
  }
  // POST accepted only with one queryId; others answer 200 + error payload.
  if (mode === 'search_error_payload' && operation === 'SearchTimeline' &&
      !(method === 'POST' && queryId === 'gkjsKepM6gl_HmFWoWKfgg')) {
    return Response.json({ errors: [{ code: 0, message: 'Query: Unspecified' }] });
  }
  if (mode === 'auth') return Response.json({ errors: [{ code: 89, message: 'SENSITIVE_VALUE' }] });
  if (mode === 'forbidden') return Response.json({}, { status: 403 });
  if (mode === 'rate' || (mode === 'partial' && variables.rawQuery === 'second')) {
    return Response.json({}, { status: 429, headers: { 'retry-after': '900' } });
  }
  if (mode === 'schema') return Response.json({ data: { new_schema: [] } });
  if (mode === 'schema_once_then_success' && operation === 'SearchTimeline' && variables.rawQuery === 'second') {
    return Response.json({ data: { new_schema: [] } });
  }
  if (mode === 'detail_schema_then_success' && operation === 'TweetDetail' && variables.focalTweetId === '456') {
    return Response.json({ data: { new_schema: [] } });
  }
  if (mode === 'network') throw new Error('SENSITIVE_VALUE');
  if (mode === 'stale' && operation === 'TweetDetail') return Response.json({}, { status: 503 });
  if (mode === 'stale' || mode === 'empty') return Response.json(timeline([], operation));
  const t = tweet(operation === 'TweetDetail' ? variables.focalTweetId : '123');
  if (mode === 'missing_views' ||
      (['detail_incomplete', 'detail_incomplete_many'].includes(mode) && operation === 'TweetDetail') ||
      (mode === 'search_incomplete_then_success' && operation === 'SearchTimeline' && variables.rawQuery === 'second')) {
    delete t.views;
  }
  return Response.json(timeline([t], operation));
};
