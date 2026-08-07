"""
login_helper.py
【初回のみ・手動実行するスクリプト】GitHub Actionsの定期実行では使わない。

ダミーのXアカウントでログインし、ログイン状態(Cookie等)を
storage_state.json として保存する。これを以降のスクレイピングで使い回すことで、
毎回ログインし直す必要がなくなり、アカウントへの負荷・凍結リスクも下げられる。

【PC/Macがある場合】
  pip install playwright
  playwright install chromium
  python login_helper.py

【iPadしか無い場合】
  GitHub Codespacesを使う。README.mdの「iPadだけの場合」セクションを参照。
  codespace_login.sh が自動的にこのスクリプトを呼び出す。
"""

import os

from playwright.sync_api import sync_playwright

OUTPUT_PATH = "storage_state.json"


def main():
    with sync_playwright() as p:
        # Codespaces + Xvfb環境では DISPLAY が設定されているのでheadless=Falseで
        # 仮想ディスプレイ上にブラウザが描画される(→ noVNC経由で見える)。
        # ローカルPC実行時も同様にheadless=Falseで実ブラウザが立ち上がる。
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        print("ブラウザが起動しました。X (twitter.com) のログイン画面に移動します...")
        page.goto("https://x.com/login")

        print("\n" + "=" * 60)
        print("ブラウザ上で、ダミーアカウントのユーザー名/パスワードを入力し、")
        print("必要なら2段階認証も完了させてください。")
        print("タイムライン画面(ログイン後のトップページ)が表示されたら、")
        print("このターミナルに戻って Enter キーを押してください。")
        print("=" * 60 + "\n")
        input("ログインが完了したら Enter を押す >> ")

        context.storage_state(path=OUTPUT_PATH)
        print(f"\n完了: ログイン状態を {OUTPUT_PATH} に保存しました。")

        browser.close()


if __name__ == "__main__":
    main()
