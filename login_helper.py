"""
login_helper.py
【初回のみ・手動実行】ダミーXにログインし storage_state.json を保存する。

iPad + Codespaces の noVNC では弾かれやすい。通らない場合は
docs/IPAD_SESSION.md と cookie_to_session.py を参照。
"""

import os
import shutil

from playwright.sync_api import sync_playwright

try:
    from playwright_stealth import stealth_sync
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

OUTPUT_PATH = "storage_state.json"


def main():
    with sync_playwright() as p:
        # 本物の Google Chrome が入っていればそれを使う（Chromium より弾かれにくい）
        launch_kwargs = {
            "headless": False,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        }
        if shutil.which("google-chrome") or shutil.which("google-chrome-stable"):
            launch_kwargs["channel"] = "chrome"
            print("(本物の Google Chrome で起動します)")
        else:
            print("(Playwright 同梱 Chromium で起動します)")

        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        page = context.new_page()

        if HAS_STEALTH:
            stealth_sync(page)
            print("(ステルス化を適用しました)")

        print("ブラウザが起動しました。X のログイン画面に移動します...")
        page.goto("https://x.com/login")

        print("\n" + "=" * 60)
        print("ブラウザ上でダミーアカウントのログインを完了してください。")
        print("タイムラインが表示されたら、このターミナルで Enter を押してください。")
        print("=" * 60 + "\n")
        input("ログインが完了したら Enter を押す >> ")

        context.storage_state(path=OUTPUT_PATH)
        print(f"\n完了: ログイン状態を {OUTPUT_PATH} に保存しました。")

        browser.close()


if __name__ == "__main__":
    main()
