import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from semapad import deeplink


CLI_ID = "aaaaaaaa-1111-4111-8111-111111111111"
OTHER_CLI_ID = "99999999-9999-4999-8999-bbbbbbbbbbbb"
LOCAL_UUID = "bbbbbbbb-2222-4222-8222-222222222222"
OTHER_LOCAL_UUID = "cccccccc-3333-4333-8333-333333333333"
LOCAL_ID = f"local_{LOCAL_UUID}"
OTHER_LOCAL_ID = f"local_{OTHER_LOCAL_UUID}"


def _mapping(
    root: Path,
    local_id: str = LOCAL_ID,
    cli_id: object = CLI_ID,
    *,
    content_local_id: object | None = None,
    title: str = "test",
) -> Path:
    account = root / "org" / "account"
    account.mkdir(parents=True, exist_ok=True)
    path = account / f"{local_id}.json"
    path.write_text(json.dumps({
        "sessionId": local_id if content_local_id is None else content_local_id,
        "cliSessionId": cli_id,
        "title": title,
    }))
    return path


def test_mapping_constants_point_at_claude_desktop_account_files():
    assert deeplink.MAPPING_ROOT == (
        Path.home() / "Library" / "Application Support" / "Claude"
        / "claude-code-sessions"
    )
    assert deeplink.MAPPING_GLOB == "*/*/local_*.json"


def test_url_uses_the_exact_desktop_path_and_not_an_import_route():
    url = deeplink.url_for(LOCAL_ID)
    assert url == f"claude://claude.ai/epitaxy/{LOCAL_ID}"
    assert "resume" not in url


@pytest.mark.parametrize("local_id", [
    "local_not-a-uuid",
    f"local_{LOCAL_UUID.upper()}",
    LOCAL_UUID,
    f"{LOCAL_ID}/extra",
])
def test_url_rejects_noncanonical_local_ids(local_id: str):
    with pytest.raises(ValueError):
        deeplink.url_for(local_id)


def test_local_id_is_joined_by_the_exact_cli_session_uuid(tmp_path: Path):
    _mapping(tmp_path)
    assert deeplink.local_id_for(CLI_ID, [tmp_path]) == LOCAL_ID
    assert deeplink.local_id_for(OTHER_CLI_ID, [tmp_path]) is None


@pytest.mark.parametrize("session_id", [
    "not-a-uuid",
    CLI_ID.upper(),
    CLI_ID.replace("-", ""),
    f" {CLI_ID}",
])
def test_noncanonical_cli_session_ids_do_not_match(
    tmp_path: Path, session_id: str,
):
    _mapping(tmp_path)
    assert deeplink.local_id_for(session_id, [tmp_path]) is None


def test_missing_broken_and_wrong_top_level_files_are_skipped(tmp_path: Path):
    missing = tmp_path / "missing"
    account = tmp_path / "org" / "account"
    account.mkdir(parents=True)
    (account / f"local_{OTHER_LOCAL_UUID}.json").write_text("{broken")
    (account / f"local_44444444-4444-4444-8444-444444444444.json").write_text("[]")
    _mapping(tmp_path)

    assert deeplink.local_id_for(CLI_ID, [missing, tmp_path]) == LOCAL_ID


@pytest.mark.parametrize(
    ("filename_id", "content_id", "cli_id"),
    [
        ("local_not-a-uuid", "local_not-a-uuid", CLI_ID),
        (LOCAL_ID, OTHER_LOCAL_ID, CLI_ID),
        (LOCAL_ID, LOCAL_ID, 123),
        (LOCAL_ID, LOCAL_ID, CLI_ID.upper()),
    ],
)
def test_wrong_id_shapes_and_filename_content_mismatches_are_skipped(
    tmp_path: Path,
    filename_id: str,
    content_id: object,
    cli_id: object,
):
    _mapping(
        tmp_path,
        filename_id,
        cli_id,
        content_local_id=content_id,
    )
    assert deeplink.local_id_for(CLI_ID, [tmp_path]) is None


def test_symlinked_mapping_file_is_skipped(tmp_path: Path):
    target_root = tmp_path / "target"
    target = _mapping(target_root)
    account = tmp_path / "search" / "org" / "account"
    account.mkdir(parents=True)
    (account / target.name).symlink_to(target)

    assert deeplink.local_id_for(CLI_ID, [tmp_path / "search"]) is None


def test_symlinked_account_directory_is_skipped(tmp_path: Path):
    target_root = tmp_path / "target"
    _mapping(target_root)
    org = tmp_path / "search" / "org"
    org.mkdir(parents=True)
    (org / "account").symlink_to(target_root / "org" / "account")

    assert deeplink.local_id_for(CLI_ID, [tmp_path / "search"]) is None


def test_conflicting_local_ids_are_ambiguous_regardless_of_file_order(tmp_path: Path):
    _mapping(tmp_path / "first", LOCAL_ID, CLI_ID)
    _mapping(tmp_path / "second", OTHER_LOCAL_ID, CLI_ID)

    assert deeplink.local_id_for(
        CLI_ID, [tmp_path / "first", tmp_path / "second"],
    ) is None
    assert deeplink.local_id_for(
        CLI_ID, [tmp_path / "second", tmp_path / "first"],
    ) is None


def test_duplicate_files_with_the_same_local_id_are_not_ambiguous(tmp_path: Path):
    _mapping(tmp_path / "first", LOCAL_ID, CLI_ID)
    _mapping(tmp_path / "second", LOCAL_ID, CLI_ID)
    assert deeplink.local_id_for(
        CLI_ID, [tmp_path / "first", tmp_path / "second"],
    ) == LOCAL_ID


def test_mapping_reader_has_no_size_cap(tmp_path: Path):
    _mapping(tmp_path, title="x" * 1_100_000)
    assert deeplink.local_id_for(CLI_ID, [tmp_path]) == LOCAL_ID


def test_open_session_runs_absolute_open_with_an_argv_list(tmp_path: Path):
    _mapping(tmp_path)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    assert deeplink.open_session(CLI_ID, [tmp_path], runner=runner) is True
    assert calls == [(
        ["/usr/bin/open", f"claude://claude.ai/epitaxy/{LOCAL_ID}"],
        {"check": False, "timeout": 5.0},
    )]
    assert "shell" not in calls[0][1]


def test_open_session_requires_a_zero_return_code(tmp_path: Path):
    _mapping(tmp_path)

    def runner(_argv, **_kwargs):
        return SimpleNamespace(returncode=7)

    assert deeplink.open_session(CLI_ID, [tmp_path], runner=runner) is False


@pytest.mark.parametrize("error", [
    OSError("open unavailable"),
    subprocess.TimeoutExpired(["/usr/bin/open"], timeout=5.0),
])
def test_open_session_turns_process_errors_into_false(tmp_path: Path, error: Exception):
    _mapping(tmp_path)

    def runner(_argv, **_kwargs):
        raise error

    assert deeplink.open_session(CLI_ID, [tmp_path], runner=runner) is False


def test_unmapped_session_never_calls_the_runner(tmp_path: Path):
    calls = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    assert deeplink.open_session(CLI_ID, [tmp_path], runner=runner) is False
    assert calls == []
