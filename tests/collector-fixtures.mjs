export function tweet(id = '123') {
  return { rest_id: id, views: { count: '50000' },
    core: { user_results: { result: { core: { name: 'Author', screen_name: 'author' } } } },
    legacy: { id_str: id, created_at: 'Tue Sep 22 10:00:00 +0000 2026', full_text: '速報',
      favorite_count: 500, retweet_count: 100, reply_count: 20, quote_count: 5, bookmark_count: 30 } };
}
export function timeline(tweets = [tweet()], operation = 'SearchTimeline') {
  const instructions = [{ type: 'TimelineAddEntries', entries: tweets.map(result => ({
    content: { itemContent: { tweet_results: { result } } },
  })) }];
  return { data: operation === 'SearchTimeline'
    ? { search_by_raw_query: { search_timeline: { timeline: { instructions } } } }
    : { threaded_conversation_with_injections_v2: { instructions } } };
}
