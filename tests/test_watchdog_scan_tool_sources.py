"""``watchdog_scan`` must serve Zotero too, not only Calibre.

The scanner exists — ``ZoteroWatchdogScanner`` runs daily through the scheduled
routine — but the MCP tool refused every non-Calibre source ("watchdog_scan is
currently Calibre-only"). So the capability was there and only the access from
a client was missing, which is the kind of gap that keeps Zotero a second-class
source relative to Calibre.

Two things are pinned here: the right scanner is chosen per source, and
``max_new`` reaches it. The cap matters more for Zotero than for Calibre — the
live queue holds 545 items and an uncapped run through a client's request
timeout would never finish.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _server(tmp_path, adapter_type):
    """A CalibreMCPServer wired to a source of the given adapter type."""
    from src.calibre_mcp.server import CalibreMCPServer

    srv = CalibreMCPServer.__new__(CalibreMCPServer)
    srv.library_path = tmp_path
    srv.rag_db_path = str(tmp_path / "rag_db")
    srv._archilles_dir = tmp_path / ".archilles"
    srv._archilles_dir.mkdir(parents=True, exist_ok=True)
    srv.adapter = SimpleNamespace(adapter_type=adapter_type)
    return srv


_RESULT = {
    "scanned": 7, "new_books": [], "metadata_changed": [],
    "annotations_changed": [], "delta_updates": 0, "total_time": 1.0,
    "errors": [],
}


class TestScannerChoice:
    def test_calibre_source_uses_the_calibre_scanner(self, tmp_path):
        srv = _server(tmp_path, "calibre")
        with patch("src.archilles.watchdog.WatchdogScanner") as cal, \
             patch("src.archilles.watchdog.ZoteroWatchdogScanner") as zot:
            cal.return_value.scan.return_value = dict(_RESULT)
            srv.watchdog_scan_tool(dry_run=True)

        assert cal.called
        assert not zot.called

    def test_zotero_source_uses_the_zotero_scanner(self, tmp_path):
        srv = _server(tmp_path, "zotero")
        with patch("src.archilles.watchdog.WatchdogScanner") as cal, \
             patch("src.archilles.watchdog.ZoteroWatchdogScanner") as zot:
            zot.return_value.scan.return_value = dict(_RESULT)
            result = srv.watchdog_scan_tool(dry_run=True)

        assert zot.called, "the Zotero scanner exists and must be used"
        assert not cal.called
        assert "error" not in result

    def test_unsupported_source_says_so_clearly(self, tmp_path):
        srv = _server(tmp_path, "obsidian")
        result = srv.watchdog_scan_tool(dry_run=True)

        assert "error" in result
        assert "obsidian" in result["error"]

    def test_no_adapter_still_scans_calibre(self, tmp_path):
        """Legacy single-source setups have no adapter object."""
        srv = _server(tmp_path, "calibre")
        srv.adapter = None
        with patch("src.archilles.watchdog.WatchdogScanner") as cal:
            cal.return_value.scan.return_value = dict(_RESULT)
            srv.watchdog_scan_tool(dry_run=True)

        assert cal.called


class TestMaxNewReachesTheScanner:
    """An uncapped Zotero run cannot finish inside a client request."""

    def test_zotero_receives_max_new(self, tmp_path):
        srv = _server(tmp_path, "zotero")
        with patch("src.archilles.watchdog.ZoteroWatchdogScanner") as zot:
            zot.return_value.scan.return_value = dict(_RESULT)
            srv.watchdog_scan_tool(index_new=True, max_new=5)

        assert zot.return_value.scan.call_args.kwargs["max_new"] == 5

    def test_calibre_receives_max_new(self, tmp_path):
        srv = _server(tmp_path, "calibre")
        with patch("src.archilles.watchdog.WatchdogScanner") as cal:
            cal.return_value.scan.return_value = dict(_RESULT)
            srv.watchdog_scan_tool(index_new=True, max_new=3)

        assert cal.return_value.scan.call_args.kwargs["max_new"] == 3

    def test_omitting_it_stays_uncapped(self, tmp_path):
        srv = _server(tmp_path, "calibre")
        with patch("src.archilles.watchdog.WatchdogScanner") as cal:
            cal.return_value.scan.return_value = dict(_RESULT)
            srv.watchdog_scan_tool(dry_run=True)

        assert cal.return_value.scan.call_args.kwargs["max_new"] is None


class TestUnifiedServerGate:
    """The unified server gated the tool behind ``calibre_sources``."""

    def _unified(self, adapter_types):
        from src.calibre_mcp.unified_server import UnifiedMCPServer

        servers = {}
        for name, atype in adapter_types.items():
            srv = SimpleNamespace(
                adapter=SimpleNamespace(adapter_type=atype),
                watchdog_scan_tool=lambda **kw: dict(_RESULT),
            )
            servers[name] = srv
        u = UnifiedMCPServer.__new__(UnifiedMCPServer)
        u.servers = servers
        u.default_source = next(iter(servers), None)
        return u

    def test_zotero_source_is_accepted(self):
        u = self._unified({"cal": "calibre", "zot": "zotero"})
        result = u.watchdog_scan_tool(source="zot", dry_run=True)

        assert "error" not in result
        assert result["source"] == "zot"

    def test_obsidian_source_is_still_refused(self):
        u = self._unified({"cal": "calibre", "lab": "obsidian"})
        result = u.watchdog_scan_tool(source="lab", dry_run=True)

        assert "error" in result
        assert "watchdog_sources" in result or "available" in str(result).lower()

    def test_unknown_source_reports_the_available_ones(self):
        u = self._unified({"cal": "calibre"})
        result = u.watchdog_scan_tool(source="nope", dry_run=True)

        assert "error" in result

    def test_watchdog_sources_lists_calibre_and_zotero(self):
        u = self._unified({"cal": "calibre", "zot": "zotero", "lab": "obsidian"})

        assert sorted(u.watchdog_sources) == ["cal", "zot"]
