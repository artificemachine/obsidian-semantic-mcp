"""Synthetic search outcomes and aggregate persistence never retain content."""

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from test_caasiopeia_retrieval import (
    API_KEY,
    MAIN_SOURCE,
    FakeCaas,
    make_passage,
    make_result,
)


@pytest.fixture
def metrics(monkeypatch):
    import server
    recorded = []
    monkeypatch.setattr(server, "_record_search_metric", lambda *args: recorded.append(args), raising=False)
    server._search_cache.invalidate()
    monkeypatch.setattr(server, "VAULT_PATHS", ["/v/main"])
    monkeypatch.setattr(server, "VAULT_PATH", "/v/main")
    server._init_retrieval_backend({}, ["/v/main"])
    yield recorded
    server._init_retrieval_backend({}, ["/v/main"])
    server._search_cache.invalidate()


def configure_caas(monkeypatch):
    import server
    fake = FakeCaas()
    monkeypatch.setattr(server, "CaasClient", lambda *a, **kw: fake)
    server._init_retrieval_backend({
        "OSM_RETRIEVAL_BACKEND": "caasiopeia",
        "CAASIOPEIA_BASE_URL": "http://caas.invalid:8080",
        "CAASIOPEIA_API_KEY": API_KEY,
        "CAASIOPEIA_SOURCE_MAP": f"main={MAIN_SOURCE}",
    }, ["/v/main"])
    return fake


def local_db(monkeypatch, rows):
    import server
    cur = MagicMock()
    cur.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    @contextlib.contextmanager
    def connection():
        yield conn
    monkeypatch.setattr(server, "db_conn", connection)
    monkeypatch.setattr(server, "embed", lambda query: [0.1])
    monkeypatch.setattr(server, "_rerank", lambda query, rows, limit: rows[:limit])
    return cur


def search(**args):
    import server
    return asyncio.run(server.call_tool("search_vault", {"query": "private query", **args}))


@pytest.mark.parametrize("degraded", [False, True])
def test_direct_caas_records_filtered_count_and_only_enums(metrics, monkeypatch, degraded):
    fake = configure_caas(monkeypatch)
    fake.result = make_result([make_passage(text="private result"), make_passage(score=0.1)], degraded=degraded)
    result = search(min_similarity=0.5)
    assert "private result" in result[0].text
    assert len(metrics) == 1
    state, duration = metrics[0]
    assert state == {"backend": "caasiopeia", "mode": "hybrid", "outcome": "success", "results": 1, "fallbacks": 0, "degraded": int(degraded)}
    assert duration >= 0
    assert "private" not in repr(metrics)


def test_direct_local_and_cache_hit_each_record_once(metrics, monkeypatch):
    local_db(monkeypatch, [("/v/main/a.md", "private result", 0.8), ("/v/main/b.md", "private result", 0.7)])
    first = search(mode="keyword")
    second = search(mode="keyword")
    assert first == second
    assert len(metrics) == 2
    assert all(state["backend"] == "local" and state["results"] == 2 and state["outcome"] == "success" for state, _ in metrics)


@pytest.mark.parametrize("local_failure", [False, True])
def test_nested_fallback_records_exactly_once(metrics, monkeypatch, local_failure):
    import caasiopeia_client as cc
    fake = configure_caas(monkeypatch)
    fake.error = cc.CaasUnavailable("synthetic outage")
    local_db(monkeypatch, [("/v/main/a.md", "synthetic text", 0.8)])
    if local_failure:
        import server
        monkeypatch.setattr(server, "embed", lambda query: (_ for _ in ()).throw(RuntimeError("synthetic failure")))
    result = search()
    assert len(metrics) == 1
    state, _ = metrics[0]
    assert state["backend"] == "local_fallback" and state["fallbacks"] == 1
    assert state["outcome"] == ("error" if local_failure else "success")
    assert state["results"] == (0 if local_failure else 1)
    assert "fallback from Caasiopeia" in result[0].text


def test_terminal_caas_error_records_error_without_fallback(metrics, monkeypatch):
    import caasiopeia_client as cc
    fake = configure_caas(monkeypatch)
    fake.error = cc.CaasUnauthorized("synthetic rejection")
    assert "Search error" in search()[0].text
    assert len(metrics) == 1
    assert metrics[0][0]["outcome"] == "error"
    assert metrics[0][0]["fallbacks"] == 0


def test_zero_results_is_success(metrics, monkeypatch):
    fake = configure_caas(monkeypatch)
    fake.result = make_result([])
    search(mode="untrusted label")
    assert metrics[0][0]["mode"] == "hybrid"
    assert metrics[0][0]["outcome"] == "success"
    assert metrics[0][0]["results"] == 0


def test_metrics_exception_does_not_change_search_result(metrics, monkeypatch, caplog):
    import server
    configure_caas(monkeypatch)
    def fail(*args):
        raise RuntimeError("private content must not appear in warning")
    monkeypatch.setattr(server, "_record_search_metric", fail)
    assert "passage text" in search()[0].text
    assert "private content" not in caplog.text
    assert "search metrics" in caplog.text


def state():
    return {"backend": "local", "mode": "keyword", "outcome": "success", "results": 2, "fallbacks": 0, "degraded": 0}


def persistence_db(monkeypatch):
    import server
    cur = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    @contextlib.contextmanager
    def connection():
        yield conn
    monkeypatch.setattr(server, "db_conn", connection)
    return cur


def test_hourly_upsert_utc_bucket_retention_and_parameters(monkeypatch):
    import server
    cur = persistence_db(monkeypatch)
    now = datetime(2026, 10, 7, 13, 59, tzinfo=timezone(timedelta(hours=2)))
    server._record_search_metric(state(), 12.5, now=now)
    calls = cur.execute.call_args_list
    upsert = next(call.args for call in calls if "INSERT INTO search_metrics_hourly" in call.args[0])
    hour = datetime(2026, 10, 7, 11, tzinfo=timezone.utc)
    assert upsert[1] == (hour, "local", "keyword", "success", 2, 12.5, 0, 0)
    assert "ON CONFLICT" in upsert[0] and "requests + 1" in upsert[0]
    deletion = next(call.args for call in calls if "DELETE FROM" in call.args[0])
    assert deletion[1] == (hour - timedelta(hours=719),)
    server._record_search_metric(state(), 5, now=now + timedelta(minutes=1))
    assert cur.execute.call_args_list[-1].args[1][0] == hour + timedelta(hours=1)


@pytest.mark.parametrize("field,value", [("backend", "private query"), ("mode", "private query"), ("outcome", "private query"), ("results", -1), ("fallbacks", 2), ("degraded", "private text")])
def test_invalid_metric_dimensions_fail_closed(monkeypatch, caplog, field, value):
    import server
    cur = persistence_db(monkeypatch)
    record = state()
    record[field] = value
    server._record_search_metric(record, 1)
    assert not cur.execute.called
    assert "private" not in caplog.text


def test_schema_has_only_bounded_aggregate_fields(monkeypatch):
    import server
    cur = persistence_db(monkeypatch)
    server._init_search_metrics()
    ddl = next(call.args[0] for call in cur.execute.call_args_list if "CREATE TABLE" in call.args[0])
    assert "PRIMARY KEY (hour, backend, mode, outcome)" in ddl
    assert "CHECK" in ddl
    assert all(word not in ddl.lower() for word in ("query", "path", "content", "trace"))


def test_metrics_schema_initializes_even_when_embedding_service_is_unavailable(monkeypatch):
    import server
    initialized = []
    monkeypatch.setattr(server.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(server, "_init_search_metrics", lambda: initialized.append(True))
    def unavailable():
        raise RuntimeError("synthetic embedding outage")
    monkeypatch.setattr(server, "get_embed_dim", unavailable)
    server.background_init([])
    assert initialized == [True]
