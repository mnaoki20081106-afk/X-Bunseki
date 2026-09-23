// Best-effort discovery of read-only operation IDs from X's public web bundle.
// No bundle code is executed, and no session cookies are sent to the asset CDN.
const OPERATIONS = ['SearchTimeline', 'TweetDetail'];
export function extractQueryIds(source) {
  const result = {};
  // Do not require the closing brace: real operation objects include nested
  // metadata. Require both fields in the same flat portion of the object.
  const patterns = [
    /\bqueryId["']?\s*:\s*["']([A-Za-z0-9_-]{10,100})["'][^{}]{0,500}\boperationName["']?\s*:\s*["'](SearchTimeline|TweetDetail)["']/g,
    /\boperationName["']?\s*:\s*["'](SearchTimeline|TweetDetail)["'][^{}]{0,500}\bqueryId["']?\s*:\s*["']([A-Za-z0-9_-]{10,100})["']/g,
  ];
  for (const [index, pattern] of patterns.entries()) {
    for (const match of source.matchAll(pattern)) {
      result[match[index === 0 ? 2 : 1]] = match[index === 0 ? 1 : 2];
    }
  }
  return result;
}

export function queryIdOverrides(text = '{}') {
  const data = JSON.parse(text || '{}');
  if (!data || Array.isArray(data) || typeof data !== 'object') throw new Error('invalid_query_ids');
  for (const [key, value] of Object.entries(data)) {
    if (!OPERATIONS.includes(key) || typeof value !== 'string' || !/^[A-Za-z0-9_-]{10,100}$/.test(value)) {
      throw new Error('invalid_query_ids');
    }
  }
  return data;
}

async function boundedText(response, limit) {
  if (!response.ok || !response.body) throw new Error('bundle_unavailable');
  const reader = response.body.getReader();
  const chunks = []; let bytes = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > limit) throw new Error('bundle_too_large');
      chunks.push(Buffer.from(value));
    }
  } finally { await reader.cancel(); }
  return Buffer.concat(chunks).toString('utf8');
}

export async function discoverQueryIds(fetchImpl = fetch) {
  // The landing page now uses x-web, while the search page still supplies the
  // GraphQL operation bundle. Share a deadline across HTML and bundle reads.
  const init = { signal: AbortSignal.timeout(45000), redirect: 'error' };
  let stage = 'page';
  try {
    const html = await boundedText(await fetchImpl('https://x.com/home', init), 2_000_000);
    const urls = [...new Set(html.match(/https:\/\/abs\.twimg\.com\/responsive-web\/client-web(?:-legacy)?\/main\.[A-Za-z0-9_-]+\.js/g) || [])].slice(0, 2);
    stage = 'bundle';
    for (const url of urls) {
      const source = await boundedText(await fetchImpl(url, init), 8_000_000);
      const ids = extractQueryIds(source);
      if (ids.SearchTimeline && ids.TweetDetail) return ids;
    }
    throw new Error('query_ids_not_found');
  } catch (error) {
    // Do not log URL, cookies, HTML or response bodies. These two fields are
    // enough to distinguish a failed asset lookup from a network timeout.
    const reason = error instanceof Error ? error.name : 'UnknownError';
    const cause = error instanceof Error && typeof error.cause === 'object' && error.cause !== null
      ? error.cause.code : undefined;
    const knownCodes = ['ETIMEDOUT', 'ECONNRESET', 'ENOTFOUND', 'EAI_AGAIN', 'UND_ERR_CONNECT_TIMEOUT'];
    console.error(`[compat] discovery stage=${stage} kind=${reason} code=${knownCodes.includes(cause) ? cause : 'none'}`);
    throw error;
  }
}
