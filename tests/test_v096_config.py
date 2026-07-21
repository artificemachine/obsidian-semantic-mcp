import os
import sys
from pathlib import Path
from unittest.mock import patch

# Ensure we can import from src
sys.path.insert(0, str(Path(__file__).parent.parent))

from src import launcher

def test_project_root_discovery_from_config(tmp_path, monkeypatch):
    """Test that _project_root finds the path in the config file."""
    config_dir = tmp_path / ".config" / "obsidian-semantic-mcp"
    config_dir.mkdir(parents=True)
    root_file = config_dir / "project_root"
    root_file.write_text("/mock/project/root")

    # Mock Path.home() to return our tmp_path
    with patch("pathlib.Path.home", return_value=tmp_path):
        # Need to reload or update the global constants in launcher if they were already initialized
        launcher.OSM_CONFIG_DIR = tmp_path / ".config" / "obsidian-semantic-mcp"
        launcher.PROJECT_ROOT_FILE = launcher.OSM_CONFIG_DIR / "project_root"
        
        root = launcher._project_root()
        assert str(root) == "/mock/project/root"

def test_docker_mode_implicit_discovery(tmp_path, monkeypatch):
    """Test that launcher enables Docker mode if config file exists."""
    config_dir = tmp_path / ".config" / "obsidian-semantic-mcp"
    config_dir.mkdir(parents=True)
    root_file = config_dir / "project_root"
    root_file.write_text(str(tmp_path))

    monkeypatch.delenv("OSM_DOCKER", raising=False)
    monkeypatch.delenv("OSM_PROJECT_ROOT", raising=False)
    
    # Mocking necessary parts for launcher.main()
    with patch("pathlib.Path.home", return_value=tmp_path), \
         patch("src.launcher._docker_info_ok", return_value=True), \
         patch("src.launcher._container_id", return_value="abc123"), \
         patch("src.launcher._exec_into_container") as mock_exec:
        
        launcher.OSM_CONFIG_DIR = tmp_path / ".config" / "obsidian-semantic-mcp"
        launcher.PROJECT_ROOT_FILE = launcher.OSM_CONFIG_DIR / "project_root"
        
        launcher.main()
        mock_exec.assert_called_once()

def test_docker_mode_explicit_disable(tmp_path, monkeypatch):
    """Test that OSM_DOCKER=0 disables Docker mode even if config file exists."""
    config_dir = tmp_path / ".config" / "obsidian-semantic-mcp"
    config_dir.mkdir(parents=True)
    root_file = config_dir / "project_root"
    root_file.write_text(str(tmp_path))

    monkeypatch.setenv("OSM_DOCKER", "0")
    
    with patch("pathlib.Path.home", return_value=tmp_path), \
         patch("src.launcher._run_server") as mock_run, \
         patch("src.launcher._validate_env"): # avoid exit
        
        launcher.OSM_CONFIG_DIR = tmp_path / ".config" / "obsidian-semantic-mcp"
        launcher.PROJECT_ROOT_FILE = launcher.OSM_CONFIG_DIR / "project_root"
        
        launcher.main()
        mock_run.assert_called_once()

def test_osm_init_writes_project_root(tmp_path):
    """Test that osm_init._write_project_root_config writes the correct path."""
    import osm_init
    
    # Mock Path.home() to return our tmp_path
    with patch("pathlib.Path.home", return_value=tmp_path):
        osm_init.OSM_CONFIG_DIR = tmp_path / ".config" / "obsidian-semantic-mcp"
        osm_init.PROJECT_ROOT_FILE = osm_init.OSM_CONFIG_DIR / "project_root"
        osm_init.PROJECT_ROOT = Path("/some/abs/root")
        
        osm_init._write_project_root_config()
        
        assert osm_init.PROJECT_ROOT_FILE.exists()
        assert osm_init.PROJECT_ROOT_FILE.read_text().strip() == "/some/abs/root"

def test_launcher_loads_dotenv_from_root(tmp_path, monkeypatch):
    """Test that launcher loads .env from the discovered project root."""
    env_file = tmp_path / ".env"
    env_file.write_text("TEST_VAR=discovered_value")
    
    # Mock _project_root to return our tmp_path
    with patch("src.launcher._project_root", return_value=tmp_path),          patch("src.launcher._run_server"),          patch("src.launcher._validate_env"):
        
        # Disable Docker mode for this test so we hit the local path
        monkeypatch.setenv("OSM_DOCKER", "0")
        
        launcher.main()
        assert os.environ.get("TEST_VAR") == "discovered_value"
