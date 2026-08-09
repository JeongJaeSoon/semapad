from __future__ import annotations

import sys

import pytest

from semapad import frontmost


class FakeRuntime:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def bundle_id(self) -> str | None:
        return self.value


def test_injected_runtime_returns_bundle_identifier() -> None:
    assert frontmost.bundle_id(
        lambda: FakeRuntime("com.anthropic.claudefordesktop"),
    ) == "com.anthropic.claudefordesktop"


def test_injected_runtime_can_report_no_aqua_frontmost_app() -> None:
    assert frontmost.bundle_id(lambda: FakeRuntime(None)) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS native smoke test")
def test_native_runtime_binds_without_a_permission_prompt() -> None:
    # Headless test runners legitimately have no frontmost Aqua application.
    value = frontmost._ObjCRuntime().bundle_id()
    assert value is None or isinstance(value, str)
