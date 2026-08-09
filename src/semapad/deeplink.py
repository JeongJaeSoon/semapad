"""Resolve a Claude CLI session and open its matching Desktop session."""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

#: Claude Desktop stores mappings below account-specific org/account folders.
MAPPING_ROOT = (Path.home() / "Library" / "Application Support" / "Claude"
                / "claude-code-sessions")
MAPPING_GLOB = "*/*/local_*.json"

# The legacy /claude-code-desktop route is rewritten to /epitaxy inside the
# app and that redirect renders as a full reload; the direct route switches
# sessions in place.
_ROUTE = "claude://claude.ai/epitaxy"
_OPEN_TIMEOUT_SECONDS = 5.0


def _is_canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError):
        return False
    return str(parsed) == value


def _is_local_id(value: object) -> bool:
    return (isinstance(value, str) and value.startswith("local_")
            and _is_canonical_uuid(value.removeprefix("local_")))


def _has_symlink(root: Path, path: Path) -> bool:
    """Reject links in the mapping path instead of reading outside ``root``."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True

    current = root
    if current.is_symlink():
        return True
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def url_for(local_id: str) -> str:
    """Build the exact Desktop route for a validated local session ID."""
    if not _is_local_id(local_id):
        raise ValueError("local_id must be local_ followed by a canonical UUID")
    return f"{_ROUTE}/{local_id}"


def _mapping_paths(root: Path) -> list[Path]:
    try:
        return sorted(root.glob(MAPPING_GLOB), key=lambda path: str(path))
    except OSError:
        return []


def local_id_for(session_id: str,
                 roots: Sequence[Path] | None = None) -> str | None:
    """Map a canonical CLI session UUID to one unambiguous Desktop ID.

    Mapping files are read only when this function is called. Broken, malformed,
    and linked entries are ignored because this is a private on-disk schema.
    """
    if not _is_canonical_uuid(session_id):
        return None

    found: set[str] = set()
    search_roots = roots if roots is not None else (MAPPING_ROOT,)
    for root in search_roots:
        for path in _mapping_paths(root):
            if _has_symlink(root, path):
                continue
            local_id = path.stem
            if not _is_local_id(local_id):
                continue
            try:
                raw = json.loads(path.read_text())
            except (OSError, ValueError, RecursionError):
                continue
            if not isinstance(raw, dict):
                continue
            if raw.get("sessionId") != local_id:
                continue
            if raw.get("cliSessionId") == session_id:
                found.add(local_id)

    return next(iter(found)) if len(found) == 1 else None


def open_session(
    session_id: str,
    roots: Sequence[Path] | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> bool:
    """Open the mapped Desktop session, returning true only on exit status 0."""
    local_id = local_id_for(session_id, roots)
    if local_id is None:
        return False

    try:
        completed = runner(
            ["/usr/bin/open", url_for(local_id)],
            check=False,
            timeout=_OPEN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return getattr(completed, "returncode", None) == 0
