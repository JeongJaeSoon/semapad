"""semapad — Claude Desktop 세션 상태를 Codex Micro와 웹 대시보드로."""


def version() -> str:
    """Installed package version; 'dev' for a bare source tree."""
    try:
        from importlib.metadata import version as pkg_version
        return pkg_version("semapad")
    except Exception:
        return "dev"
