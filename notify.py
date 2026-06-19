"""Minimal Telegram notifier, env-gated. No-op if creds absent (safe in paper/testnet).

Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS (comma-separated) in .env to enable.
"""
import logging
import os

import requests

logger = logging.getLogger("slow.notify")


def notify(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_ids = os.getenv("TELEGRAM_CHAT_IDS", "")
    if not token or not chat_ids:
        return
    for chat_id in [c.strip() for c in chat_ids.split(",") if c.strip()]:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": f"[slow] {text}"},
                timeout=10,
            )
        except Exception as e:
            logger.error(f"telegram notify failed: {e}")
