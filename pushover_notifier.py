"""
pushover_notifier.py
Pushover 通知 (2026-09: DISABLE_NOTIFICATIONS=1 で送信停止)
"""
from __future__ import annotations

import os

import requests

import notification_text

PUSHOVER_ENDPOINT = "https://api.pushover.net/1/messages.json"

DEFAULT_SOUND = "siren"
DEFAULT_SCHOOL_PRIORITY = "0"
DEFAULT_PRIORITY = "1"
DEFAULT_URGENT_PRIORITY = "2"
DEFAULT_RETRY = "300"
DEFAULT_EXPIRE = "600"


def _is_at_school() -> bool:
    url = os.environ.get("SCHOOL_STATUS_URL")
    secret = os.environ.get("SCHOOL_STATUS_SECRET")
    if not url or not secret:
        return False
    try:
        resp = requests.get(url, headers={"X-Secret": secret}, timeout=5)
        if resp.status_code != 200:
            return False
        data = resp.json()
        return bool(data.get("at_school"))
    except Exception:
        return False


def send_notification(post: dict) -> bool:
    if os.environ.get("DISABLE_NOTIFICATIONS", "1") == "1":
        print("  (Pushover通知は無効化されています)")
        return False

    token = os.environ.get("PUSHOVER_TOKEN")
    user_key = os.environ.get("PUSHOVER_USER_KEY")
    if not token or not user_key:
        raise RuntimeError("PUSHOVER_TOKEN / PUSHOVER_USER_KEY が未設定です")

    text = notification_text.build_message(post)
    title = "急上昇検知"
    if post.get("system_message"):
        title = "システム通知"
        text = post["system_message"]

    if _is_at_school():
        priority = os.environ.get("PUSHOVER_SCHOOL_PRIORITY") or DEFAULT_SCHOOL_PRIORITY
    elif post.get("is_gekiatsu"):
        priority = os.environ.get("PUSHOVER_URGENT_PRIORITY") or DEFAULT_URGENT_PRIORITY
    else:
        priority = os.environ.get("PUSHOVER_PRIORITY") or DEFAULT_PRIORITY

    payload = {
        "token": token,
        "user": user_key,
        "title": title,
        "message": text[:1024],
        "priority": int(priority),
        "sound": os.environ.get("PUSHOVER_SOUND") or DEFAULT_SOUND,
    }
    if int(priority) == 2:
        payload["retry"] = os.environ.get("PUSHOVER_RETRY") or DEFAULT_RETRY
        payload["expire"] = os.environ.get("PUSHOVER_EXPIRE") or DEFAULT_EXPIRE

    resp = requests.post(PUSHOVER_ENDPOINT, data=payload, timeout=10)
    if resp.status_code != 200:
        print(f"[Pushover失敗] {resp.status_code} {resp.text}")
        return False
    return True
