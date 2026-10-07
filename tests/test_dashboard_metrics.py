"""Synthetic rollups exercise both public aggregate surfaces without services."""
import contextlib
import http.client
import http.server
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
os.environ.setdefault("OBSIDIAN_VAULT", "/tmp/test_vault")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
import dashboard


def fixture_db(monkeypatch, rows=(), error=None):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    if error:
        cur.execute.side_effect = error
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    @contextlib.contextmanager
    def db():
        yield conn
    monkeypatch.setattr(dashboard, "db_conn", db)
    return cur


def test_utc_bucket_window_and_shared_aggregates(monkeypatch):
    now = datetime(2026, 10, 7, 10, 43, tzinfo=timezone.utc)
    cur = fixture_db(monkeypatch, [("local", "hybrid", "success", 2, 7, 120.0, 0, 0),
                                 ("local_fallback", "semantic", "error", 1, 0, 80.0, 1, 1)])
    metrics = dashboard.read_search_metrics(now)
    assert metrics["requests"] == 3
    assert metrics["results"] == 7
    assert metrics["errors"] == metrics["fallbacks"] == metrics["degraded"] == 1
    assert metrics["duration_ms"] == 200
    assert metrics["window_start"] == "2026-10-06T11:00:00+00:00"
    assert metrics["window_end"] == "2026-10-07T11:00:00+00:00"
    assert cur.execute.call_args.args[1] == (now.replace(minute=0) - timedelta(hours=23), now.replace(minute=0) + timedelta(hours=1))
    assert cur.execute.call_args_list[0].args == ("SET LOCAL statement_timeout = '1s'",)
    text = dashboard.render_search_metrics(metrics)
    assert '# TYPE osm_search_requests gauge' in text
    assert 'osm_search_requests{backend="local",mode="hybrid",outcome="success"} 2' in text
    assert 'osm_search_duration_seconds{backend="local",mode="hybrid",outcome="success"} 0.12' in text
    for name in ("results", "fallbacks", "errors"):
        assert f"osm_search_{name}" in text


def test_missing_table_returns_safe_zero_metrics(monkeypatch, caplog):
    fixture_db(monkeypatch, error=RuntimeError("secret query/path"))
    metrics = dashboard.read_search_metrics()
    assert metrics["requests"] == 0
    assert not metrics["available"]
    assert "secret" not in json.dumps(metrics) + caplog.text
    assert "osm_search_requests 0" in dashboard.render_search_metrics(metrics)


@pytest.mark.parametrize("row", [
    ('bad\"\npath', "hybrid", "success", 1, 0, 1, 0, 0),
    ("local", "query", "success", 1, 0, 1, 0, 0),
    ("local", "hybrid", "success", -1, 0, 1, 0, 0),
    ("local", "hybrid", "success", 1, 0, float("nan"), 0, 0),
])
def test_invalid_dimensions_or_numbers_never_export(monkeypatch, row):
    fixture_db(monkeypatch, [row])
    metrics = dashboard.read_search_metrics()
    assert metrics["series"] == []
    assert metrics["requests"] == 0


def test_prometheus_label_escaping():
    assert dashboard._prometheus_label('a\\b"c\nd') == 'a\\\\b\\"c\\nd'


def test_prometheus_preserves_large_counts_and_duration_precision(monkeypatch):
    fixture_db(monkeypatch, [("local", "hybrid", "success", 1234567, 2345678,
                             1234567.89, 123456, 0)])
    metrics = dashboard.read_search_metrics()
    text = dashboard.render_search_metrics(metrics)
    samples = {line.split(" ")[0].split("{")[0]: line.split(" ")[1]
               for line in text.splitlines() if not line.startswith("#")}
    assert samples["osm_search_requests"] == str(metrics["requests"])
    assert samples["osm_search_results"] == str(metrics["results"])
    assert float(samples["osm_search_duration_seconds"]) == metrics["duration_ms"] / 1000


def test_invalid_row_discards_partial_aggregates(monkeypatch):
    fixture_db(monkeypatch, [("local", "hybrid", "success", 4, 2, 10, 0, 0),
                             ("private/path", "hybrid", "success", 1, 1, 10, 0, 0)])
    metrics = dashboard.read_search_metrics()
    assert not metrics["available"]
    assert metrics["requests"] == 0
    assert metrics["series"] == []


def test_gather_stats_includes_shared_metrics(monkeypatch):
    fixture_db(monkeypatch)
    for name in ("_get_db_stats", "_get_vault_stats", "_get_ollama_stats"):
        monkeypatch.setattr(dashboard, name, lambda stats: None)
    @contextlib.contextmanager
    def lock():
        yield True
    monkeypatch.setattr(dashboard, "reindex_lock", lock)
    import server
    monkeypatch.setattr(server, "get_last_rebuild_failures", list)
    monkeypatch.setattr(server, "get_index_state", list)
    stats = dashboard.gather_stats()
    assert stats["search_metrics"]["available"]
    assert stats["search_metrics"]["requests"] == 0


def test_http_metrics_stats_and_unknown_route(monkeypatch):
    fixture_db(monkeypatch, [("caasiopeia", "keyword", "success", 4, 8, 20, 0, 0)])
    monkeypatch.setattr(dashboard, "gather_stats", lambda: {"search_metrics": dashboard.read_search_metrics()})
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        for path, content_type in (("/api/stats", "application/json"), ("/metrics", "text/plain"), ("/unknown", "text/html")):
            conn = http.client.HTTPConnection(*httpd.server_address, timeout=2)
            conn.request("GET", path)
            response = conn.getresponse()
            body = response.read().decode()
            assert response.status == 200
            assert content_type in response.getheader("Content-Type")
            if path == "/api/stats":
                assert json.loads(body)["search_metrics"]["requests"] == 4
            elif path == "/metrics":
                assert 'osm_search_requests{backend="caasiopeia",mode="keyword",outcome="success"} 4' in body
            conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_dashboard_cards_use_safe_dom_values():
    html = dashboard.HTML_PAGE
    assert 'id="v-search-requests"' in html
    assert "Current UTC hour + previous 23" in html
    assert "Heure UTC actuelle" in html
    assert "getElementById('v-search-requests').textContent" in html
    assert "getElementById('d-search-backends').textContent" in html
