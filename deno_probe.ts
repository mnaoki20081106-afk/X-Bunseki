// Deno Deploy X connectivity probe.
// No cookie values are logged. Set X_SESSION_STATE as a Deno Deploy environment variable.
function getCookies(): { auth_token: string; ct0: string } {
  const raw = Deno.env.get("X_SESSION_STATE") ?? "";
  if (!raw) throw new Error("X_SESSION_STATE is missing");
  const state = JSON.parse(raw);
  const cookies = Object.fromEntries((state.cookies ?? []).map((c: any) => [c.name, c.value]));
  if (!cookies.auth_token || !cookies.ct0) throw new Error("auth_token/ct0 missing");
  return { auth_token: cookies.auth_token, ct0: cookies.ct0 };
}

async function probe() {
  const c = getCookies();
  const cookie = `auth_token=${c.auth_token}; ct0=${c.ct0}`;
  const res = await fetch("https://x.com/home", {
    redirect: "manual",
    headers: {
      "cookie": cookie,
      "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
      "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "accept-language": "ja,en-US;q=0.9,en;q=0.8",
    },
  });
  const text = await res.text();
  const waiting = text.includes("しばらくお待ちください") || text.toLowerCase().includes("just a moment");
  return {
    ok: res.status >= 200 && res.status < 400 && !waiting,
    status: res.status,
    location: res.headers.get("location"),
    waiting,
    auth_cookie_present: true,
    checked_at: new Date().toISOString(),
  };
}


async function searchPageProbe() {
  const c = getCookies();
  const cookie = `auth_token=${c.auth_token}; ct0=${c.ct0}`;
  const q = encodeURIComponent("lang:ja");
  const res = await fetch(`https://x.com/search?q=${q}&src=typed_query&f=live`, {
    redirect: "manual",
    headers: {
      "cookie": cookie,
      "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
      "accept": "text/html,application/xhtml+xml",
      "accept-language": "ja,en-US;q=0.9,en;q=0.8",
    },
  });
  const body = await res.text();
  const waiting = body.includes("しばらくお待ちください") || body.toLowerCase().includes("just a moment");
  return {
    ok: res.status >= 200 && res.status < 400 && !waiting,
    status: res.status,
    location: res.headers.get("location"),
    waiting,
    response_bytes: body.length,
    checked_at: new Date().toISOString(),
  };
}


async function searchInspect() {
  const c = getCookies();
  const cookie = `auth_token=${c.auth_token}; ct0=${c.ct0}`;
  const q = encodeURIComponent("lang:ja");
  const res = await fetch(`https://x.com/search?q=${q}&src=typed_query&f=live`, {
    headers: {
      "cookie": cookie,
      "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
      "accept": "text/html,application/xhtml+xml",
      "accept-language": "ja,en-US;q=0.9,en;q=0.8",
    },
  });
  const body = await res.text();
  const markers = {
    initial_state: body.includes("__INITIAL_STATE__"),
    graphql: body.includes("SearchTimeline") || body.includes("/i/api/graphql/"),
    tweet_result: body.includes("tweet_results") || body.includes("Tweet"),
    status_links: (body.match(/\/status\//g) || []).length,
    scripts: (body.match(/<script/g) || []).length,
  };
  return { ok: res.ok, status: res.status, response_bytes: body.length, markers, checked_at: new Date().toISOString() };
}


async function initialStateInspect() {
  const c = getCookies();
  const cookie = `auth_token=${c.auth_token}; ct0=${c.ct0}`;
  const q = encodeURIComponent("lang:ja");
  const res = await fetch(`https://x.com/search?q=${q}&src=typed_query&f=live`, {
    headers: {
      "cookie": cookie,
      "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
      "accept": "text/html,application/xhtml+xml",
      "accept-language": "ja,en-US;q=0.9,en;q=0.8",
    },
  });
  const body = await res.text();
  const m = body.match(/window\.__INITIAL_STATE__\s*=\s*({[\s\S]*?});\s*window\./);
  if (!m) {
    const pos = body.indexOf("__INITIAL_STATE__");
    return { ok: false, status: res.status, found: pos >= 0, position: pos, checked_at: new Date().toISOString() };
  }
  try {
    const state = JSON.parse(m[1]);
    const top_keys = Object.keys(state).slice(0, 50);
    const serialized = JSON.stringify(state);
    const rest_ids = serialized.match(/"rest_id":"[0-9]+"/g) || [];
    const tweetish = serialized.match(/"tweet[^"]*"/gi) || [];
    return {
      ok: true,
      status: res.status,
      parsed: true,
      top_keys,
      rest_id_count: rest_ids.length,
      tweet_marker_count: tweetish.length,
      state_bytes: serialized.length,
      checked_at: new Date().toISOString(),
    };
  } catch (e) {
    return { ok: false, status: res.status, parsed: false, error: String(e), checked_at: new Date().toISOString() };
  }
}


const X_BEARER = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA";


async function adaptiveSearchProbe() {
  const c = getCookies();
  const cookie = `auth_token=${c.auth_token}; ct0=${c.ct0}`;
  const params = new URLSearchParams({
    q: "lang:ja",
    count: "20",
    tweet_search_mode: "live",
    query_source: "typed_query",
    tweet_mode: "extended",
    include_entities: "true",
    include_user_entities: "true",
    include_quote_count: "true",
    include_reply_count: "1",
    send_error_codes: "true",
  });
  const headers = {
    "authorization": `Bearer ${X_BEARER}`,
    "cookie": cookie,
    "x-csrf-token": c.ct0,
    "x-twitter-auth-type": "OAuth2Session",
    "x-twitter-active-user": "yes",
    "x-twitter-client-language": "ja",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "accept": "*/*",
  };
  const urls = [
    `https://x.com/i/api/2/search/adaptive.json?${params}`,
    `https://api.x.com/2/search/adaptive.json?${params}`,
  ];
  const results = [];
  for (const url of urls) {
    const res = await fetch(url, { headers });
    const body = await res.text();
    results.push({
      host: new URL(url).host,
      status: res.status,
      ok: res.ok,
      bytes: body.length,
      has_global_objects: body.includes("globalObjects"),
      body_prefix: res.ok ? undefined : body.slice(0, 200),
    });
  }
  return { ok: results.some(r => r.ok && r.has_global_objects), results, checked_at: new Date().toISOString() };
}

async function graphqlSearchProbe() {
  const c = getCookies();
  const cookie = `auth_token=${c.auth_token}; ct0=${c.ct0}`;
  const ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

  // Current live SearchTimeline query ID discovered by XActions from x.com's bundle.
  const queryId = "auLkqtmHqYEpRvflfvLhyQ";
  const variables = { rawQuery: "lang:ja", count: 20, querySource: "typed_query", product: "Latest" };
  const features = {
    rweb_tipjar_consumption_enabled: true,
    responsive_web_graphql_exclude_directive_enabled: true,
    verified_phone_label_enabled: false,
    creator_subscriptions_tweet_preview_api_enabled: true,
    responsive_web_graphql_timeline_navigation_enabled: true,
    responsive_web_graphql_skip_user_profile_image_extensions_enabled: false,
    communities_web_enable_tweet_community_results_fetch: true,
    c9s_tweet_anatomy_moderator_badge_enabled: true,
    articles_preview_enabled: true,
    responsive_web_edit_tweet_api_enabled: true,
    graphql_is_translatable_rweb_tweet_is_translatable_enabled: true,
    view_counts_everywhere_api_enabled: true,
    longform_notetweets_consumption_enabled: true,
    responsive_web_twitter_article_tweet_consumption_enabled: true,
    tweet_awards_web_tipping_enabled: false,
    creator_subscriptions_quote_tweet_preview_enabled: false,
    freedom_of_speech_not_reach_fetch_enabled: true,
    standardized_nudges_misinfo: true,
    tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled: true,
    longform_notetweets_rich_text_read_enabled: true,
    longform_notetweets_inline_media_enabled: true,
    responsive_web_enhance_cards_enabled: false
  };
  const qs = new URLSearchParams({
    variables: JSON.stringify(variables),
    features: JSON.stringify(features),
  });
  const url = `https://x.com/i/api/graphql/${queryId}/SearchTimeline?${qs}`;
  const res = await fetch(url, {
    headers: {
      "authorization": `Bearer ${X_BEARER}`,
      "cookie": cookie,
      "x-csrf-token": c.ct0,
      "x-twitter-auth-type": "OAuth2Session",
      "x-twitter-active-user": "yes",
      "x-twitter-client-language": "ja",
      "user-agent": ua,
      "accept": "*/*",
    },
  });
  const body = await res.text();
  let parsed: any = null;
  try { parsed = JSON.parse(body); } catch {}
  const serialized = parsed ? JSON.stringify(parsed) : body;
  return {
    ok: res.ok,
    status: res.status,
    query_id: queryId,
    response_bytes: body.length,
    has_search_timeline: serialized.includes("search_timeline"),
    tweet_result_markers: (serialized.match(/tweet_results/g) || []).length,
    body_prefix: res.ok ? undefined : body.slice(0, 300),
    checked_at: new Date().toISOString(),
  };
}

Deno.cron("X connectivity probe", "*/15 * * * *", async () => {
  try { console.log(JSON.stringify(await probe())); }
  catch (e) { console.error(String(e)); }
});

Deno.serve(async (req) => {
  const u = new URL(req.url);
  if (u.pathname !== "/probe" && u.pathname !== "/search-probe" && u.pathname !== "/search-inspect" && u.pathname !== "/state-inspect" && u.pathname !== "/graphql-probe" && u.pathname !== "/adaptive-probe") return new Response("X-Bunseki Deno probe", { status: 200 });
  try {
    return Response.json(u.pathname === "/adaptive-probe" ? await adaptiveSearchProbe() : u.pathname === "/graphql-probe" ? await graphqlSearchProbe() : u.pathname === "/state-inspect" ? await initialStateInspect() : u.pathname === "/search-inspect" ? await searchInspect() : u.pathname === "/search-probe" ? await searchPageProbe() : await probe());
  } catch (e) {
    return Response.json({ ok: false, error: String(e) }, { status: 500 });
  }
});
