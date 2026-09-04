#!/usr/bin/env python3
"""
ARCHILLES MCP Server Entry Point

Starts the Calibre MCP Server with stdio transport for Claude Desktop integration.
"""

# CRITICAL: Configure logging FIRST - before ANY other imports.
# Libraries (e.g. calibre_mcp.server) call logging.basicConfig() at module level.
# If we let them run first, our own basicConfig() becomes a NO-OP and logs never
# reach the file handler. By configuring logging here with explicit handlers we
# also guarantee that ALL log output goes to sys.stderr, never to stdout, which
# keeps the stdout channel clean for JSON-RPC (MCP protocol).
import sys
import logging
from pathlib import Path

_log_file = Path.home() / ".archilles" / "mcp_server.log"
_log_file.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(str(_log_file), mode='a'),
        logging.StreamHandler(sys.stderr),  # explicit stderr - never stdout
    ],
)
logger = logging.getLogger(__name__)

# Redirect stdout to stderr AFTER logging is configured but BEFORE further imports.
# This catches any stray print() calls in third-party libraries loaded at import time
# so they cannot corrupt the JSON-RPC stream on stdout.
_original_stdout = sys.stdout
sys.stdout = sys.stderr

import asyncio
import json
import threading

# Repo-Wurzel auf den Pfad — kanonische Import-Wurzel ist `src.*` (Review 5.14)
sys.path.insert(0, str(Path(__file__).parent))

from src import __version__ as ARCHILLES_VERSION
from src.archilles.stdout_guard import redirect_stdout_to_stderr
from src.calibre_mcp.server import CalibreMCPServer, create_mcp_tools
from src.calibre_mcp.unified_server import UnifiedMCPServer, create_unified_tools

# Restore stdout for JSON-RPC communication
sys.stdout = _original_stdout


# ── Protocol versions ─────────────────────────────────────────────────────
# MCP 2026-07-28 dropped the initialize/initialized handshake: "modern"
# clients declare protocol version, client info and capabilities in ``_meta``
# on *every* request, so a request carries everything needed to serve it.
# "Legacy" clients (2025-11-25 and earlier) still open with ``initialize``.
#
# ARCHILLES is dual-era — it answers both on the same stdio process, as the
# spec permits. This costs us nothing: the server never held session state to
# begin with (tool calls read config and files, never prior requests).
MODERN_PROTOCOL_VERSIONS = ("2026-07-28",)
LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SUPPORTED_PROTOCOL_VERSIONS = MODERN_PROTOCOL_VERSIONS + LEGACY_PROTOCOL_VERSIONS
LATEST_LEGACY_PROTOCOL_VERSION = LEGACY_PROTOCOL_VERSIONS[0]

# Reserved ``_meta`` keys (spec 2026-07-28, "General fields")
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# JSON-RPC and MCP error codes
ERR_METHOD_NOT_FOUND = -32601
ERR_INTERNAL = -32603
ERR_INVALID_PARAMS = -32602
ERR_UNSUPPORTED_PROTOCOL_VERSION = -32022


TOOL_MAP = {
    'get_book_annotations': 'get_book_annotations_tool',
    'search_annotations': 'search_annotations_tool',
    'list_annotated_books': 'list_annotated_books_tool',
    'compute_annotation_hash': 'compute_hash_tool',
    'detect_duplicates': 'detect_duplicates_tool',
    'get_book_details': 'get_book_details_tool',
    'get_doublette_tag_instruction': 'get_doublette_tag_instruction_tool',
    'export_bibliography': 'export_bibliography_tool',
    'list_books_by_author': 'list_books_by_author_tool',
    'list_tags': 'list_tags_tool',
    'search_books_with_citations': 'search_books_with_citations_tool',
    'set_research_interests': 'set_research_interests_tool',
    'watchdog_scan': 'watchdog_scan_tool',
}


def _reconfigure_stdio_utf8(*streams) -> None:
    """Force UTF-8 (and line buffering) on the stdio transport streams.

    On Windows, Python decodes stdin with the locale code page (cp1252)
    while MCP clients send UTF-8 — 'Straßengewalt' arrived as
    'StraÃŸengewalt', corrupting every non-ASCII query before it reached
    the search engine. errors='replace' keeps a stray invalid byte from
    killing the read loop.
    """
    for stream in streams:
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(
                    encoding="utf-8", errors="replace", line_buffering=True
                )
            except Exception as e:
                logger.warning(f"Could not reconfigure stdio stream: {e}")


def _start_preload(server, enabled: bool) -> threading.Thread | None:
    """Warm the RAG stack in a background daemon thread (unless disabled).

    Keeps the stdio/HTTP loop responsive during startup: ``initialize`` and
    ``tools/list`` answer immediately while models load. Returns the thread
    (for tests), or None when preloading is disabled.
    """
    if not enabled:
        logger.info("Model preload disabled by config")
        return None
    thread = threading.Thread(
        target=server.preload, name="model-preload", daemon=True
    )
    thread.start()
    logger.info("Model preload started in background thread")
    return thread


def _dispatch_tool(server, tool_name: str, params: dict) -> dict:
    """Dispatch a tool call synchronously. Safe to run in a thread pool.

    ``server`` is either a :class:`CalibreMCPServer` (legacy single-source
    mode) or a :class:`UnifiedMCPServer` (multi-source mode). Tools that
    only exist on the legacy server (``get_doublette_tag_instruction``)
    return a structured error in unified mode rather than raising
    AttributeError.

    The call runs with stdout redirected to stderr (review 1.3). stdout is the
    JSON-RPC channel, and the code behind the tools is shared with the CLI,
    where printing is correct: the watchdog prints scan progress,
    ``index_book`` prints per-book lines, ``_cleanup_orphaned_books`` announces
    a deletion *before* its dry-run return. The service layer wrapped only the
    search paths, so ``watchdog_scan`` corrupted the stream even in dry-run
    mode. Guarding here rather than per tool means the next scan-adjacent tool
    inherits the protection instead of re-opening the hole.
    """
    method_name = TOOL_MAP.get(tool_name)
    if not method_name:
        return {'error': f'Unknown tool: {tool_name}'}
    method = getattr(server, method_name, None)
    if method is None:
        return {'error': f'Tool {tool_name!r} is not available in this server mode'}
    try:
        with redirect_stdout_to_stderr():
            return method(**params)
    except Exception as e:
        logger.error(f"Error in tool {tool_name}: {e}", exc_info=True)
        return {'error': str(e)}


def request_meta(request: dict) -> dict:
    """Return the ``_meta`` object of a request, or an empty dict."""
    params = request.get("params")
    if not isinstance(params, dict):
        return {}
    meta = params.get("_meta")
    return meta if isinstance(meta, dict) else {}


def is_modern_request(request: dict) -> bool:
    """True when the client speaks a handshake-free (2026-07-28+) revision.

    The presence of a protocol version in ``_meta`` is the era marker: only
    modern clients send one per request. Everything else — including a bare
    ``tools/list`` from a legacy client that skipped ``initialize`` — is
    served under legacy semantics, which are a superset here.
    """
    return META_PROTOCOL_VERSION in request_meta(request)


def negotiate_protocol_version(requested: str | None) -> str:
    """Pick the version to answer a legacy ``initialize`` with.

    Echoes the client's version when we support it, otherwise offers our
    newest legacy revision. A legacy client can never be answered with a
    modern version: those have no handshake for it to complete.
    """
    if requested in LEGACY_PROTOCOL_VERSIONS:
        return requested
    return LATEST_LEGACY_PROTOCOL_VERSION


def _validate_modern_meta(meta: dict) -> dict | None:
    """Return a JSON-RPC error object if per-request metadata is unusable."""
    version = meta.get(META_PROTOCOL_VERSION)
    if version not in MODERN_PROTOCOL_VERSIONS:
        return {
            "code": ERR_UNSUPPORTED_PROTOCOL_VERSION,
            "message": "Unsupported protocol version",
            "data": {
                "supported": list(SUPPORTED_PROTOCOL_VERSIONS),
                "requested": version,
            },
        }
    # clientCapabilities is required on every modern request; a missing one is
    # a malformed request, not an empty capability set.
    if not isinstance(meta.get(META_CLIENT_CAPABILITIES), dict):
        return {
            "code": ERR_INVALID_PARAMS,
            "message": f"Missing required _meta field: {META_CLIENT_CAPABILITIES}",
        }
    return None


def _server_info(server) -> dict:
    return {"name": server.instance_name, "version": ARCHILLES_VERSION}


def _result(payload: dict, server, modern: bool) -> dict:
    """Shape a result payload for the era the client speaks.

    Modern results declare a ``resultType`` and identify the server in
    ``_meta``; legacy results carry neither, since clients on those revisions
    learned the server's identity once, from ``initialize``.
    """
    if not modern:
        return payload
    return {
        "resultType": "complete",
        **payload,
        "_meta": {META_SERVER_INFO: _server_info(server)},
    }


def _tools_call_payload(server, params: dict) -> dict:
    """Run a tool call and wrap its outcome as MCP tool content."""
    tool_name = params.get("name")
    result = _dispatch_tool(server, tool_name, params.get("arguments") or {})
    payload = {
        "content": [
            {"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}
        ]
    }
    if isinstance(result, dict) and "error" in result:
        payload["isError"] = True
    return payload


def build_response(server, tools: list[dict], request: dict) -> dict | None:
    """Turn one JSON-RPC request into its response.

    Returns ``None`` for notifications, which take no reply. Free of I/O so
    the stdio loop and the tests can both drive it directly.
    """
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    modern = is_modern_request(request)

    if request_id is None:
        if method == "notifications/initialized":
            logger.info("Client sent initialized notification")
        else:
            logger.warning(f"Received notification: {method}")
        return None

    def _ok(payload: dict) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": _result(payload, server, modern)}

    def _fail(code: int, message: str, data: dict | None = None) -> dict:
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}

    if modern and (err := _validate_modern_meta(request_meta(request))):
        logger.warning(f"Rejecting {method}: {err['message']}")
        return {"jsonrpc": "2.0", "id": request_id, "error": err}

    if method == "server/discover":
        # Mandatory in 2026-07-28, and the probe dual-era clients use on stdio
        # to tell a modern server from a legacy one.
        return _ok({
            "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
            "capabilities": {"tools": {}},
        })

    if method == "initialize":
        if modern:
            # No handshake exists in modern revisions — an initialize carrying
            # modern _meta is a contradiction, not a request we can serve.
            return _fail(ERR_METHOD_NOT_FOUND, "Method not found: initialize")
        version = negotiate_protocol_version(params.get("protocolVersion"))
        logger.info(
            "Legacy initialize: client requested %r, answering %r",
            params.get("protocolVersion"), version,
        )
        return _ok({
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": _server_info(server),
        })

    if method == "tools/list":
        return _ok({"tools": tools})

    if method == "tools/call":
        if not params.get("name"):
            return _fail(ERR_INVALID_PARAMS, "Missing required parameter: name")
        return _ok(_tools_call_payload(server, params))

    return _fail(ERR_METHOD_NOT_FOUND, f"Method not found: {method}")


async def stdio_server(server, tools: list[dict]):
    """
    Run an MCP server using stdio transport.

    Reads JSON-RPC requests from stdin and writes responses to stdout.
    """
    logger.info("Starting ARCHILLES MCP Server (stdio mode)")

    # Capture the protocol channel ONCE, and write every response to this
    # object rather than resolving sys.stdout at write time. Several code
    # paths temporarily point sys.stdout at stderr to keep stray prints out of
    # the JSON-RPC stream (src/archilles/stdout_guard.py) — including the
    # background model-preload thread, which holds the redirect for as long as
    # a model takes to load. Resolving sys.stdout per write meant that during a
    # preload every response was written to stderr and the client saw nothing.
    # Observed live on a watchdog_scan call, 2026-09-04.
    out = sys.stdout

    # UTF-8 + line buffering (important on Windows: default stdin decoding
    # is the locale code page, which mangles non-ASCII queries)
    _reconfigure_stdio_utf8(sys.stdin, out)

    logger.info(f"Registered {len(tools)} tools")
    for tool in tools:
        logger.info(f"  - {tool['name']}: {tool['description'][:50]}...")

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        if not line.strip():
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {e}")
            continue

        request_id = request.get('id') if isinstance(request, dict) else None
        try:
            logger.info(f"Received request: {request.get('method') or 'unknown'}")
            response = build_response(server, tools, request)
        except Exception as e:
            logger.error(f"Error processing request: {e}", exc_info=True)
            response = {
                'jsonrpc': '2.0',
                'id': request_id if request_id is not None else -1,
                'error': {'code': ERR_INTERNAL, 'message': str(e)},
            }

        if response is None:
            continue

        out.write(json.dumps(response) + '\n')
        out.flush()
        logger.info(f"Sent response for request {request_id}")


async def sse_server(
    server,
    tools: list[dict],
    host: str = "127.0.0.1",
    port: int = 8765,
    auth_token: str | None = None,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
):
    """Run MCP server with SSE transport (for ChatGPT, Codex and other HTTP clients)."""
    import mcp.server as mcp_sdk
    import mcp.types as types
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Mount, Route
    import uvicorn

    logger.info(f"Starting ARCHILLES MCP Server (SSE mode) on {host}:{port}")

    mcp_srv = mcp_sdk.Server(server.instance_name)

    sdk_tools = [
        types.Tool(
            name=t["name"],
            description=t["description"],
            inputSchema=t["inputSchema"],
        )
        for t in tools
    ]

    @mcp_srv.list_tools()
    async def list_tools():
        return sdk_tools

    @mcp_srv.call_tool()
    async def call_tool(name: str, arguments: dict | None):
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: _dispatch_tool(server, name, arguments or {})
        )
        content = [
            types.TextContent(
                type="text",
                text=json.dumps(result, indent=2, ensure_ascii=False),
            )
        ]
        is_error = isinstance(result, dict) and 'error' in result
        return types.CallToolResult(content=content, isError=is_error)

    sse_transport = SseServerTransport("/messages/")
    init_options = mcp_srv.create_initialization_options()

    def _check_auth(request: Request) -> Response | None:
        if auth_token and request.headers.get("Authorization") != f"Bearer {auth_token}":
            return Response("Unauthorized", status_code=401)
        return None

    def _check_auth_scope(scope) -> Response | None:
        if not auth_token:
            return None
        headers = dict(scope.get("headers", []))
        auth_hdr = headers.get(b"authorization", b"").decode()
        if auth_hdr != f"Bearer {auth_token}":
            return Response("Unauthorized", status_code=401)
        return None

    async def handle_sse(request: Request):
        if (err := _check_auth(request)):
            return err
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as (read_stream, write_stream):
            await mcp_srv.run(read_stream, write_stream, init_options)
        # Starlette's Route wrapper awaits the handler's return value as an ASGI
        # response, so we must return one even though the client has already
        # disconnected by the time we get here.
        return Response(status_code=204)

    async def messages_app(scope, receive, send):
        """ASGI wrapper for ``SseServerTransport.handle_post_message``.

        Mounted (not Routed) so Starlette does not wrap it with
        ``request_response()``: ``handle_post_message`` is already a full ASGI
        app that writes its response via ``send`` and returns ``None`` —
        letting Starlette's wrapper then ``await None(...)`` raises
        ``TypeError: 'NoneType' object is not callable`` on every POST.
        """
        if (err := _check_auth_scope(scope)):
            await err(scope, receive, send)
            return
        await sse_transport.handle_post_message(scope, receive, send)

    app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=messages_app),
        ]
    )

    scheme = "https" if ssl_certfile else "http"
    logger.info(f"SSE endpoint: {scheme}://{host}:{port}/sse")
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        ssl_certfile=ssl_certfile or None,
        ssl_keyfile=ssl_keyfile or None,
    )
    userver = uvicorn.Server(config)
    try:
        await userver.serve()
    except OSError as e:
        if getattr(e, "errno", 0) in (98, 10048) or "address already in use" in str(e).lower():
            logger.error(f"Port {port} is already in use.")
            sys.stderr.write(f"\nERROR: Port {port} is already in use. Use --port to choose another.\n\n")
            sys.exit(1)
        raise


async def streamable_http_server(
    server,
    tools: list[dict],
    host: str = "127.0.0.1",
    port: int = 8765,
    auth_token: str | None = None,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
):
    """Run MCP server with Streamable HTTP transport (for ChatGPT Desktop and modern MCP clients)."""
    import uuid
    import anyio
    import mcp.server as mcp_sdk
    import mcp.types as types
    from mcp.server.streamable_http import StreamableHTTPServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Mount
    import uvicorn

    logger.info(f"Starting ARCHILLES MCP Server (Streamable HTTP mode) on {host}:{port}")

    mcp_srv = mcp_sdk.Server(server.instance_name)

    sdk_tools = [
        types.Tool(
            name=t["name"],
            description=t["description"],
            inputSchema=t["inputSchema"],
        )
        for t in tools
    ]

    @mcp_srv.list_tools()
    async def list_tools():
        return sdk_tools

    @mcp_srv.call_tool()
    async def call_tool(name: str, arguments: dict | None):
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: _dispatch_tool(server, name, arguments or {})
        )
        content = [
            types.TextContent(
                type="text",
                text=json.dumps(result, indent=2, ensure_ascii=False),
            )
        ]
        is_error = isinstance(result, dict) and "error" in result
        return types.CallToolResult(content=content, isError=is_error)

    init_options = mcp_srv.create_initialization_options()

    async def mcp_endpoint(scope, receive, send):
        if auth_token:
            headers = dict(scope.get("headers", []))
            auth_hdr = headers.get(b"authorization", b"").decode()
            if auth_hdr != f"Bearer {auth_token}":
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"Unauthorized", "more_body": False})
                return
        transport = StreamableHTTPServerTransport(mcp_session_id=str(uuid.uuid4()))
        async with transport.connect() as (read_stream, write_stream):
            async with anyio.create_task_group() as tg:
                tg.start_soon(mcp_srv.run, read_stream, write_stream, init_options)
                await transport.handle_request(scope, receive, send)
                tg.cancel_scope.cancel()

    app = Starlette(routes=[Mount("/mcp", app=mcp_endpoint)])

    scheme = "https" if ssl_certfile else "http"
    logger.info(f"MCP endpoint: {scheme}://{host}:{port}/mcp")

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        ssl_certfile=ssl_certfile or None,
        ssl_keyfile=ssl_keyfile or None,
    )
    userver = uvicorn.Server(config)
    try:
        await userver.serve()
    except OSError as e:
        if getattr(e, "errno", 0) in (98, 10048) or "address already in use" in str(e).lower():
            logger.error(f"Port {port} is already in use.")
            sys.stderr.write(f"\nERROR: Port {port} is already in use. Use --port to choose another.\n\n")
            sys.exit(1)
        raise


def main():
    """Main entry point — picks unified or legacy single-source mode."""
    import argparse
    import os

    parser = argparse.ArgumentParser(description="ARCHILLES MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default=None,
        help="Transport protocol (default: stdio, or from config.json)",
    )
    parser.add_argument("--host", default=None, help="SSE bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="SSE port (default: 8765)")
    parser.add_argument("--ssl-certfile", default=None, help="TLS certificate file (enables HTTPS)")
    parser.add_argument("--ssl-keyfile", default=None, help="TLS private key file (enables HTTPS)")
    args = parser.parse_args()

    # ── Mode selection: master config wins over legacy single-source ──
    from src.archilles.config import load_master_config, master_config_path

    try:
        master = load_master_config()
    except (ValueError, json.JSONDecodeError) as e:
        logger.error("Master config %s is malformed: %s", master_config_path(), e)
        sys.stderr.write(f"\nERROR: Master config malformed: {e}\n\n")
        sys.exit(1)

    if master is not None:
        server, tools, transport_cfg, preload_enabled = _init_unified(master)
    else:
        server, tools, transport_cfg, preload_enabled = _init_legacy_single_source()

    _start_preload(server, preload_enabled)

    # Resolve transport from CLI args → config → default (stdio)
    transport_mode = args.transport or transport_cfg.get("mode", "stdio")
    sse_host = args.host or transport_cfg.get("host", "127.0.0.1")
    sse_port = args.port or transport_cfg.get("port", 8765)
    sse_auth_token = transport_cfg.get("auth_token")
    sse_certfile = args.ssl_certfile or transport_cfg.get("ssl_certfile")
    sse_keyfile = args.ssl_keyfile or transport_cfg.get("ssl_keyfile")

    if transport_mode == "sse":
        logger.info(f"Transport: SSE ({sse_host}:{sse_port})")
        asyncio.run(sse_server(server, tools, sse_host, sse_port, sse_auth_token, sse_certfile, sse_keyfile))
    elif transport_mode == "streamable-http":
        logger.info(f"Transport: Streamable HTTP ({sse_host}:{sse_port})")
        asyncio.run(streamable_http_server(server, tools, sse_host, sse_port, sse_auth_token, sse_certfile, sse_keyfile))
    else:
        logger.info("Transport: stdio")
        asyncio.run(stdio_server(server, tools))


def _init_unified(master):
    """Build the unified multi-source server from a parsed master config."""
    server = UnifiedMCPServer.from_master_config(master)
    tools = create_unified_tools(server)
    logger.info(
        "Mode: unified (sources: %s, default: %r)",
        server.source_names, server.default_source,
    )
    return server, tools, dict(master.transport), master.preload_models


def _init_legacy_single_source():
    """Build the single-source server from ARCHILLES_LIBRARY_PATH + library config."""
    import os
    from src.archilles.config import master_config_path

    library_path = os.getenv('ARCHILLES_LIBRARY_PATH') or os.getenv('CALIBRE_LIBRARY_PATH')

    if not library_path:
        master_path = master_config_path()
        logger.error("Neither master config nor ARCHILLES_LIBRARY_PATH is set")
        sys.stderr.write("\n" + "=" * 60 + "\n")
        sys.stderr.write("ERROR: No configuration found\n")
        sys.stderr.write("=" * 60 + "\n\n")
        sys.stderr.write("Provide either:\n\n")
        sys.stderr.write(f"  A) Master config at {master_path} for multi-source mode, OR\n")
        sys.stderr.write("  B) Set ARCHILLES_LIBRARY_PATH for single-source mode:\n\n")
        sys.stderr.write("  Windows (PowerShell):\n")
        sys.stderr.write('    $env:ARCHILLES_LIBRARY_PATH = "C:\\path\\to\\Library"\n\n')
        sys.stderr.write("  Linux/macOS:\n")
        sys.stderr.write('    export ARCHILLES_LIBRARY_PATH="/path/to/Library"\n\n')
        sys.stderr.write("  Claude Desktop (claude_desktop_config.json):\n")
        sys.stderr.write('    "env": {"ARCHILLES_LIBRARY_PATH": "/path/to/Library"}\n\n')
        sys.stderr.flush()
        sys.exit(1)

    config_path = Path(library_path) / ".archilles" / "config.json"
    config = {}
    if config_path.exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            logger.info(f"Loaded config from {config_path}")
        except Exception as e:
            logger.warning(f"Failed to load config: {e}")

    library_path = config.get('library_path', config.get('calibre_library_path', library_path))
    archilles_dir = Path(library_path) / ".archilles"
    instance_name = config.get('instance_name', 'archilles')

    rag_db_path = os.getenv('RAG_DB_PATH') or config.get('rag_db_path', str(archilles_dir / "rag_db"))

    adapter_type = config.get('adapter')  # None = auto-detect
    try:
        from src.adapters import create_adapter
        adapter = create_adapter(Path(library_path), adapter_type)
        logger.info(f"Adapter: {adapter.adapter_type} (instance: {instance_name})")
    except Exception as e:
        logger.warning(f"Could not create adapter: {e} — continuing without adapter")
        adapter = None

    logger.info(f"Library path: {library_path}")
    logger.info(f"Instance: {instance_name}")
    logger.info(f"RAG database path: {rag_db_path}")

    from src.archilles.config import resolve_enable_reranking
    enable_reranking = resolve_enable_reranking(config.get('enable_reranking'))
    reranker_device = config.get('reranker_device', 'cpu')
    if enable_reranking:
        logger.info(f"Cross-encoder reranking enabled (device: {reranker_device})")

    from src.citation.config import CitationConfig
    citation_config = CitationConfig.from_dict(config.get('citation', {}))
    logger.info(f"Citation style: {citation_config.label} (locale: {citation_config.locale})")

    server = CalibreMCPServer(
        library_path=library_path,
        annotations_dir=None,
        rag_db_path=rag_db_path,
        enable_reranking=enable_reranking,
        reranker_device=reranker_device,
        citation_config=citation_config,
        adapter=adapter,
        instance_name=instance_name,
    )
    logger.info(
        "Mode: legacy single-source — server %r (%s)",
        instance_name, adapter.adapter_type if adapter else "no adapter",
    )
    tools = create_mcp_tools(server)
    preload_enabled = bool(config.get("preload_models", True))
    return server, tools, config.get("transport", {}), preload_enabled


if __name__ == '__main__':
    main()