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
    ap.add_argument("--host", default="127.0.0.1", help="bind address for --http (keep loopback; expose through a tunnel)")
    ap.add_argument("--token", default=None, help="bearer token required on every HTTP request (or env TCMCP_HTTP_TOKEN); "
                                                 "mandatory unless --host is 127.0.0.1")
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
        import os
        token = args.token or os.environ.get("TCMCP_HTTP_TOKEN")
        if args.host not in ("127.0.0.1", "localhost", "::1") and not token:
            raise SystemExit("refusing to bind a non-loopback address without --token / TCMCP_HTTP_TOKEN")
        run_http(server, args.host, int(args.http), token)
    else:
        server.run(transport="stdio")


def run_http(server, host: str, port: int, token: str | None) -> None:
    """Streamable HTTP with a fixed bearer token — what a claude.ai custom connector can send as a
    request header. Bind loopback and put Cloudflare Tunnel / Tailscale Funnel in front for remote use."""
    import hmac
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    app = server.streamable_http_app(host=host)

    class BearerGate(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if token:
                auth = request.headers.get("authorization", "")
                ok = auth.lower().startswith("bearer ") and hmac.compare_digest(auth[7:].strip(), token)
                if not ok:
                    return JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
            return await call_next(request)

    app.add_middleware(BearerGate)
    print(f"twincat-mcp streamable-http on http://{host}:{port}/mcp  (bearer token: {'required' if token else 'NONE - loopback only'})", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
