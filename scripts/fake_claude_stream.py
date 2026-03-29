#!/usr/bin/env python3
"""Tiny fake Claude CLI for dashboard smoke testing.

It speaks the subset of Claude's stream-json protocol that Cymphony consumes:
- one system/init event
- one assistant notification
- one result event

Use env vars to control behavior:
- CYMPHONY_FAKE_CLAUDE_MODE=success|fail|input_required
- CYMPHONY_FAKE_CLAUDE_DELAY_SECS=<float>
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> int:
    mode = os.environ.get("CYMPHONY_FAKE_CLAUDE_MODE", "success").strip().lower()
    delay = float(os.environ.get("CYMPHONY_FAKE_CLAUDE_DELAY_SECS", "20"))
    session_id = f"fake-session-{uuid.uuid4()}"

    _emit({
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
    })
    _emit({
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "text",
                    "text": "Fake smoke-test runner started. This is a safe no-op turn.",
                }
            ]
        },
    })

    time.sleep(max(delay, 0))

    if mode == "input_required":
        _emit({
            "type": "result",
            "subtype": "input_required",
        })
        return 0

    if mode == "fail":
        _emit({
            "type": "result",
            "subtype": "error",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_read_input_tokens": 0,
            },
        })
        return 0

    _emit({
        "type": "result",
        "subtype": "success",
        "usage": {
            "input_tokens": 42,
            "output_tokens": 9,
            "cache_read_input_tokens": 0,
        },
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
