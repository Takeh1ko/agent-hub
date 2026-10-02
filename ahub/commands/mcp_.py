"""ahub mcp — MCP server (stdio) for Codex and other agents."""

from __future__ import annotations


def cmd_mcp(args) -> int:
    from ahub import log as hublog
    from ahub.mcp import serve

    hublog.setup()
    serve()
    return 0


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("mcp", help=t("help.mcp"))
    p.set_defaults(func=cmd_mcp)
