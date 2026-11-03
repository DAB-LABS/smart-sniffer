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


def _helpers() -> str:
    """The functions the config step calls (v1.0: the carry-over on a rewrite)."""
    source = (ROOT / "install.sh").read_text()
    return "".join(
        re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", source, re.MULTILINE | re.DOTALL).group(0)
        for name in ("carry_config_blocks", "config_block_keys"))


def _run_config_step(folder: Path, keep: bool) -> subprocess.CompletedProcess:
    script = f"""
set -e
info() {{ :; }}; success() {{ :; }}
{_helpers()}
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
    # v1.0: a rewritten file is saved first, root only too; a kept one is not.
    backups = list(folder.glob("config.yaml.bak-*")) if folder.exists() else []
    if existing and not keep:
        assert len(backups) == 1 and stat.S_IMODE(backups[0].stat().st_mode) == 0o600
        assert backups[0].read_text() == "port: 9099\n"
    else:
        assert backups == []


def test_install_ps1_restricts_the_config_before_writing_it():
    source = (ROOT / "install.ps1").read_text()
    assert '"*S-1-5-32-544:(F)" "*S-1-5-18:(F)"' in source  # Administrators, SYSTEM
    assert "/inheritance:r" in source
    step = source[source.index("# Write config (skipped when keeping existing)"):]
    step = step[:step.index("Write-Ok \"Config written.\"")]
    keep, write = step.split("} else {", 1)
    assert "Protect-ConfigFile $ConfigFile" in keep
    assert write.index("Protect-ConfigFile $ConfigFile") < write.index("Set-Content -Path $ConfigFile")
