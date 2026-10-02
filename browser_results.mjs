import { parseTimeline, responseError } from './x_response.mjs';

// Same measurement validation as the SDK path; only the HTTP producer differs.
export function normalizeBrowserResults(events, navigationError = null) {
  const posts = new Map();
  const health = {status: 'success', error_code: null, search_succeeded: 0,
    search_failed: 0, detail_succeeded: 0, detail_failed: 0, detail_skipped: 0,
    invalid_tweets: 0, search_rate_limit: {limit: null, remaining: null, reset: null}};
  for (const event of events) {
    const search = event.operation === 'SearchTimeline';
    const error = responseError(event.status, event.body);
    if (search && event.rate) health.search_rate_limit = event.rate;
    try {
      if (error) throw new Error(error);
      const parsed = parseTimeline(event.body, event.operation);
      health.invalid_tweets += parsed.invalid;
      if (search && parsed.candidates && !parsed.tweets.length) throw new Error('incomplete_observations');
      const tweets = search ? parsed.tweets : parsed.tweets.filter(p => p.post_id === event.postId);
      if (!search && !tweets.length) { health.detail_skipped++; continue; }
      health[search ? 'search_succeeded' : 'detail_succeeded']++;
      for (const tweet of tweets) {
        const previous = posts.get(tweet.post_id);
        const source = search ? (event.source || 'keyword') : 'watchlist';
        posts.set(tweet.post_id, {...tweet,
          discovery_source: previous?.discovery_source || source,
          discovery_sources: [...new Set([...(previous?.discovery_sources || []), source])],
          discovery_query_hits: (previous?.discovery_query_hits || 0) + (search ? 1 : 0)});
      }
    } catch (e) {
      health[search ? 'search_failed' : 'detail_failed']++;
      health.error_code ||= error || (['schema_changed','incomplete_observations'].includes(e.message) ? e.message : 'collection_failed');
    }
  }
  health.error_code ||= navigationError;
  if (!health.search_succeeded && !health.detail_succeeded) health.error_code ||= 'collection_failed';
  if (health.error_code) health.status = posts.size ? 'degraded' : health.error_code;
  return {posts: [...posts.values()], health};
}
