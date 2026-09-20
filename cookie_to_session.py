"""
cookie_to_session.py

noVNC / Playwrightログインが弾かれるとき用。
**自分のスマホ・PCの普通のブラウザ**でダミーXにログインし、
Cookieの auth_token と ct0 だけを貼り付けて storage_state.json を作る。

使い方:
  python3 cookie_to_session.py

その後:
  base64 -w0 storage_state.json
  → 出た文字列を GitHub Secrets の X_SESSION_STATE に登録
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

OUTPUT = Path("storage_state.json")


def main() -> None:
    print("=" * 60)
    print("X Cookie → storage_state.json 変換")
    print("=" * 60)
    print()
    print("【事前準備】")
    print("1. ダミーXアカウントで、普段使っているブラウザからログイン")
    print("   (iPhone Safari / PC Chrome など。CodespacesやnoVNCは使わない)")
    print("2. Cookieを取る:")
    print()
    print("  ■ PC (Chrome/Edge)")
    print("    x.com を開く → F12 → Application → Cookies → https://x.com")
    print("    auth_token と ct0 の Value をコピー")
    print()
    print("  ■ iPhone (Safari) — やや面倒")
    print("    Macがあるなら: MacのSafariで「開発」メニューからiPhoneを選択")
    print("    無い場合: PCのブラウザで同じダミーアカウントにログインしてCookie取得")
    print()
    print("  ■ 拡張機能 (PC)")
    print("    EditThisCookie / Cookie-Editor で x.com の cookie をエクスポート")
    print("=" * 60)
    print()

    auth_token = input("auth_token を貼り付け: ").strip().strip('"').strip("'")
    ct0 = input("ct0 を貼り付け: ").strip().strip('"').strip("'")

    if not auth_token or not ct0:
        print("[ERROR] auth_token と ct0 の両方が必要です。")
        sys.exit(1)
    if len(auth_token) < 20 or len(ct0) < 20:
        print("[ERROR] 値が短すぎます。コピー漏れを確認してください。")
        sys.exit(1)

    cookies = [
        {
            "name": "auth_token",
            "value": auth_token,
            "domain": ".x.com",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": True,
            "sameSite": "None",
        },
        {
            "name": "ct0",
            "value": ct0,
            "domain": ".x.com",
            "path": "/",
            "expires": -1,
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        },
        # 互換用に twitter.com ドメインにも同じものを置く
        {
            "name": "auth_token",
            "value": auth_token,
            "domain": ".twitter.com",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": True,
            "sameSite": "None",
        },
        {
            "name": "ct0",
            "value": ct0,
            "domain": ".twitter.com",
            "path": "/",
            "expires": -1,
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        },
    ]

    state = {
        "cookies": cookies,
        "origins": [
            {
                "origin": "https://x.com",
                "localStorage": [],
            }
        ],
    }

    OUTPUT.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    b64 = base64.b64encode(OUTPUT.read_bytes()).decode("ascii")

    print()
    print(f"完了: {OUTPUT} を書きました。")
    print()
    print("GitHub Secrets の X_SESSION_STATE に、次の1行をそのまま貼ってください:")
    print("-" * 60)
    print(b64)
    print("-" * 60)
    print()
    print("注意: auth_token はログイン本体です。リポジトリにコミットしないでください。")


if __name__ == "__main__":
    main()
