// Offline only: keep the actual SDK's searchPage/getTweet and parsers, replacing
// its transaction bootstrap/HTTP boundary. No credentials or live X requests.
import { XClient, QID } from 'x-agent-sdk';
import { appendFileSync } from 'node:fs';
import { tweet, timeline } from './collector-fixtures.mjs';
XClient.prototype.request = async function(method, action, variables) {
  const url = new URL(`https://x.com/i/api/graphql/${QID[action]}/${action}`);
  url.searchParams.set('variables', JSON.stringify(variables));
  return (await this.fetchWithTracking(url.toString(), { method })).json();
};
globalThis.fetch = async url => {
  const parsed = new URL(url);
  const operation = parsed.pathname.split('/').at(-1);
  const variables = JSON.parse(parsed.searchParams.get('variables'));
  appendFileSync('requests.jsonl', JSON.stringify({ operation, variables }) + '\n');
  const mode = process.env.TEST_SCENARIO;
  if (mode === 'auth') return Response.json({ errors: [{ code: 89, message: 'SENSITIVE_VALUE' }] });
  if (mode === 'forbidden') return Response.json({}, { status: 403 });
  if (mode === 'rate' || (mode === 'partial' && variables.rawQuery === 'second')) {
    return Response.json({}, { status: 429, headers: { 'retry-after': '900' } });
  }
  if (mode === 'schema') return Response.json({ data: { new_schema: [] } });
  if (mode === 'detail_schema_then_success' && operation === 'TweetDetail' && variables.focalTweetId === '456') {
    return Response.json({ data: { new_schema: [] } });
  }
  if (mode === 'network') throw new Error('SENSITIVE_VALUE');
  if (mode === 'stale' && operation === 'TweetDetail') return Response.json({}, { status: 503 });
  if (mode === 'stale' || mode === 'empty') return Response.json(timeline([], operation));
  const t = tweet(operation === 'TweetDetail' ? variables.focalTweetId : '123');
  if (mode === 'missing_views' || (mode === 'detail_incomplete' && operation === 'TweetDetail')) delete t.views;
  return Response.json(timeline([t], operation));
};
