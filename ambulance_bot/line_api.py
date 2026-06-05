from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request


LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
TEXT_LIMIT = 4900


def configured_recipients() -> list[str]:
    values: list[str] = []
    multi = os.getenv("LINE_TO_USER_IDS", "").strip()
    if multi:
        values.extend(item.strip() for item in multi.split(",") if item.strip())
    single = os.getenv("LINE_TO_USER_ID", "").strip()
    if single:
        values.append(single)
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def verify_signature(channel_secret: str, body: bytes, signature: str) -> bool:
    if not channel_secret:
        return True
    if not signature:
        return False
    digest = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature)


def reply_text(reply_token: str, text: str) -> None:
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not token or not reply_token:
        print("[line] reply skipped: missing token or replyToken", flush=True)
        return
    _post_line(
        LINE_REPLY_URL,
        token,
        {
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": _cap_text(text)}],
        },
    )


def push_text(to: str, text: str) -> None:
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not token or not to:
        print("[line] push skipped: missing token or recipient", flush=True)
        return
    _post_line(
        LINE_PUSH_URL,
        token,
        {
            "to": to,
            "messages": [{"type": "text", "text": _cap_text(text)}],
        },
    )


def _post_line(url: str, token: str, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            print(f"[line] {url} -> {getattr(response, 'status', 'unknown')}", flush=True)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"[line] HTTP {exc.code}: {detail}", flush=True)
        raise


def _cap_text(text: str) -> str:
    if len(text) <= TEXT_LIMIT:
        return text
    return text[: TEXT_LIMIT - 20] + "\n...內容過長已截斷"
