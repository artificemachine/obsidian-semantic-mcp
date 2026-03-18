"""
Dashboard smoke tests.

Offline  — static analysis of the embedded HTML/JS source (no services needed).
Online   — live HTTP checks against the running stack (auto-skipped if unreachable).

Run all:
    uv run pytest tests/test_dashboard_smoke.py -v

Target a remote instance:
    DASHBOARD_URL=http://host:8484 uv run pytest tests/test_dashboard_smoke.py -v
"""
import os
import re
import time
from pathlib import Path

import pytest
import requests

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://localhost:8484")
_TIMEOUT = 10  # seconds for live HTTP calls
_SRC = Path(__file__).parent.parent / "src" / "dashboard.py"


# ── helpers ───────────────────────────────────────────────────────────────────

def _reachable() -> bool:
    try:
        requests.get(f"{DASHBOARD_URL}/", timeout=3)
        return True
    except Exception:
        return False


requires_live = pytest.mark.skipif(
    not _reachable(),
    reason=f"Dashboard not reachable at {DASHBOARD_URL} — start the stack first",
)


def _extract_html_page() -> str:
    """Read HTML_PAGE out of dashboard.py source without importing it.

    Importing dashboard triggers `from server import ...` which needs a live DB.
    Parsing the source avoids that dependency entirely.
    """
    src = _SRC.read_text()
    m = re.search(r'HTML_PAGE\s*=\s*"""(.*?)"""', src, re.DOTALL)
    assert m, "Could not locate HTML_PAGE triple-quoted string in dashboard.py"
    return m.group(1)


# ── Offline: static source analysis ──────────────────────────────────────────

class TestDashboardStatic:
    """No running services required — analyses dashboard.py source directly."""

    @pytest.fixture(scope="class")
    def html(self):
        return _extract_html_page()

    # ── JS string safety ──────────────────────────────────────────────────────

    def test_no_bare_newline_in_js_single_quoted_strings(self, html):
        """Regression: Python '\\n' inside a triple-quoted str renders as a literal
        newline, breaking JS parsing and silently killing all dashboard JS
        (introduced in commit 5fceb95, fixed this session)."""
        for block in re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL):
            for m in re.finditer(r"'([^'\\]*(?:\\.[^'\\]*)*)'", block):
                assert "\n" not in m.group(1), (
                    f"Bare newline inside JS single-quoted string literal: {m.group(0)!r}"
                )

    def test_no_bare_newline_in_js_double_quoted_strings(self, html):
        for block in re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL):
            for m in re.finditer(r'"([^"\\]*(?:\\.[^"\\]*)*)"', block):
                assert "\n" not in m.group(1), (
                    f"Bare newline inside JS double-quoted string literal: {m.group(0)!r}"
                )

    def test_script_block_exists(self, html):
        assert re.search(r"<script[^>]*>.*?</script>", html, re.DOTALL), \
            "No <script> block found in HTML_PAGE"

    # ── JS function presence ──────────────────────────────────────────────────

    def test_fetchstats_function_defined(self, html):
        assert "async function fetchStats" in html, \
            "fetchStats() missing — dashboard will never load stats"

    def test_fetch_has_abort_controller_timeout(self, html):
        assert "AbortController" in html, \
            "AbortController timeout guard missing — fetch can hang forever when services are down"

    def test_fetch_error_shows_osm_status_hint(self, html):
        assert "osm status" in html, \
            "fetch error handler should tell the user to run 'osm status'"

    # ── DOM element completeness ───────────────────────────────────────────────

    def test_all_getelementbyid_refs_exist_in_html(self, html):
        """Every getElementById('x') call must have a matching id='x' in the HTML."""
        referenced = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]", html))
        defined    = set(re.findall(r'id=["\']([^"\']+)["\']', html))
        missing = referenced - defined
        assert not missing, \
            f"getElementById refs without a matching id= attribute: {missing}"

    def test_required_stat_element_ids_present(self, html):
        """All IDs the JS writes to must exist in the HTML so stats actually render."""
        required = {
            "v-indexed", "v-vault", "v-gap", "v-orphaned", "v-dbsize",
            "v-last", "v-pgvec", "dot-db", "dot-ollama", "dot-model",
            "lbl-db", "lbl-ollama", "lbl-model", "err-db",
            "footer", "recent-list", "btn-reindex", "btn-rebuild",
        }
        defined = set(re.findall(r'id=["\']([^"\']+)["\']', html))
        missing = required - defined
        assert not missing, f"Required stat element IDs missing from HTML: {missing}"

    # ── Visual feedback ────────────────────────────────────────────────────────

    def test_status_dots_have_initial_grey_class(self, html):
        """Dots must start as grey so they're visible before the first fetch completes.
        Without this, the status badges appear blank on initial load."""
        dot_spans = re.findall(r'<span class="([^"]*)" id="dot-\w+"', html)
        assert dot_spans, "No dot span elements found"
        for cls in dot_spans:
            assert "grey" in cls, (
                f"Status dot starts without 'grey' class — invisible on load "
                f"(class='{cls}')"
            )


# ── Online: live HTTP smoke tests ─────────────────────────────────────────────

@requires_live
class TestDashboardLive:
    """Requires the full Docker stack. Auto-skipped when dashboard is unreachable."""

    def test_root_returns_html(self):
        r = requests.get(f"{DASHBOARD_URL}/", timeout=_TIMEOUT)
        assert r.status_code == 200
        assert "text/html" in r.headers.get("Content-Type", "")
        assert "Obsidian Semantic MCP" in r.text

    def test_stats_endpoint_returns_valid_json(self):
        r = requests.get(f"{DASHBOARD_URL}/api/stats", timeout=_TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict) and data

    def test_stats_has_all_required_keys(self):
        data = requests.get(f"{DASHBOARD_URL}/api/stats", timeout=_TIMEOUT).json()
        required = {
            "db_ok", "indexed_count", "vault_file_count", "unindexed_count",
            "orphaned_embeddings", "db_size_human", "last_indexed",
            "ollama_ok", "model_loaded", "pgvector_version", "pg_version",
            "recent_notes", "reindex_busy", "timestamp",
        }
        missing = required - data.keys()
        assert not missing, f"/api/stats response missing keys: {missing}"

    def test_stats_value_types(self):
        data = requests.get(f"{DASHBOARD_URL}/api/stats", timeout=_TIMEOUT).json()
        assert isinstance(data["db_ok"],           bool), "db_ok must be bool"
        assert isinstance(data["ollama_ok"],        bool), "ollama_ok must be bool"
        assert isinstance(data["model_loaded"],     bool), "model_loaded must be bool"
        assert isinstance(data["indexed_count"],    int),  "indexed_count must be int"
        assert isinstance(data["vault_file_count"], int),  "vault_file_count must be int"
        assert isinstance(data["recent_notes"],     list), "recent_notes must be list"
        assert isinstance(data["reindex_busy"],     bool), "reindex_busy must be bool"

    def test_stats_no_placeholder_dash_when_db_is_up(self):
        """'—' in a numeric field means the stat silently failed to populate."""
        data = requests.get(f"{DASHBOARD_URL}/api/stats", timeout=_TIMEOUT).json()
        if data["db_ok"]:
            assert data["db_size_human"] != "—", \
                "db_size_human is still '—' — DB stat query may have failed silently"
            assert data["pgvector_version"] != "—", \
                "pgvector_version is still '—' — pgvector extension may be missing"

    def test_all_services_healthy(self):
        data = requests.get(f"{DASHBOARD_URL}/api/stats", timeout=_TIMEOUT).json()
        errors = []
        if not data["db_ok"]:
            errors.append(f"PostgreSQL DOWN — {data.get('db_error', 'no detail')}")
        if not data["ollama_ok"]:
            errors.append(f"Ollama DOWN — {data.get('ollama_error', 'no detail')}")
        if not data["model_loaded"]:
            errors.append("Embedding model not loaded in Ollama")
        assert not errors, "Service health check failed:\n" + "\n".join(errors)

    def test_reindex_status_endpoint(self):
        r = requests.get(f"{DASHBOARD_URL}/api/reindex/status", timeout=_TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert "busy" in data
        assert isinstance(data["busy"], bool)

    def test_unknown_get_path_serves_html(self):
        """Unknown GET paths fall through to the HTML page (catch-all SPA pattern)."""
        r = requests.get(f"{DASHBOARD_URL}/api/does-not-exist", timeout=_TIMEOUT)
        assert r.status_code == 200
        assert "text/html" in r.headers.get("Content-Type", "")

    def test_unknown_post_path_returns_404(self):
        r = requests.post(f"{DASHBOARD_URL}/api/does-not-exist", timeout=_TIMEOUT)
        assert r.status_code == 404

    def test_stats_response_time(self):
        """Stats must respond within 8s — connect_timeout=5 + headroom.
        A longer response means a service is stalling (hung TCP connect)."""
        t0 = time.monotonic()
        requests.get(f"{DASHBOARD_URL}/api/stats", timeout=_TIMEOUT)
        elapsed = time.monotonic() - t0
        assert elapsed < 8.0, (
            f"/api/stats took {elapsed:.1f}s — a service may be stalling. "
            f"Run: osm status"
        )
