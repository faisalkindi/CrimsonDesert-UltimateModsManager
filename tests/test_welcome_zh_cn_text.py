"""The zh-CN welcome headline was corrupted by a bad unicode escape:
``\\u5192\\u96669\\u8005`` parses as U+5192 + U+9666 + literal "9" +
U+8005, rendering the nonsense string "冒陦" + "9" + "者"
instead of 冒险者 (adventurer)."""
from __future__ import annotations

from cdumm.gui.welcome_wizard import _WELCOME_MESSAGES


def test_zh_cn_welcome_says_adventurer():
    title, _subtitle = _WELCOME_MESSAGES["zh-CN"]
    assert title == "欢迎，冒险者！"
    assert "陦" not in title  # the corrupt hanzi
    assert "9" not in title       # the stray literal digit
