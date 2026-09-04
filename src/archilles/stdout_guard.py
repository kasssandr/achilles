"""Keep stdout clean for JSON-RPC (review 1.3).

The MCP server speaks JSON-RPC over stdout: one stray ``print`` anywhere in the
call tree corrupts the stream and the client sees a protocol error rather than
the text. That is a hard boundary — `CLAUDE.md` states it for
``src/calibre_mcp/server.py`` — but it cannot be kept by discipline alone,
because the paths behind the tools are shared with the CLI, where printing is
the *right* behaviour: `watchdog.py` prints its scan progress, `index_book`
prints per-book lines, `_cleanup_orphaned_books` announces a deletion.

So the redirect belongs at the boundary, not in the printing code. This module
holds it on its own, with no heavy imports, so both the service layer and the
MCP entry point use the *same* refcount — two independent counters would race
and could leave stdout permanently pointed at stderr.
"""

from __future__ import annotations

import sys
import threading
from contextlib import contextmanager
from typing import Any

_redirect_lock = threading.Lock()
_redirect_depth = 0
_redirect_original_stdout: Any = None


@contextmanager
def redirect_stdout_to_stderr():
    """Temporarily redirect stdout to stderr (prevents MCP JSON-RPC corruption).

    Thread-safe via refcount: the first concurrent enter captures and replaces
    sys.stdout under a lock; subsequent enters increment the counter without
    touching sys.stdout. The original is restored only when the last holder
    exits. This keeps cross-source fan-out parallelism intact while preventing
    the save/restore race that previously could leave stdout permanently
    pointed at stderr.
    """
    global _redirect_depth, _redirect_original_stdout
    with _redirect_lock:
        if _redirect_depth == 0:
            _redirect_original_stdout = sys.stdout
            sys.stdout = sys.stderr
        _redirect_depth += 1
    try:
        yield
    finally:
        with _redirect_lock:
            _redirect_depth -= 1
            if _redirect_depth == 0:
                sys.stdout = _redirect_original_stdout
                _redirect_original_stdout = None
