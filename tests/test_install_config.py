"""The installers keep the agent's config, which can hold its token, from
other users: install.sh makes config.yaml mode 600, install.ps1 lets only
Administrators and SYSTEM read it.

install.sh's config step is top-level script code, so the test cuts it out
between its section comments and runs it in bash against a temporary folder.
The token is invented.
"""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "fake-token-1234"


def _config_step() -> str:
    source = (ROOT / "install.sh").read_text()
    match = re.search(
        r"^# Write config \(skip if keeping existing config\)\n# -+\n(.*?)^# -+\n"
        r"# Platform-specific service installation",
        source, re.MULTILINE | re.DOTALL)
    assert match, "the config step was not found in install.sh"
    return match.group(1)


def _run_config_step(folder: Path, keep: bool) -> subprocess.CompletedProcess:
    script = f"""
set -e
info() {{ :; }}; success() {{ :; }}
INSTALL_CFG='{folder}'
KEEP_CONFIG={"true" if keep else "false"}
PORT=9099; SCAN_INTERVAL=60s; TOKEN='{TOKEN}'
ADV_IFACE=''; STANDBY_MODE=''; EXCLUDE_YAML=''; FS_YAML=''
{_config_step()}
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize(("existing", "keep"), [(False, False), (True, False), (True, True)])
def test_install_sh_config_is_readable_by_its_owner_only(tmp_path, existing, keep):
    folder = tmp_path / "smartha-agent"
    config = folder / "config.yaml"
    if existing:
        folder.mkdir()
        config.write_text("port: 9099\n")
        config.chmod(0o644)  # as an older installer left it
    result = _run_config_step(folder, keep)
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert (TOKEN in config.read_text()) is not keep


def test_install_ps1_restricts_the_config_before_writing_it():
    source = (ROOT / "install.ps1").read_text()
    assert '"*S-1-5-32-544:(F)" "*S-1-5-18:(F)"' in source  # Administrators, SYSTEM
    assert "/inheritance:r" in source
    step = source[source.index("# Write config (skipped when keeping existing)"):]
    step = step[:step.index("Write-Ok \"Config written.\"")]
    keep, write = step.split("} else {", 1)
    assert "Protect-ConfigFile $ConfigFile" in keep
    assert write.index("Protect-ConfigFile $ConfigFile") < write.index("Set-Content -Path $ConfigFile")
