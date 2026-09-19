"""line_notifier.py - DISABLED 2026-09"""

def send_notification(post: dict) -> bool:
    print("  (LINE通知は無効化されています)")
    return False

def should_send(post: dict) -> bool:
    return False

def build_message(post: dict) -> str:
    return ""
