"""Replacing the agent binary on an update: retried for a while, then a clear stop.

install.ps1: after the service stops, Windows can keep the old .exe open for
a moment (the service manager, an antivirus scan), so a failed copy is
retried for up to 10 seconds. install.sh: the binary is copied next to the
old one and renamed over it, so the old file is never written to (a running
binary on Linux, the cached code signature on Apple silicon), and a failed
step is retried for up to 10 seconds. Both then stop with a clear message.
The helpers are taken out of the scripts and run with a copy that fails on
purpose; the installers themselves are never run here.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"
INSTALL_PS1 = ROOT / "install.ps1"
PWSH = os.environ.get("PWSH") or shutil.which("pwsh")
needs_pwsh = pytest.mark.skipif(PWSH is None, reason="pwsh not installed (set PWSH)")


# --- install.sh ---------------------------------------------------------------------


def _sh_function(name: str) -> str:
    source = INSTALL_SH.read_text()
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", source, re.M | re.S)
    assert match, name
    return match.group(0)


def test_install_sh_puts_the_binary_in_place_only_through_replace_binary():
    source = INSTALL_SH.read_text()
    assert re.search(r'^if ! replace_binary "\$TMPDIR/\$BINARY_FILE" "\$INSTALL_BIN"; then\n'
                     r'  fail "Could not replace \$INSTALL_BIN after 10 seconds: ', source, re.M)
    copies = [line for line in source.splitlines()
              if re.match(r"\s*cp\b", line) and "INSTALL_BIN" in line]
    assert copies == [], copies


def _run_replace(tmp_path: Path, failures: int, tries: int = 10) -> tuple[subprocess.CompletedProcess, Path]:
    """Runs replace_binary with a cp that fails `failures` times first and a
    sleep that only records the call."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    real_cp = shutil.which("cp")
    (stubs / "cp").write_text(
        "#!/bin/sh\n"
        f"n=$(cat '{tmp_path}/cp.count' 2>/dev/null || echo 0); n=$((n + 1)); echo $n > '{tmp_path}/cp.count'\n"
        f"if [ $n -le {failures} ]; then echo \"cp: cannot create regular file '$2': No space left on device\" >&2; "
        "exit 1; fi\n"
        f"exec {real_cp} \"$@\"\n")
    (stubs / "sleep").write_text(f"#!/bin/sh\necho \"$1\" >> '{tmp_path}/sleeps'\n")
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    folder = tmp_path / "bin"
    folder.mkdir()
    src = tmp_path / "new-agent"
    src.write_text("new build\n")
    dest = folder / "smartha-agent"
    dest.write_text("old build\n")
    script = (
        "set -e\n"
        "warn() { echo \"WARN $*\"; }\n"
        + _sh_function("replace_binary")
        + 'if replace_binary "$1" "$2"; then echo replaced; else echo "failed: $REPLACE_ERROR"; fi\n'
    )
    result = subprocess.run(["bash", "-c", script, "t", str(src), str(dest)], capture_output=True,
                            text=True, check=False, timeout=30,
                            env={"PATH": f"{stubs}:/usr/bin:/bin", "REPLACE_TRIES": str(tries)})
    return result, dest


def test_install_sh_a_failed_copy_is_retried_until_it_works(tmp_path):
    result, dest = _run_replace(tmp_path, failures=3)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "WARN Could not replace the binary yet; retrying for up to 10 seconds...", "replaced"]
    assert dest.read_text() == "new build\n"
    assert (tmp_path / "cp.count").read_text().strip() == "4"
    assert (tmp_path / "sleeps").read_text().split() == ["1", "1", "1"]
    assert sorted(p.name for p in dest.parent.iterdir()) == ["smartha-agent"]


def test_install_sh_the_new_binary_is_a_new_file_and_the_old_one_is_never_written(tmp_path):
    """A process still holding the old binary keeps reading the old bytes:
    the old file was renamed over, not overwritten."""
    folder = tmp_path / "bin"
    folder.mkdir()
    dest = folder / "smartha-agent"
    dest.write_text("old build\n")
    old_inode = dest.stat().st_ino
    src = tmp_path / "new-agent"
    src.write_text("new build\n")
    with dest.open() as held:
        script = ("set -e\nwarn() { echo \"WARN $*\"; }\n" + _sh_function("replace_binary")
                  + 'replace_binary "$1" "$2" && echo replaced\n')
        result = subprocess.run(["bash", "-c", script, "t", str(src), str(dest)], capture_output=True,
                                text=True, check=False, timeout=30)
        assert result.stdout.splitlines() == ["replaced"], result.stdout + result.stderr
        assert held.read() == "old build\n"
    assert dest.read_text() == "new build\n"
    assert dest.stat().st_ino != old_inode
    assert sorted(p.name for p in folder.iterdir()) == ["smartha-agent"]


def test_install_sh_a_copy_that_works_at_once_does_not_wait(tmp_path):
    result, dest = _run_replace(tmp_path, failures=0)
    assert result.stdout.splitlines() == ["replaced"]
    assert dest.read_text() == "new build\n"
    assert not (tmp_path / "sleeps").exists()


def test_install_sh_a_copy_that_keeps_failing_gives_up_after_ten_tries(tmp_path):
    result, dest = _run_replace(tmp_path, failures=99)
    assert result.returncode == 0, result.stderr
    last = result.stdout.splitlines()[-1]
    assert last.startswith("failed: cp: cannot create regular file") and last.endswith("No space left on device")
    assert dest.read_text() == "old build\n"
    assert sorted(p.name for p in dest.parent.iterdir()) == ["smartha-agent"]
    assert (tmp_path / "cp.count").read_text().strip() == "10"
    assert len((tmp_path / "sleeps").read_text().split()) == 9


# --- install.ps1 --------------------------------------------------------------------


def test_install_ps1_puts_the_binary_in_place_only_through_copy_agent_binary():
    source = INSTALL_PS1.read_text()
    assert ("$CopyError = Copy-AgentBinary -Source $BinaryTmp -Destination $BinaryPath "
            "-ExpectedHash $SourceHash") in source
    assert 'Write-Fail "Could not replace $BinaryPath after 10 seconds: $CopyError.' in source
    copies = [line for line in source.splitlines()
              if "Copy-Item" in line and "$BinaryPath" in line]
    assert copies == [], copies


PS_HARNESS = r"""
param($Installer, $Work, $Busy, $Timeout, $Interval, $Expect)
$errs = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$null, [ref]$errs)
if ($errs.Count) { exit 3 }
foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    if ($f.Name -eq 'Copy-AgentBinary') { . ([scriptblock]::Create($f.Extent.Text)) }
}
$script:calls = 0
$script:warns = @()
function Write-Warn { param($msg) $script:warns += $msg }
# A copy that fails the way a locked .exe does, $Busy times, then copies.
function Copy-Item {
    param([string]$LiteralPath, [string]$Destination, [switch]$Force, $ErrorAction)
    $script:calls++
    if ($script:calls -le [int]$Busy) {
        throw [System.IO.IOException]::new("The process cannot access the file '$Destination' because it is being used by another process.")
    }
    Microsoft.PowerShell.Management\Copy-Item -LiteralPath $LiteralPath -Destination $Destination -Force
}
$src = Join-Path $Work 'new.exe'; $dst = Join-Path $Work 'smartha-agent.exe'
Set-Content -LiteralPath $src -Value 'new build' -NoNewline
Set-Content -LiteralPath $dst -Value 'old build' -NoNewline
$hash = if ($Expect -eq 'wrong') { 'F' * 64 } else { (Get-FileHash -LiteralPath $src -Algorithm SHA256).Hash }
$started = Get-Date
$result = Copy-AgentBinary -Source $src -Destination $dst -ExpectedHash $hash -TimeoutSeconds ([double]$Timeout) -IntervalMilliseconds ([int]$Interval)
"result=[$result]"
"calls=$($script:calls)"
"warns=$($script:warns.Count)"
"warn=$($script:warns -join '|')"
"content=$(Get-Content -LiteralPath $dst -Raw)"
"elapsed_ms=$([int]((Get-Date) - $started).TotalMilliseconds)"
"""


def _ps(tmp_path: Path, busy: int, timeout: float = 10, interval: int = 50,
        expect: str = "right") -> dict:
    script = tmp_path / "harness.ps1"
    script.write_text(PS_HARNESS)
    work = tmp_path / "work"
    work.mkdir()
    result = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(script),
                             str(INSTALL_PS1), str(work), str(busy), str(timeout), str(interval), expect],
                            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


@needs_pwsh
def test_install_ps1_a_locked_binary_is_retried_until_it_is_free(tmp_path):
    out = _ps(tmp_path, busy=3)
    assert out["result"] == "[]"
    assert out["calls"] == "4" and out["content"] == "new build"
    assert out["warns"] == "1"
    assert out["warn"] == "The old binary is still in use; retrying for up to 10 seconds..."


@needs_pwsh
def test_install_ps1_a_free_binary_is_copied_at_once(tmp_path):
    out = _ps(tmp_path, busy=0)
    assert out["result"] == "[]" and out["calls"] == "1" and out["warns"] == "0"


@needs_pwsh
def test_install_ps1_a_binary_that_stays_locked_gives_up_with_the_error(tmp_path):
    out = _ps(tmp_path, busy=10**6, timeout=0.6, interval=100)
    assert out["result"] == ("[The process cannot access the file '"
                             f"{tmp_path / 'work' / 'smartha-agent.exe'}' because it is being used "
                             "by another process]")
    assert out["content"] == "old build"
    assert 4 <= int(out["calls"]) <= 9, out["calls"]
    assert 550 <= int(out["elapsed_ms"]) < 5000, out["elapsed_ms"]
    assert out["warns"] == "1"


@needs_pwsh
def test_install_ps1_a_copy_that_does_not_match_the_download_is_not_accepted(tmp_path):
    out = _ps(tmp_path, busy=0, timeout=0.3, interval=100, expect="wrong")
    assert out["result"] == "[the file in place does not match the download]"
    assert int(out["calls"]) >= 2
