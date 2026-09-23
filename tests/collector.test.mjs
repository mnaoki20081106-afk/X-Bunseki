import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { createRequestGuard, normalizeTweet, parseTimeline, responseError } from '../x_response.mjs';
import { discoverQueryIds, extractQueryIds, queryIdOverrides } from '../x_query_ids.mjs';
import { tweet, timeline } from './collector-fixtures.mjs';

test('normalizes wrapped tweets, core user and exact metrics', () => {
  const t = normalizeTweet({ result: { tweet: tweet() } });
  assert.equal(t.author_name, 'Author');
  assert.equal(t.impressions, 50000);
  assert.equal(t.quotes, 5);
  assert.equal(t.posted_at, '2026-09-22T10:00:00.000Z');
  const zero = tweet(); zero.views.count = '0';
  assert.equal(normalizeTweet(zero).impressions, 0);
  for (const invalid of [null, '', -1, Infinity, true, 'bad']) {
    const t = tweet(); t.views.count = invalid;
    assert.equal(normalizeTweet(t), null);
  }
  const undated = tweet(); delete undated.legacy.created_at;
  assert.equal(normalizeTweet(undated), null, 'never invent posting time');
});

test('supports single-entry and module timeline variants without collecting quotes', () => {
  const t = tweet(); t.quoted_status_result = { result: tweet('999') };
  const body = timeline([]);
  body.data.search_by_raw_query.search_timeline.timeline.instructions = [
    { entry: { content: { itemContent: { tweet_results: { result: t } } } } },
    { moduleItems: [{ item: { itemContent: { tweet_results: { result: tweet('456') } } } }] },
  ];
  assert.deepEqual(parseTimeline(body, 'SearchTimeline').tweets.map(t => t.post_id), ['123', '456']);
  assert.throws(() => parseTimeline({}, 'SearchTimeline'), /schema_changed/);
  assert.equal(parseTimeline(timeline([]), 'SearchTimeline').tweets.length, 0);
  const removed = parseTimeline(timeline([{ __typename: 'TweetTombstone' }], 'TweetDetail'), 'TweetDetail');
  assert.equal(removed.unavailable, 1);
  assert.equal(removed.invalid, 0, 'deleted posts must not stop all detail requests');
});

test('distinguishes auth, restrictions, rate limits and request failures', () => {
  assert.equal(responseError(200, { errors: [{ code: 89 }] }), 'session_expired');
  assert.equal(responseError(403, {}), 'access_denied');
  assert.equal(responseError(200, { errors: [{ code: 326 }] }), 'account_restricted');
  assert.equal(responseError(429, {}), 'rate_limited');
  assert.equal(responseError(200, { errors: [{ code: 88 }] }), 'rate_limited');
  assert.equal(responseError(404, {}), 'schema_changed');
  assert.equal(responseError(503, {}), 'upstream_error');
});

test('rate guard isolates operations; auth stops both', () => {
  const guard = createRequestGuard();
  guard.fail('SearchTimeline', 'rate_limited');
  assert.throws(() => guard.check('SearchTimeline'), /rate_limited/);
  guard.check('TweetDetail');
  guard.fail('TweetDetail', 'session_expired');
  assert.throws(() => guard.check('SearchTimeline'), /session_expired/);
  assert.throws(() => guard.check('TweetDetail'), /session_expired/);
});

test('discovers bounded read-only query IDs without executing bundle or forwarding cookies', async () => {
  const source = '{queryId:"new_search_1234",operationName:"SearchTimeline",operationType:"query",metadata:{featureSwitches:[]}};' +
    '{operationName:"TweetDetail",queryId:"new_detail_1234"};' +
    '{queryId:"create_tweet_123",operationName:"CreateTweet"};';
  assert.deepEqual(extractQueryIds(source), { SearchTimeline: 'new_search_1234', TweetDetail: 'new_detail_1234' });
  assert.throws(() => queryIdOverrides('{"CreateTweet":"create_tweet_123"}'));
  assert.throws(() => queryIdOverrides('{"SearchTimeline":"https://evil"}'));
  const calls = [];
  const result = await discoverQueryIds(async (url, options) => {
    calls.push(url);
    assert.equal(options.headers, undefined);
    assert.equal(options.redirect, 'error');
    return new Response(url === 'https://x.com/'
      ? '<script src="https://abs.twimg.com/responsive-web/client-web/main.abcdef.js"></script><script src="https://evil/main.js"></script>'
      : source);
  });
  assert.equal(result.SearchTimeline, 'new_search_1234');
  assert.equal(calls.length, 2);
});

function collect(scenario) {
  const dir = mkdtempSync(join(tmpdir(), 'x-collector-'));
  try {
    writeFileSync(join(dir, 'storage_state.json'), JSON.stringify({ cookies: [
      { name: 'auth_token', value: 'FAKE_TOKEN' }, { name: 'ct0', value: 'FAKE_CSRF' },
    ] }));
    writeFileSync(join(dir, '.x-agent-queries.json'), JSON.stringify(['first', 'second', 'third'].map(query => ({ query, product: 'Latest', limit: 20, source: 'keyword' }))));
    writeFileSync(join(dir, '.x-agent-watchlist.json'), JSON.stringify([{ post_id: '456', last_impressions: 90000 }]));
    const result = spawnSync(process.execPath, ['--import', fileURLToPath(new URL('./mock-x-transport.mjs', import.meta.url)), fileURLToPath(new URL('../x_collector.mjs', import.meta.url))], {
      cwd: dir, encoding: 'utf8', timeout: 15000,
      env: { ...process.env, TEST_SCENARIO: scenario, SEARCH_CONCURRENCY: '1', DETAIL_CONCURRENCY: '1',
        X_QUERY_ID_DISCOVERY: 'off', X_GRAPHQL_QUERY_IDS: '{}', X_SESSION_STATE_PATH: 'storage_state.json' },
    });
    assert.equal(result.status, 0, result.stderr);
    assert.doesNotMatch(result.stderr, /SENSITIVE_VALUE|FAKE_TOKEN|FAKE_CSRF/);
    return { ...JSON.parse(result.stdout), requests: readFileSync(join(dir, 'requests.jsonl'), 'utf8').trim().split('\n').map(JSON.parse) };
  } finally { rmSync(dir, { recursive: true, force: true }); }
}

test('full collector keeps successful search + fresh watchlist metrics', () => {
  const result = collect('success');
  assert.equal(result.health.status, 'success');
  assert.deepEqual(result.posts.map(p => p.post_id), ['123', '456']);
  assert.equal(result.posts[0].discovery_query_hits, 3);
  assert.equal(result.posts[1].impressions, 50000, 'fresh value replaces watchlist history');
});
test('failed detail never emits stale watchlist observation', () => {
  const result = collect('stale');
  assert.deepEqual(result.posts, []);
  assert.equal(result.health.status, 'upstream_error');
});
test('partial success keeps valid data and reports degraded', () => {
  const result = collect('partial');
  assert.equal(result.health.status, 'degraded');
  assert.equal(result.health.error_code, 'rate_limited');
  assert.equal(result.requests.filter(r => r.operation === 'SearchTimeline').length, 2);
  assert.equal(result.posts.length, 2);
});
for (const [scenario, status] of [['auth', 'session_expired'], ['forbidden', 'access_denied'], ['rate', 'rate_limited'], ['schema', 'schema_changed'], ['missing_views', 'schema_changed'], ['network', 'network_error']]) {
  test(`full collector detects ${scenario}`, () => {
    const result = collect(scenario);
    assert.equal(result.health.status, status);
    assert.deepEqual(result.posts, []);
    if (['auth', 'forbidden'].includes(scenario)) assert.equal(result.requests.length, 1);
    if (scenario === 'rate') assert.equal(result.requests.length, 2, 'one search and one detail, no rapid retries');
  });
}
