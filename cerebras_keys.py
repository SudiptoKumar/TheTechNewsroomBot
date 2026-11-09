"""Cerebras multi-key failover + owner alerts (2026-10-31).

Key order: CEREBRAS_API_KEY first, CEREBRAS_API_KEY_2 second. When a call
fails with a quota/auth status (401/402/403), the active key is retired
for the rest of the run, the owner is alerted once per key through the
Newsroom bot, and the bot switches to the next key (see cerebras_create in
main.py). A 429 is only a rate limit, not a dead key: the same key is
retried with backoff (20s, 40s, 60s) first, and retired — with a separate
throttle alert — only if it keeps failing. Alerts are best-effort: they
never raise, and they are skipped when NEWSROOM_BOT_TOKEN or ALERT_CHAT_ID
is not configured.
"""
from __future__ import annotations

import logging
import os
import time
from urllib import parse as urlparse
from urllib import request as urlrequest

logger = logging.getLogger("cerebras_keys")

QUOTA_STATUSES = {401, 402, 403, 429}
BOT_LABEL = "TheTechNewsroom"

THROTTLE_BACKOFF = (20, 40, 60)

_snapshot: list[str] | None = None
_index = 0
_dead: set[int] = set()
_alerted: set[int] = set()
_all_dead_alerted = False
_throttle_attempts: dict[int, int] = {}
_throttled: set[int] = set()


def _keys() -> list[str]:
    """The ordered key list, snapshotted on first use so it always reflects
    the original key 1 + key 2 pair from the environment."""
    global _snapshot
    if _snapshot is None:
        keys: list[str] = []
        for raw in (os.getenv("CEREBRAS_API_KEY", ""), os.getenv("CEREBRAS_API_KEY_2", "")):
            key = (raw or "").strip()
            if key and key not in keys:
                keys.append(key)
        _snapshot = keys
    return _snapshot


def active_key() -> str:
    keys = _keys()
    if not keys:
        return ""
    return keys[min(_index, len(keys) - 1)]


def is_quota_status(status) -> bool:
    return status in QUOTA_STATUSES


def _send_alert(text: str) -> None:
    token = os.getenv("NEWSROOM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("ALERT_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    try:
        data = urlparse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urlrequest.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        urlrequest.urlopen(req, timeout=15).read()
    except Exception:
        pass


def _alert_key_dead(index: int, status, total: int) -> None:
    if index in _alerted:
        return
    _alerted.add(index)
    if index + 1 < total:
        _send_alert(
            f"⚠️ {BOT_LABEL}: Cerebras key {index + 1} finished (HTTP {status}). "
            f"Switched to key {index + 2} — posting continues."
        )


def _alert_key_throttled(index: int, total: int) -> None:
    if index in _alerted:
        return
    _alerted.add(index)
    if index + 1 < total:
        _send_alert(
            f"⏳ {BOT_LABEL}: Cerebras key {index + 1} is rate-limited (HTTP 429) even after retries. "
            f"Switched to key {index + 2} — posting continues."
        )


def failover(status) -> bool:
    """Retire the active key after a quota/auth failure. Returns True when
    the call should be retried — on the next key after a retirement, or on
    the SAME key after a backoff wait when the failure was a 429 rate
    limit that has not exhausted its retries. Returns False when no key
    is left, in which case the owner gets the all-dead alert."""
    global _index, _all_dead_alerted
    keys = _keys()
    if not keys:
        return False
    if status == 429:
        attempts = _throttle_attempts.get(_index, 0)
        if attempts < len(THROTTLE_BACKOFF):
            delay = THROTTLE_BACKOFF[attempts]
            _throttle_attempts[_index] = attempts + 1
            logger.warning(
                "Cerebras key %d rate-limited (HTTP 429); retrying the same key in %ds (attempt %d/%d)",
                _index + 1, delay, attempts + 1, len(THROTTLE_BACKOFF),
            )
            time.sleep(delay)
            return True
        _throttled.add(_index)
    _dead.add(_index)
    if status == 429:
        _alert_key_throttled(_index, len(keys))
    else:
        _alert_key_dead(_index, status, len(keys))
    if _index + 1 < len(keys):
        _index += 1
        logger.warning(
            "Cerebras key %d exhausted (HTTP %s); switched to key %d",
            _index, status, _index + 1,
        )
        return True
    if not _all_dead_alerted:
        _all_dead_alerted = True
        if _index in _throttled:
            _send_alert(
                f"⛔ {BOT_LABEL}: ALL Cerebras keys are rate-limited (last error HTTP {status}). "
                "AI features are down until the rate limits clear."
            )
        else:
            _send_alert(
                f"⛔ {BOT_LABEL}: ALL Cerebras keys are finished (last error HTTP {status}). "
                "AI features are down until a key is topped up."
            )
    return False


def _reset_state() -> None:
    """Test support: restore the first key and clear run state."""
    global _index, _all_dead_alerted, _snapshot
    _snapshot = None
    _index = 0
    _dead.clear()
    _alerted.clear()
    _all_dead_alerted = False
    _throttle_attempts.clear()
    _throttled.clear()
