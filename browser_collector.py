"""Fallback for SDK bootstrap incompatibility; let X's own web app make requests."""
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import quote


def collect_browser(queries, watch, session_path):
    from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout

    events = []
    current = {}
    navigation_error = None
    started = time.monotonic()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(storage_state=session_path)
        page = context.new_page()

        def on_response(response):
            operation = response.url.split('?', 1)[0].rsplit('/', 1)[-1]
            if operation not in {'SearchTimeline', 'TweetDetail'}:
                return
            try:
                body = response.json()
            except Exception:
                body = {}
            headers = response.headers
            def integer(name):
                value = headers.get(name)
                return int(value) if value and value.isdigit() else None
            events.append({**current, 'operation': operation, 'status': response.status,
                'body': body, 'rate': {'limit': integer('x-rate-limit-limit'),
                'remaining': integer('x-rate-limit-remaining'), 'reset': integer('x-rate-limit-reset')}})

        page.on('response', on_response)
        for spec in queries:
            if time.monotonic() - started > 160:
                break
            current = {'source': spec['source']}
            url = 'https://x.com/search?q=' + quote(spec['query']) + '&src=typed_query&f=' + ('live' if spec['product'] == 'Latest' else 'top')
            try:
                with page.expect_response(lambda r: '/SearchTimeline?' in r.url, timeout=15000):
                    page.goto(url, wait_until='domcontentloaded', timeout=20000)
                page.wait_for_timeout(500)
            except BrowserTimeout:
                if '/login' in page.url or '/i/flow/login' in page.url:
                    navigation_error = 'session_expired'
                    break
                navigation_error = 'network_error'
            if events and events[-1]['status'] in {401, 403, 429}:
                break
        if navigation_error != 'session_expired':
            for item in watch:
                if time.monotonic() - started > 230:
                    break
                current = {'postId': str(item['post_id'])}
                try:
                    with page.expect_response(lambda r: '/TweetDetail?' in r.url, timeout=12000):
                        page.goto('https://x.com/i/status/' + quote(str(item['post_id'])), wait_until='domcontentloaded', timeout=15000)
                    page.wait_for_timeout(300)
                except BrowserTimeout:
                    navigation_error = navigation_error or 'network_error'
                if events and events[-1]['status'] in {401, 403, 429}:
                    break
        browser.close()
    helper = Path(__file__).with_name('browser_results.mjs').as_uri()
    script = 'import {normalizeBrowserResults} from ' + json.dumps(helper) + ';let s="";for await(const c of process.stdin)s+=c;const d=JSON.parse(s);console.log(JSON.stringify(normalizeBrowserResults(d.events,d.error)));'
    result = subprocess.run(['node', '--input-type=module', '-e', script],
        input=json.dumps({'events': events, 'error': navigation_error}), text=True,
        capture_output=True, check=True, timeout=20)
    print('[browser] observed HTTP responses=' + str(len(events)))
    return result.stdout
