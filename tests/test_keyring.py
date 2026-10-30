from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["CEREBRAS_API_KEY"] = "test-key-1"
os.environ["CEREBRAS_API_KEY_2"] = "test-key-2"

KEY1, KEY2 = "test-key-1", "test-key-2"
BEHAVIOR = {KEY1: 402, KEY2: 200}


class FakeAPIError(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status_code = status


class FakeCerebras:
    instances: list[str] = []

    def __init__(self, api_key=None, **_):
        self.api_key = api_key
        FakeCerebras.instances.append(api_key)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        status = BEHAVIOR.get(self.api_key, 200)
        if status >= 400:
            raise FakeAPIError(status)
        return {"ok": True, "key": self.api_key}


# Inject the fake SDK client before main imports the real one.
import cerebras.cloud.sdk as _sdk

_sdk.Cerebras = FakeCerebras

import cerebras_keys
import main


def reset(behavior):
    BEHAVIOR.clear()
    BEHAVIOR.update(behavior)
    FakeCerebras.instances.clear()
    cerebras_keys._reset_state()
    main.cerebras = None
    main._cerebras_key = None


def test_failover_to_second_key() -> None:
    reset({KEY1: 402, KEY2: 200})
    result = main.cerebras_create(model="m", messages=[])
    assert result == {"ok": True, "key": KEY2}, result
    assert FakeCerebras.instances == [KEY1, KEY2], FakeCerebras.instances


def test_non_quota_error_does_not_rotate() -> None:
    reset({KEY1: 400, KEY2: 200})
    try:
        main.cerebras_create(model="m", messages=[])
    except FakeAPIError as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("400 should have raised")
    assert FakeCerebras.instances == [KEY1], FakeCerebras.instances


def test_both_keys_dead_raises() -> None:
    reset({KEY1: 402, KEY2: 402})
    try:
        main.cerebras_create(model="m", messages=[])
    except FakeAPIError as exc:
        assert exc.status_code == 402
    else:
        raise AssertionError("dead keys should have raised")
    assert FakeCerebras.instances == [KEY1, KEY2], FakeCerebras.instances


def test_alert_fires_once() -> None:
    reset({KEY1: 402, KEY2: 200})
    os.environ["NEWSROOM_BOT_TOKEN"] = "dummy-token"
    os.environ["ALERT_CHAT_ID"] = "12345"
    sent = []

    class _Resp:
        def read(self):
            return b"{}"

    original = cerebras_keys.urlrequest.urlopen
    cerebras_keys.urlrequest.urlopen = lambda req, timeout=None: (sent.append(req.data.decode()), _Resp())[1]
    try:
        main.cerebras_create(model="m", messages=[])
    finally:
        cerebras_keys.urlrequest.urlopen = original
        os.environ.pop("NEWSROOM_BOT_TOKEN", None)
        os.environ.pop("ALERT_CHAT_ID", None)
    assert len(sent) == 1, sent
    assert "TheTechNewsroom" in sent[0] and "key+1" in sent[0] or "key 1" in sent[0], sent


def run_all() -> None:
    test_failover_to_second_key()
    test_non_quota_error_does_not_rotate()
    test_both_keys_dead_raises()
    test_alert_fires_once()
    reset({KEY1: 402, KEY2: 200})
    print("keyring failover tests: PASS")


if __name__ == "__main__":
    run_all()
