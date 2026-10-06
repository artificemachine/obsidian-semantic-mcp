"""
search_vault routed through Caasiopeia (OSM_RETRIEVAL_BACKEND=caasiopeia).

Every test mocks CaasClient: no socket, no Postgres, no Ollama. embed() and
db_conn() are replaced with tripwires so any local retrieval work fails loudly.
"""
import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

os.environ.setdefault("OBSIDIAN_VAULT", "/tmp/test_vault")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("OLLAMA_URL", "http://localhost:11434")

import caasiopeia_client as cc

MAIN_SOURCE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SIDE_SOURCE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
API_KEY = f"synthetic-{uuid.uuid4().hex}"
TRACE = "00000000-0000-4000-8000-0000000000aa"


def make_passage(external_id="notes/foo.md", score=0.8, text="passage text",
                 source_id=MAIN_SOURCE, heading_path=("Top", "Sub")):
    return cc.Passage(
        chunk_id=str(uuid.uuid4()),
        document_id=str(uuid.uuid4()),
        source_id=source_id,
        external_id=external_id,
        title=None,
        heading_path=tuple(heading_path),
        ordinal=0,
        score=score,
        text=text,
        tokens=3,
    )


def make_result(passages, degraded=False, reason=None):
    return cc.ContextResult(
        passages=tuple(passages),
        tokens_used=3,
        cache="miss",
        trace_id=TRACE,
        degraded=degraded,
        degradation_reason=reason,
        model_id="fixture",
    )


class FakeCaas:
    """Stands in for CaasClient; records calls, returns or raises a script."""

    def __init__(self, *args, **kwargs):
        self.calls = []
        self.result = make_result([make_passage()])
        self.error = None

    def search(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def caas(monkeypatch):
    """Server configured for the caasiopeia backend over two vaults."""
    import server

    fake = FakeCaas()

    def no_local(*args, **kwargs):
        raise AssertionError("local retrieval must not run in caasiopeia mode")

    monkeypatch.setattr(server, "CaasClient", lambda *a, **kw: fake)
    monkeypatch.setattr(server, "embed", no_local)
    monkeypatch.setattr(server, "db_conn", no_local)
    monkeypatch.setattr(server, "VAULT_PATHS", ["/v/main", "/v/side"])
    monkeypatch.setattr(server, "_VAULT_LIST", ["/v/main", "/v/side"])
    monkeypatch.setattr(server, "VAULT_PATH", "/v/main")
    server._search_cache.invalidate()
    server._init_retrieval_backend(
        {
            "OSM_RETRIEVAL_BACKEND": "caasiopeia",
            "CAASIOPEIA_BASE_URL": "http://caas.invalid:8080",
            "CAASIOPEIA_API_KEY": API_KEY,
            "CAASIOPEIA_SOURCE_MAP": f"main={MAIN_SOURCE},side={SIDE_SOURCE}",
            "CAASIOPEIA_TOKEN_BUDGET": "1234",
        },
        ["/v/main", "/v/side"],
    )
    yield fake
    server._init_retrieval_backend({}, ["/v/main"])


def search(server, **arguments):
    arguments.setdefault("query", "find the plan")
    content = asyncio.run(server.call_tool("search_vault", arguments))
    return "\n".join(part.text for part in content)


@pytest.mark.parametrize(
    "osm_mode, caas_mode",
    [("hybrid", "hybrid"), ("semantic", "dense"), ("keyword", "lexical")],
)
def test_search_vault_maps_hybrid_semantic_and_keyword_modes_to_caas(
    caas, osm_mode, caas_mode
):
    import server

    text = search(server, mode=osm_mode)

    assert len(caas.calls) == 1
    call = caas.calls[0]
    assert call["mode"] == caas_mode
    assert call["query"] == "find the plan"
    assert call["token_budget"] == 1234
    assert "passage text" in text


def test_unknown_mode_keeps_the_established_hybrid_normalization(caas):
    import server

    search(server, mode="fuzzy")

    assert caas.calls[0]["mode"] == "hybrid"


def test_search_vault_selects_only_the_configured_source_for_a_vault(caas):
    import server

    search(server, vault="side")
    assert caas.calls[-1]["source_ids"] == [SIDE_SOURCE]

    search(server, vault="/v/main")
    assert caas.calls[-1]["source_ids"] == [MAIN_SOURCE]


def test_unfiltered_search_scopes_to_every_configured_vault_source(caas):
    import server

    search(server)

    assert sorted(caas.calls[0]["source_ids"]) == sorted([MAIN_SOURCE, SIDE_SOURCE])


def test_unknown_vault_is_rejected_before_any_caas_request(caas):
    import server

    text = search(server, vault="nope")

    assert "No vault matching 'nope'" in text
    assert caas.calls == []


def test_search_vault_preserves_limit_threshold_and_empty_result_semantics(caas):
    import server

    caas.result = make_result(
        [make_passage(f"n{i}.md", score=1.0 - i * 0.1, text=f"text {i}") for i in range(6)]
    )

    capped = search(server, limit=2)
    assert "text 0" in capped and "text 1" in capped
    assert "text 2" not in capped

    thresholded = search(server, limit=10, min_similarity=0.75)
    assert "text 0" in thresholded and "text 2" in thresholded
    assert "text 3" not in thresholded

    caas.result = make_result([])
    empty = search(server)
    assert "no results" in empty.lower()
    assert "reindex" not in empty.lower()

    caas.result = make_result([make_passage(score=0.1)])
    below = search(server, min_similarity=0.9)
    assert "min_similarity" in below
    assert "reindex" not in below.lower()


def test_output_renders_provenance_and_prefixes_the_vault_when_there_are_several(caas):
    import server

    caas.result = make_result([
        make_passage("notes/foo.md", 0.83, "alpha", MAIN_SOURCE, ("Top", "Sub")),
        make_passage("bar.md", 0.5, "beta", SIDE_SOURCE, ()),
    ])

    text = search(server)

    assert text.startswith("_Retrieval backend: Caasiopeia._")
    assert "**main/notes/foo.md**" in text
    assert "Top > Sub" in text
    assert "score: 0.83" in text
    assert "**side/bar.md**" in text
    assert "alpha" in text and "beta" in text


def test_degraded_answers_say_so(caas):
    import server

    caas.result = make_result([make_passage()], degraded=True, reason="lexical_only")

    text = search(server)

    assert "degraded" in text.lower()
    assert "lexical_only" in text


@pytest.mark.parametrize(
    "error, expected",
    [
        (cc.CaasUnauthorized("Caasiopeia rejected the credential (HTTP 401)", trace_id=TRACE, status=401),
         "CAASIOPEIA_API_KEY"),
        (cc.CaasInvalidResponse("passage.score missing or of the wrong type", trace_id=TRACE),
         "invalid response"),
    ],
)
def test_search_vault_does_not_fall_back_to_local_results_for_non_retryable_caas_failures(
    caas, error, expected
):
    import server

    caas.error = error

    text = search(server)

    assert expected.lower() in text.lower()
    assert TRACE in text
    assert "local" in text.lower(), "the message must say local ranking was not used"
    assert API_KEY not in text
    assert "Traceback" not in text


@pytest.mark.parametrize(
    "error",
    [
        cc.CaasUnavailable("Caasiopeia is unavailable (HTTP 503)", trace_id=TRACE, status=503),
        cc.CaasTimeout("Caasiopeia request timed out", trace_id=TRACE),
    ],
)
def test_search_vault_falls_back_to_local_results_for_temporary_caas_failures(
    caas, monkeypatch, error
):
    import server

    calls = []

    async def local_search(query, limit, min_similarity, mode, vault_filter, vault_ids, graph_expand):
        calls.append((query, limit, min_similarity, mode, vault_filter, vault_ids, graph_expand))
        return [server.TextContent(type="text", text="local fallback result")]

    monkeypatch.setattr(server, "_search_vault_local", local_search, raising=False)
    caas.error = error

    text = search(server)

    assert text.startswith("_Retrieval backend: local (fallback from Caasiopeia)._")
    assert text.endswith("local fallback result")
    assert calls == [("find the plan", 5, 0.0, "hybrid", "", None, False)]


def test_retrieval_provenance_marks_direct_local_results():
    import server

    results = server._with_retrieval_provenance(
        [server.TextContent(type="text", text="local result")], "local"
    )

    assert results[0].text == "_Retrieval backend: local._\n\nlocal result"


def test_an_unexpected_client_exception_is_reported_without_a_traceback(caas):
    import server

    caas.error = RuntimeError(f"boom {API_KEY}")

    text = search(server)

    assert "server log" in text
    assert API_KEY not in text
    assert "Traceback" not in text


def test_caasiopeia_answers_are_not_served_from_the_local_result_cache(caas):
    import server

    search(server)
    search(server)

    assert len(caas.calls) == 2


def test_local_backend_is_untouched_by_the_caas_branch(monkeypatch):
    """Explicit local backend still reaches the local embedding path."""
    import server

    server._init_retrieval_backend({"OSM_RETRIEVAL_BACKEND": "local"}, ["/v/main"])
    monkeypatch.setattr(server, "VAULT_PATHS", ["/v/main"])

    class Reached(Exception):
        pass

    def tripwire(*args, **kwargs):
        raise Reached

    monkeypatch.setattr(server, "embed", tripwire)
    server._search_cache.invalidate()

    text = search(server, query="local only query")

    assert text.startswith("_Retrieval backend: local._")
    assert "Search error:" in text


def test_a_rejected_client_configuration_is_reported_not_raised(caas, monkeypatch):
    import server

    def rejects(*args, **kwargs):
        raise ValueError("invalid Caasiopeia base URL")

    monkeypatch.setattr(server, "CaasClient", rejects)
    server._CAAS_CLIENT = None

    text = search(server)

    assert "not configured" in text
    assert "Local ranking was not used" in text


# ── Iteration 3: verified wikilink expansion ─────────────────────────────────

@pytest.fixture
def caas_fs(caas, tmp_path, monkeypatch):
    """The caasiopeia backend over two real temporary vault directories."""
    import server

    main, side = tmp_path / "main", tmp_path / "side"
    (main / "notes").mkdir(parents=True)
    side.mkdir()
    (main / "notes" / "foo.md").write_text("# foo\n", encoding="utf-8")
    (main / "notes" / "dup.md").write_text("# dup\n", encoding="utf-8")
    (side / "bar.md").write_text("# bar\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("# secret\n", encoding="utf-8")
    (main / "notes" / "escape.md").symlink_to(outside)
    (main / "notes" / "adir.md").mkdir()

    paths = [str(main), str(side)]
    monkeypatch.setattr(server, "VAULT_PATHS", paths)
    monkeypatch.setattr(server, "_VAULT_LIST", paths)
    monkeypatch.setattr(server, "VAULT_PATH", paths[0])
    server._init_retrieval_backend(
        {
            "OSM_RETRIEVAL_BACKEND": "caasiopeia",
            "CAASIOPEIA_BASE_URL": "http://caas.invalid:8080",
            "CAASIOPEIA_API_KEY": API_KEY,
            "CAASIOPEIA_SOURCE_MAP": f"main={MAIN_SOURCE},side={SIDE_SOURCE}",
        },
        paths,
    )
    return main, side


def test_caasiopeia_passage_external_id_resolves_to_one_configured_vault_path(caas_fs):
    import server

    main, side = caas_fs

    assert server._resolve_caas_seed_path(
        make_passage("notes/foo.md", source_id=MAIN_SOURCE)
    ) == str(main / "notes" / "foo.md")
    assert server._resolve_caas_seed_path(
        make_passage("bar.md", source_id=SIDE_SOURCE)
    ) == str(side / "bar.md")
    # The same relative id under the other source is a different (missing) file.
    assert server._resolve_caas_seed_path(
        make_passage("bar.md", source_id=MAIN_SOURCE)
    ) is None


@pytest.mark.parametrize(
    "external_id, source_id",
    [
        ("../outside.md", MAIN_SOURCE),
        ("notes/../../outside.md", MAIN_SOURCE),
        ("/etc/passwd", MAIN_SOURCE),
        ("C:/Windows/win.ini", MAIN_SOURCE),
        ("notes\\foo.md", MAIN_SOURCE),
        ("notes//foo.md", MAIN_SOURCE),
        ("./notes/foo.md", MAIN_SOURCE),
        ("", MAIN_SOURCE),
        ("notes/foo.md\x00.png", MAIN_SOURCE),
        ("notes/missing.md", MAIN_SOURCE),
        ("notes/escape.md", MAIN_SOURCE),   # symlink leaving the vault
        ("notes/adir.md", MAIN_SOURCE),     # a directory, not a note
        ("notes/foo.md", "cccccccc-cccc-4ccc-8ccc-cccccccccccc"),  # unmapped source
    ],
)
def test_caasiopeia_graph_expand_rejects_path_escape_and_unmapped_external_id(
    caas_fs, caas, monkeypatch, external_id, source_id
):
    import server

    seen = []
    monkeypatch.setattr(server, "expand_via_links", lambda paths, hops=1: seen.append(paths) or [])
    passage = make_passage(external_id, source_id=source_id)

    assert server._resolve_caas_seed_path(passage) is None

    caas.result = make_result([passage])
    text = search(server, graph_expand=True)

    assert seen == [], "an unverified passage must never seed graph expansion"
    assert "Traceback" not in text


def test_caasiopeia_graph_expand_uses_only_verified_seed_paths(caas_fs, caas, monkeypatch):
    import server

    main, side = caas_fs
    caas.result = make_result([
        make_passage("notes/foo.md", 0.9, "alpha", MAIN_SOURCE),
        make_passage("notes/foo.md", 0.8, "alpha again", MAIN_SOURCE),   # duplicate
        make_passage("../outside.md", 0.7, "evil", MAIN_SOURCE),         # escape
        make_passage("bar.md", 0.6, "beta", SIDE_SOURCE),
    ])
    calls = []

    def fake_expand(paths, hops=1):
        calls.append((list(paths), hops))
        return [(str(main / "notes" / "dup.md"), "neighbor body", str(main / "notes" / "foo.md"))]

    monkeypatch.setattr(server, "expand_via_links", fake_expand)

    text = search(server, graph_expand=True, limit=10)

    assert calls == [([str(main / "notes" / "foo.md"), str(side / "bar.md")], 1)]
    assert "**Wikilink neighbors** _(connected notes not in top results)_" in text
    assert "**main/notes/dup.md** _(linked via main/notes/foo.md)_" in text
    assert "neighbor body" in text


def test_graph_expand_is_off_unless_requested(caas_fs, caas, monkeypatch):
    import server

    monkeypatch.setattr(
        server, "expand_via_links",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not expand")),
    )

    text = search(server)

    assert "passage text" in text


def test_a_graph_projection_failure_keeps_the_caasiopeia_results(caas_fs, caas, monkeypatch):
    import server

    caas.result = make_result([make_passage("notes/foo.md", 0.9, "alpha", MAIN_SOURCE)])

    def broken(paths, hops=1):
        raise RuntimeError("db down")

    monkeypatch.setattr(server, "expand_via_links", broken)

    text = search(server, graph_expand=True)

    assert "alpha" in text
    assert "wikilink expansion" in text.lower()
    assert "unavailable" in text.lower()


# ── Source sync subfolder (CAASIOPEIA_SOURCE_ROOTS) ──────────────────────────

@pytest.fixture
def caas_rooted(caas, tmp_path, monkeypatch):
    """A vault whose Caasiopeia source was synced from its ``notes`` subfolder."""
    import server

    vault = tmp_path / "main"
    (vault / "notes" / "10_ai").mkdir(parents=True)
    (vault / "notes" / "10_ai" / "spec.md").write_text("# spec\n", encoding="utf-8")
    (vault / "other").mkdir()
    (vault / "other" / "elsewhere.md").write_text("# elsewhere\n", encoding="utf-8")
    (vault / "vaultroot.md").write_text("# root\n", encoding="utf-8")
    (vault / "notes" / "sneaky.md").symlink_to(vault / "other" / "elsewhere.md")

    paths = [str(vault)]
    monkeypatch.setattr(server, "VAULT_PATHS", paths)
    monkeypatch.setattr(server, "_VAULT_LIST", paths)
    monkeypatch.setattr(server, "VAULT_PATH", paths[0])
    server._init_retrieval_backend(
        {
            "OSM_RETRIEVAL_BACKEND": "caasiopeia",
            "CAASIOPEIA_BASE_URL": "http://caas.invalid:8080",
            "CAASIOPEIA_API_KEY": API_KEY,
            "CAASIOPEIA_SOURCE_MAP": f"main={MAIN_SOURCE}",
            "CAASIOPEIA_SOURCE_ROOTS": "main=notes",
        },
        paths,
    )
    return vault


def test_source_root_prefix_makes_subfolder_external_ids_resolve(caas_rooted):
    import server

    seed = server._resolve_caas_seed_path(make_passage("10_ai/spec.md"))

    assert seed == str(caas_rooted / "notes" / "10_ai" / "spec.md")


def test_source_root_prefix_rejects_ids_that_are_not_under_the_sync_root(caas_rooted):
    import server

    # Vault-root-relative id: exists in the vault, but was never in this source.
    assert server._resolve_caas_seed_path(make_passage("vaultroot.md")) is None
    assert server._resolve_caas_seed_path(make_passage("notes/10_ai/spec.md")) is None
    assert server._resolve_caas_seed_path(make_passage("../other/elsewhere.md")) is None


def test_source_root_prefix_rejects_symlink_leaving_the_sync_root_inside_the_vault(caas_rooted):
    import server

    assert server._resolve_caas_seed_path(make_passage("sneaky.md")) is None
