"""ahub mcp — MCP-сервер (stdio) для Codex и других агентов."""

from __future__ import annotations


def cmd_mcp(args) -> int:
    from ahub import log as hublog
    from ahub.mcp import serve

    hublog.setup()
    serve()
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("mcp", help="MCP-сервер (stdio): те же ручки для других агентов")
    p.set_defaults(func=cmd_mcp)
