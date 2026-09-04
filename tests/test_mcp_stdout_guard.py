"""stdout must never carry anything but JSON-RPC (review 1.3).

``mcp_server.py`` restores the real stdout after import so the protocol can use
it, and the service layer wraps the *search* paths in a redirect. The scan paths
were never wrapped: ``watchdog_scan`` calls ``scanner.scan()`` unguarded, and
``_cleanup_orphaned_books`` prints its "N indexed book(s) …" line *before* the
``if dry_run: return`` — so even a dry run corrupted the stream.

Guarding one tool would leave the trap set for the next scan-adjacent tool, so
the guard sits at the single dispatch point every tool passes through.
"""

import io
import sys

import pytest

import mcp_server
from src.archilles.stdout_guard import redirect_stdout_to_stderr


class _NoisyServer:
    """A server whose tool prints — as the watchdog paths legitimately do."""

    def noisy_tool(self, **kwargs):
        print("🗑️  3 indexed book(s) no longer in the library")
        return {"ok": True, "args": kwargs}

    def quiet_tool(self, **kwargs):
        return {"ok": True}

    def raising_tool(self, **kwargs):
        print("half a line before the failure")
        raise RuntimeError("scan blew up")


@pytest.fixture
def tools(monkeypatch):
    """Register the noisy test tools in the dispatch map."""
    monkeypatch.setitem(mcp_server.TOOL_MAP, "noisy_tool", "noisy_tool")
    monkeypatch.setitem(mcp_server.TOOL_MAP, "quiet_tool", "quiet_tool")
    monkeypatch.setitem(mcp_server.TOOL_MAP, "raising_tool", "raising_tool")


class TestDispatchGuard:
    """Asserted through pytest's own capture rather than a hand-swapped
    ``sys.stdout``: the guard points stdout *at* stderr, and only capsys sees
    both sides of that as the process does."""

    def test_tool_output_does_not_reach_stdout(self, tools, capsys):
        result = mcp_server._dispatch_tool(_NoisyServer(), "noisy_tool", {})
        captured = capsys.readouterr()

        assert captured.out == "", "stdout must stay clean for JSON-RPC"
        assert "no longer in the library" in captured.err
        assert result == {"ok": True, "args": {}}

    def test_stdout_is_restored_after_the_call(self, tools):
        before = sys.stdout
        mcp_server._dispatch_tool(_NoisyServer(), "noisy_tool", {})

        assert sys.stdout is before, "the protocol channel must come back"

    def test_stdout_is_restored_when_a_tool_raises(self, tools, capsys):
        before = sys.stdout
        result = mcp_server._dispatch_tool(_NoisyServer(), "raising_tool", {})
        captured = capsys.readouterr()

        assert sys.stdout is before
        assert captured.out == ""
        assert "error" in result

    def test_unknown_tool_still_returns_a_structured_error(self, tools, capsys):
        result = mcp_server._dispatch_tool(_NoisyServer(), "no_such_tool", {})

        assert result == {"error": "Unknown tool: no_such_tool"}
        assert capsys.readouterr().out == ""

    def test_arguments_and_results_pass_through_unchanged(self, tools):
        result = mcp_server._dispatch_tool(_NoisyServer(), "noisy_tool", {"a": 1})
        assert result["args"] == {"a": 1}


class TestRedirectHelper:
    """The refcounted helper moved out of the service layer so the MCP entry
    point can use it without importing the service."""

    def test_nesting_restores_only_at_the_outermost_exit(self, monkeypatch):
        out, err = io.StringIO(), io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)

        with redirect_stdout_to_stderr():
            assert sys.stdout is err
            with redirect_stdout_to_stderr():
                assert sys.stdout is err
            assert sys.stdout is err, "the inner exit must not restore"
        assert sys.stdout is out

    def test_restores_on_exception(self, monkeypatch):
        out, err = io.StringIO(), io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)

        with pytest.raises(ValueError):
            with redirect_stdout_to_stderr():
                raise ValueError("boom")
        assert sys.stdout is out

    def test_service_layer_uses_the_same_helper(self):
        """One refcount, not two — two independent counters would race."""
        from src.service import archilles_service

        assert archilles_service._redirect_stdout_to_stderr is redirect_stdout_to_stderr


class TestLibraryLayerDoesNotPrint:
    """``warn_if_light_plan_hides_hierarchy`` fires on *every* scan in this
    library; a library-layer function has no business writing to stdout."""

    class _Plan:
        mode = "light"

    class _Store:
        def has_parent_chunks(self):
            return True

    def test_hint_goes_to_the_log_not_stdout(self, capsys, caplog):
        from src.archilles.execution import warn_if_light_plan_hides_hierarchy

        with caplog.at_level("WARNING"):
            warned = warn_if_light_plan_hides_hierarchy(self._Plan(), self._Store())

        assert warned is True
        assert capsys.readouterr().out == ""
        assert "hierarchical" in caplog.text

    def test_silent_when_the_plan_is_not_light(self, capsys):
        from src.archilles.execution import warn_if_light_plan_hides_hierarchy

        class _Full:
            mode = "full-local"

        assert warn_if_light_plan_hides_hierarchy(_Full(), self._Store()) is False
        assert capsys.readouterr().out == ""
