"""Entry point: ``python -m twincat_mcp`` or ``twincat-mcp``.

Transports: stdio (default, for Claude Code / Claude Desktop / Cursor),
``--http`` for streamable HTTP on 127.0.0.1:<port> (only bind to loopback;
put a tunnel in front yourself if you really want remote access — the server
has ADS routes to live hardware).
"""
from __future__ import annotations

import argparse
import logging
import sys

from .config import load
from .server import build_server


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="twincat-mcp")
    ap.add_argument("--config", help="TOML config path (else TCMCP_CONFIG / ./twincat_mcp.toml)")
    ap.add_argument("--http", type=int, metavar="PORT", help="serve streamable-http on 127.0.0.1:PORT instead of stdio")
    ap.add_argument("--log", default="WARNING", help="log level")
    ap.add_argument("--print-config", action="store_true", help="print effective config and exit")
    args = ap.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log.upper(), logging.WARNING), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load(args.config)
    if args.print_config:
        import json
        print(json.dumps(cfg.to_dict(), indent=2, default=str))
        return
    server = build_server(cfg)
    if args.http:
        server.run(transport="streamable-http", host="127.0.0.1", port=int(args.http))
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
