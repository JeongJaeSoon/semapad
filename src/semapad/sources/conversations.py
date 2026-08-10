"""Scan Claude Desktop conversation mapping files (spec §2).

The sidebar list is exactly the mapping files with ``isArchived == false``, and
the filename is the deeplink ``local_id``. The schema is private and may change
without notice: a file that cannot be read safely is skipped and surfaced as a
diagnostic instead of guessed at (spec principle 5, §11.10).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from semapad import deeplink


@dataclass(frozen=True)
class Conversation:
    """One sidebar line. Times are epoch seconds (Desktop stores milliseconds)."""

    local_id: str
    cli_session_id: str | None
    title: str
    cwd: str
    created_at: float
    last_activity_at: float
    #: 0.0 when Desktop has not recorded a focus yet -- the unread
    #: approximation (spec §5) then stays off rather than guessing.
    last_focused_at: float
    #: Pin state is NOT in the mapping files -- settled by the 2026-08-10
    #: pin/unpin toggle experiment: pinning added no field, unpinning left
    #: autoArchiveExempt=true behind (that flag is not pin). It lives in the
    #: app's LevelDB, which spec principles rule out. Hardwired False until
    #: Desktop ever exposes it in these files (then: selection priority,
    #: spec §11.7).
    pinned: bool


def _seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) / 1000.0


def scan(root: Path | None = None) -> tuple[tuple[Conversation, ...], tuple[str, ...]]:
    """Return (non-archived conversations, diagnostics), newest data as-is."""
    root = deeplink.MAPPING_ROOT if root is None else root
    if not root.is_dir():
        return (), ("mapping directory missing",)

    out: list[Conversation] = []
    diags: list[str] = []
    for path in sorted(root.glob(deeplink.MAPPING_GLOB), key=str):
        name = path.name
        if deeplink._has_symlink(root, path):
            diags.append(f"{name}: symlinked entry skipped")
            continue
        local_id = path.stem
        if not deeplink._is_local_id(local_id):
            continue
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError, RecursionError):
            diags.append(f"{name}: unreadable mapping file")
            continue
        if not isinstance(raw, dict):
            diags.append(f"{name}: mapping file is not an object")
            continue
        if raw.get("sessionId") != local_id:
            # The filename is the authority (same rule as the state store).
            diags.append(f"{name}: sessionId does not match filename")
            continue
        archived = raw.get("isArchived")
        if archived is True:
            continue
        if archived is not False:
            diags.append(f"{name}: isArchived missing, not provably on the sidebar")
            continue

        cli_session_id = raw.get("cliSessionId")
        if not isinstance(cli_session_id, str) or not cli_session_id:
            cli_session_id = None
        title = raw.get("title")
        cwd = raw.get("cwd")
        out.append(Conversation(
            local_id=local_id,
            cli_session_id=cli_session_id,
            title=title if isinstance(title, str) else "",
            cwd=cwd if isinstance(cwd, str) else "",
            created_at=_seconds(raw.get("createdAt")),
            last_activity_at=_seconds(raw.get("lastActivityAt")),
            last_focused_at=_seconds(raw.get("lastFocusedAt")),
            pinned=False,
        ))
    return tuple(out), tuple(diags)
