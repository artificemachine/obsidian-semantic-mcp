"""The live-stack smoke test only runs against the installed stack when the owner opts in (issue #81).

test_docker_exec_smoke_against_live_mcp_server runs ``docker compose exec`` in the real mcp-server
container of ~/.local/share/obsidian-semantic-mcp. Without a gate it did so on every suite run,
including every pre-commit hook. It is read-only, but it is the last way the suite reaches the real
deployment, so it must be asked for.
"""
import subprocess

import pytest

import test_dimension_migration as live


def test_the_live_stack_smoke_skips_without_the_opt_in_variable_and_runs_no_docker(monkeypatch):
    monkeypatch.delenv(live.LIVE_STACK_OPT_IN, raising=False)

    def no_docker(*args, **kwargs):
        raise AssertionError("docker must not run without the opt-in variable")

    monkeypatch.setattr(subprocess, "run", no_docker)
    with pytest.raises(pytest.skip.Exception) as skipped:
        live.test_docker_exec_smoke_against_live_mcp_server()
    assert live.LIVE_STACK_OPT_IN in str(skipped.value)


def test_the_opt_in_variable_lets_the_live_stack_smoke_proceed(monkeypatch):
    monkeypatch.setenv(live.LIVE_STACK_OPT_IN, "1")
    assert live._require_live_stack_opt_in() is None


@pytest.mark.parametrize("value", ["", "0", "true", "yes"])
def test_only_the_exact_value_one_opts_in(monkeypatch, value):
    monkeypatch.setenv(live.LIVE_STACK_OPT_IN, value)
    with pytest.raises(pytest.skip.Exception):
        live._require_live_stack_opt_in()
