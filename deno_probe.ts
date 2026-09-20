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

Deno.cron("X connectivity probe", "*/15 * * * *", async () => {
  try { console.log(JSON.stringify(await probe())); }
  catch (e) { console.error(String(e)); }
});

Deno.serve(async (req) => {
  const u = new URL(req.url);
  if (u.pathname !== "/probe") return new Response("X-Bunseki Deno probe", { status: 200 });
  try {
    return Response.json(await probe());
  } catch (e) {
    return Response.json({ ok: false, error: String(e) }, { status: 500 });
  }
});
