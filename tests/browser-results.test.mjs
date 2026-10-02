import {test} from 'node:test';
import assert from 'node:assert/strict';
import {normalizeBrowserResults} from '../browser_results.mjs';
import {tweet, timeline} from './collector-fixtures.mjs';

test('browser measurements use the same exact-metric validator and preserve source',()=>{
  const result=normalizeBrowserResults([{operation:'SearchTimeline',status:200,
    source:'viral_search',body:timeline([tweet()])}]);
  assert.equal(result.health.status,'success');
  assert.equal(result.posts.length,1);
  assert.equal(result.posts[0].discovery_source,'viral_search');
  assert.equal(result.posts[0].impressions,50000);
});
test('browser login failures never report a successful empty feed',()=>{
  assert.equal(normalizeBrowserResults([], 'session_expired').health.status,'session_expired');
  assert.equal(normalizeBrowserResults([]).health.status,'collection_failed');
  assert.equal(normalizeBrowserResults([{operation:'SearchTimeline',status:429,body:{}}]).health.status,'rate_limited');
});
test('browser cannot turn missing metrics into a fresh observation',()=>{
  const post=tweet();delete post.views;
  const result=normalizeBrowserResults([{operation:'SearchTimeline',status:200,body:timeline([post])}]);
  assert.equal(result.posts.length,0);
  assert.equal(result.health.invalid_tweets,1);
  assert.equal(result.health.status,'incomplete_observations');
});
