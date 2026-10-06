import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parent.parent / "scripts" / "deliver-branch-from-bundle.sh"


def test_delivery_script_documents_required_bundle_and_git_references():
    result = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "usage: deliver-branch-from-bundle.sh BUNDLE BRANCH BASE_SHA TARGET_SHA" in result.stdout


def test_delivery_script_rejects_missing_delivery_references():
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "usage: deliver-branch-from-bundle.sh" in result.stderr


def test_delivery_script_pins_the_mandatory_scanner_and_remote():
    content = SCRIPT.read_text()

    assert 'REMOTE_URL="https://github.com/artificemachine/obsidian-semantic-mcp.git"' in content
    assert 'GITLEAKS_BIN="/usr/local/bin/gitleaks"' in content
    assert '"$GITLEAKS_BIN" git --redact --no-banner' in content
    assert "credential.https://github.com.helper" in content
