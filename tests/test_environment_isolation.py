"""The test environment never inherits the host's saved retrieval selection.

``src/server.py`` calls ``load_dotenv`` on the installed ``.env`` at import time,
so a host with the Caasiopeia backend selected leaks ``OSM_RETRIEVAL_BACKEND``
and the ``CAASIOPEIA_*`` variables into ``os.environ`` during collection, before
any fixture runs. The assignments below reproduce that leak on every host, so
the test fails without the autouse fixture in conftest.py (issue #81).
"""
import os

_LEAKED_AT_COLLECTION = {
    "OSM_RETRIEVAL_BACKEND": "caasiopeia",
    "OSM_CAASIOPEIA_HOST_URL": "http://leaked.invalid",
    "CAASIOPEIA_BASE_URL": "http://leaked.invalid",
    "CAASIOPEIA_API_KEY": "leaked-placeholder",
    "CAASIOPEIA_SOURCE_MAP": "vault=leaked",
}
os.environ.update(_LEAKED_AT_COLLECTION)


def test_collection_time_retrieval_variables_are_not_visible_to_tests():
    visible = [name for name in _LEAKED_AT_COLLECTION if name in os.environ]
    assert visible == []


def test_default_deploy_dir_is_not_the_hosts_installed_stack(tmp_path):
    import osm_init

    assert osm_init._default_deploy_dir().is_relative_to(tmp_path)


def test_dashboard_token_file_is_not_the_hosts_real_one(tmp_path):
    import osm_init

    assert osm_init._DASHBOARD_TOKEN_FILE.is_relative_to(tmp_path)
    assert osm_init._OSM_CONFIG_DIR.is_relative_to(tmp_path)


def test_launcher_project_root_pointer_is_not_the_hosts_real_one(tmp_path):
    from src import launcher

    assert launcher.PROJECT_ROOT_FILE.is_relative_to(tmp_path)


def test_setup_project_root_is_the_checkout_not_the_hosts_installed_stack():
    import osm_init

    assert osm_init.PROJECT_ROOT == osm_init._CODE_DIR


def test_a_test_can_still_select_the_backend_explicitly(monkeypatch):
    monkeypatch.setenv("OSM_RETRIEVAL_BACKEND", "caasiopeia")
    assert os.environ["OSM_RETRIEVAL_BACKEND"] == "caasiopeia"
