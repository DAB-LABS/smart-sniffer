"""install.sh --yes / -y and install.ps1 SMARTHA_YES / -Yes (v1.0, item H).

--yes takes the default answer at every prompt, exactly as if Enter was
pressed each time, and never reads stdin or /dev/tty. One code path for
install and update: on an update "Keep current settings? [Y/n]" defaults to
yes, so the config is kept. Without --yes, a /dev/tty that exists but cannot
be opened (ssh without a pty) falls back to the non-interactive defaults
instead of failing at the first prompt.

The static checks always run. The full runs need root, Linux, unshare and
setsid: install.sh runs in a private mount namespace (tmpfs over the system
paths it writes), in a new session with no controlling terminal and stdin
from /dev/null, with stand-ins for curl (the download and the health check),
smartctl and systemctl on PATH. Nothing outside the test's own folder is
touched. The token is invented.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"
INSTALL_PS1 = ROOT / "install.ps1"
TOKEN = "fake-token-5678"


# --- install.sh, read --------------------------------------------------------------


def test_every_prompt_goes_through_ask():
    """Every prompt reads through ask, which reads nothing with --yes."""
    source = INSTALL_SH.read_text()
    reads = [line.strip() for line in source.splitlines()
             if re.search(r"\bread -\w*p", line) and not line.strip().startswith("#")]
    assert reads == ['read -rp "$2" "$1" < "$TTY_IN"'], reads
    body = re.search(r"^ask\(\) \{\n(.*?)^\}", source, re.M | re.S).group(1)
    yes_branch = body.split("fi", 1)[0]
    assert "ASSUME_YES" in yes_branch and "read" not in yes_branch and "/dev/tty" not in yes_branch


def test_the_options_and_their_help():
    source = INSTALL_SH.read_text()
    assert "--yes|-y) ASSUME_YES=true ;;" in source
    assert re.search(r'case "\$\{SMARTHA_YES:-\}" in\n\s+1\|true', source)
    result = subprocess.run(["bash", str(INSTALL_SH), "--help"], capture_output=True, text=True,
                            check=False, stdin=subprocess.DEVNULL, timeout=20)
    assert result.returncode == 0
    assert "--yes, -y" in result.stdout and "SMARTHA_YES=1" in result.stdout
    assert "| sudo bash -s -- --yes" in result.stdout


@pytest.mark.parametrize(("option", "message"), [
    ("--y", "Unknown option --y. Did you mean -y or --yes? See --help."),
    ("-yes", "Unknown option -yes. Did you mean -y or --yes? See --help."),
    ("--Yes", "Unknown option --Yes. Did you mean -y or --yes? See --help."),
    ("-Y", "Unknown option -Y. Did you mean -y or --yes? See --help."),
    ("--assume-yes", "Unknown option --assume-yes. Did you mean -y or --yes? See --help."),
    ("--force", "Unknown option --force. See --help."),
    ("--install-dir", "Unknown option --install-dir. See --help."),
    ("-x", "Unknown option -x. See --help."),
    ("install", "Unknown option install. See --help."),
])
def test_an_unknown_option_stops_before_anything(tmp_path, option, message):
    """Stops at the argument loop, before root is checked or anything is
    read, downloaded or written: run as any user, with a stand-in curl and
    uname that would record a call."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name in ("curl", "uname", "systemctl", "smartctl", "mkdir"):
        (stubs / name).write_text(f"#!/bin/sh\necho {name} >> '{tmp_path}/calls'\n")
        (stubs / name).chmod(0o755)
    result = subprocess.run(["bash", str(INSTALL_SH), "--yes", option], capture_output=True,
                            text=True, check=False, stdin=subprocess.DEVNULL, timeout=20,
                            env={"PATH": f"{stubs}:/usr/bin:/bin", "TERM": "dumb"})
    assert result.returncode == 2
    assert message in re.sub(r"\x1b\[[0-9;]*m", "", result.stderr), result.stderr
    assert result.stdout == "" and not (tmp_path / "calls").exists()


def test_the_bare_word_uninstall_is_still_taken():
    source = INSTALL_SH.read_text()
    assert "--uninstall|-u|uninstall) UNINSTALL_REQUESTED=true ;;" in source
    loop = source[source.index('for arg in "$@"; do'):]
    loop = loop[:loop.index("done")]
    assert loop.index("uninstall)") < loop.index('*) unknown_option "$arg" ;;')


def test_a_tty_that_cannot_be_opened_is_no_tty():
    source = INSTALL_SH.read_text()
    assert "elif ( : </dev/tty ) 2>/dev/null; then" in source
    assert "elif [ -e /dev/tty ]; then" not in source


def test_shellcheck_finds_nothing_new():
    """The three unused-variable notes predate --yes."""
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck not installed")
    result = subprocess.run([shellcheck, "-S", "warning", "-f", "gcc", str(INSTALL_SH)],
                            capture_output=True, text=True, check=False)
    codes = re.findall(r"\[(SC\d+)\]", result.stdout)
    assert set(codes) <= {"SC2034"} and len(codes) <= 3, result.stdout


# --- install.ps1, read --------------------------------------------------------------


def test_install_ps1_has_the_switch_and_the_variable():
    source = INSTALL_PS1.read_text()
    assert re.search(r"\[switch\]\$Yes", source)
    assert "$env:SMARTHA_YES" in source
    assert "$script:AssumeYes" in source


def test_install_ps1_prompts_all_go_through_ask():
    source = INSTALL_PS1.read_text()
    direct = [line.strip() for line in source.splitlines()
              if "Read-Host" in line and not line.strip().startswith("#")]
    assert len(direct) == 1 and "function" not in direct[0], direct


# --- install.sh, run ----------------------------------------------------------------

STUBS = {
    # The download (binary and checksums) and the health check.
    "curl": r"""#!/bin/bash
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift ;; http*) url="$1" ;; esac; shift
done
echo "curl $url" >> "$STUB_LOG"
if [ -n "$out" ]; then
  case "$url" in
    *checksums.txt) sha=$(sha256sum "$STUB_DIR/agent-bin" | awk '{print $1}')
                    echo "$sha  smartha-agent-linux-amd64" > "$out" ;;
    *) cp "$STUB_DIR/agent-bin" "$out" ;;
  esac
  exit 0
fi
case "$url" in *api/health) echo '{"status":"ok"}'; exit 0 ;; esac
exit 22
""",
    "smartctl": r"""#!/bin/bash
case "$*" in
  *--version*) echo "smartctl 7.4 2023-08-01 r5530 [x86_64-linux] (stand-in)" ;;
  *--scan*) echo '{"devices":[{"name":"/dev/sda","type":"sat","protocol":"ATA"}]}' ;;
  *-i*) echo '{"rotation_rate":7200,"model_name":"FAKE HDD"}' ;;
  *) echo '{}' ;;
esac
""",
    "systemctl": """#!/bin/bash
echo "systemctl $*" >> "$STUB_LOG"
[ "$1" = "is-active" ] && exit 3
exit 0
""",
    "lsblk": """#!/bin/bash
echo 'NAME="sda" SIZE="4T" MODEL="FAKE HDD" TRAN="sata" VENDOR="ATA"'
""",
    "sleep": "#!/bin/bash\nexit 0\n",
}


def _can_run() -> str | None:
    if sys.platform != "linux":
        return "Linux only"
    if os.geteuid() != 0:
        return "needs root (a private mount namespace)"
    for tool in ("unshare", "setsid", "bash", "sha256sum", "timeout"):
        if shutil.which(tool) is None:
            return f"{tool} not installed"
    probe = subprocess.run(["unshare", "-m", "true"], capture_output=True, check=False)
    return None if probe.returncode == 0 else "unshare -m not permitted here"


needs_namespace = pytest.mark.skipif(_can_run() is not None, reason=str(_can_run()))


def _run(tmp_path: Path, args: list[str],
         env: dict | None = None) -> tuple[subprocess.CompletedProcess, str]:
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    for name, body in STUBS.items():
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    (stubs / "agent-bin").write_text("#!/bin/sh\nexit 0\n")
    log = tmp_path / "stub.log"
    log.write_text("")
    inner = (
        "mount --make-rprivate / && "
        "mount -t tmpfs none /etc/systemd/system && mount -t tmpfs none /etc/init.d && "
        "mount -t tmpfs none /run && mkdir -p /run/systemd/system && "
        "mount -t tmpfs none /var/log && "
        # A new session has no controlling terminal: /dev/tty exists, and
        # opening it fails (ENXIO), as over ssh without a pty.
        "exec setsid -w bash \"$INSTALL_SH\" \"$@\" < /dev/null"
    )
    run_env = {
        "PATH": f"{stubs}:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(tmp_path), "VERSION": "1.0.0", "STUB_DIR": str(stubs),
        "STUB_LOG": str(log), "INSTALL_SH": str(INSTALL_SH), "TERM": "dumb",
        **(env or {}),
    }
    result = subprocess.run(["timeout", "120", "unshare", "-m", "bash", "-c", inner, "install", *args],
                            capture_output=True, text=True, env=run_env, check=False,
                            stdin=subprocess.DEVNULL, timeout=150, encoding="utf-8")
    return result, log.read_text()


@needs_namespace
@pytest.mark.parametrize("how", [["--yes"], ["-y"], "env"])
def test_a_fresh_install_with_yes_takes_every_default(tmp_path, how):
    folder = tmp_path / "agent"
    args = [f"--install-dir={folder}"] + (how if isinstance(how, list) else [])
    env = {"SMARTHA_YES": "1"} if how == "env" else None
    result, log = _run(tmp_path, args, env)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out[-3000:]
    config = (folder / "config.yaml").read_text()
    assert "port: 9099" in config and "scan_interval: 60s" in config
    assert "token:" not in config and "advertise_interface" not in config
    # Enter at the standby question is yes (the stand-in drive spins).
    assert "standby" in config
    # The questions were shown, each answered by its default; none was read.
    for prompt in ("Port [9099]:", "Scan interval", "Enable standby-aware polling? [Y/n]:"):
        assert re.search(re.escape(prompt) + r".*\(default\)", out), prompt
    assert "Non-interactive mode detected" not in out
    assert (folder / "smartha-agent").exists()
    assert "systemctl start smartha-agent" in log and "Health check passed" in out


@needs_namespace
def test_an_update_with_yes_keeps_the_config(tmp_path):
    folder = tmp_path / "agent"
    folder.mkdir()
    existing = (f"port: 9198\ntoken: \"{TOKEN}\"\nscan_interval: 5m\n"
                "advertise_interface: eth0\nexclude_devices:\n  - /dev/sdz\n"
                "filesystems:\n  - path: /srv\n")
    (folder / "config.yaml").write_text(existing)
    result, log = _run(tmp_path, ["--yes", f"--install-dir={folder}"])
    out = result.stdout + result.stderr
    assert result.returncode == 0, out[-3000:]
    assert re.search(r"Keep current settings\? \[Y/n\]: \(default\)", out)
    assert "Keeping existing configuration." in out
    assert (folder / "config.yaml").read_text() == existing
    assert "Writing config" not in out and "systemctl start smartha-agent" in log
    # Shown as fixed dots, valid UTF-8 (the run decodes strictly), nothing
    # of the token, not even its length.
    assert "Token:      \u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022 (set)" in out
    for part in (TOKEN, TOKEN[:2], TOKEN[-2:]):
        assert f"Token:      {part}" not in out and f"{part} (set)" not in out


@needs_namespace
def test_an_update_with_yes_takes_the_follow_up_defaults(tmp_path):
    """An older config without exclude_devices: the drive picker runs and
    takes its default (every local drive), so nothing is excluded."""
    folder = tmp_path / "agent"
    folder.mkdir()
    (folder / "config.yaml").write_text("port: 9099\nscan_interval: 60s\nadvertise_interface: eth0\n"
                                        "filesystems:\n  - path: /srv\n")
    result, _ = _run(tmp_path, ["--yes", f"--install-dir={folder}"])
    out = result.stdout + result.stderr
    assert result.returncode == 0, out[-3000:]
    assert re.search(r"Monitor \(1 / all / none\) \[1\]: \(default\)", out), out[-3000:]
    assert "exclude_devices" not in (folder / "config.yaml").read_text()


@needs_namespace
def test_without_yes_a_tty_that_cannot_be_opened_keeps_the_config(tmp_path):
    """The bug this fixes: [ -e /dev/tty ] was true here, and the first read
    from it failed under set -e."""
    folder = tmp_path / "agent"
    folder.mkdir()
    existing = f"port: 9099\ntoken: \"{TOKEN}\"\nscan_interval: 60s\n"
    (folder / "config.yaml").write_text(existing)
    result, _ = _run(tmp_path, [f"--install-dir={folder}"])
    out = result.stdout + result.stderr
    assert result.returncode == 0, out[-3000:]
    assert "Non-interactive mode — keeping existing configuration." in out
    assert (folder / "config.yaml").read_text() == existing


@needs_namespace
def test_without_yes_and_no_terminal_a_fresh_install_uses_defaults(tmp_path):
    """No controlling terminal at all (setsid): /dev/tty exists, opening it
    fails. Unchanged behaviour: the non-interactive defaults, no prompt."""
    folder = tmp_path / "agent"
    result, _ = _run(tmp_path, [f"--install-dir={folder}"])
    out = result.stdout + result.stderr
    assert result.returncode == 0, out[-3000:]
    assert "Non-interactive mode detected -- using defaults." in out
    assert "port: 9099" in (folder / "config.yaml").read_text()


# --- install.ps1, under PowerShell (pwsh) when it is installed ------------------------
#
# The script installs a Windows service as soon as it runs, so only its
# parse and its prompt helpers are run here: the helpers are taken out of
# the parsed script (AST) and called with -Yes, $env:SMARTHA_YES and
# neither. The Windows laptop run is the real test.

PWSH = os.environ.get("PWSH") or shutil.which("pwsh")
needs_pwsh = pytest.mark.skipif(PWSH is None, reason="pwsh not installed (set PWSH)")

HELPERS = r"""
$errs = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$null, [ref]$errs)
if ($errs.Count) { $errs | ForEach-Object { "PARSE: $($_.Message) line $($_.Extent.StartLineNumber)" }; exit 3 }
$want = 'Test-ConsoleReadable', 'Test-Interactive', 'Read-Answer'
foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    if ($want -contains $f.Name) { . ([scriptblock]::Create($f.Extent.Text)) }
}
$assign = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    $n.Left.Extent.Text -eq '$script:AssumeYes' }, $true) | Select-Object -First 1
$Yes = [switch]($args[1] -eq 'switch')
. ([scriptblock]::Create($assign.Extent.Text))
"assume=$($script:AssumeYes)"
"interactive=$(Test-Interactive)"
"answer=[$(Read-Answer '  Port [9099]')]"
"""


def _ps_file(tmp_path: Path, name: str, body: str, *args: str,
             env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a helper script with -File, so install.ps1's path is only ever an
    argument to it: install.ps1 itself is never run here."""
    script = tmp_path / name
    script.write_text(body)
    return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(script), *args],
                          capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
                          timeout=120, check=False)


def _pwsh(tmp_path: Path, mode: str, env_yes: str | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "SMARTHA_YES"}
    if env_yes is not None:
        env["SMARTHA_YES"] = env_yes
    result = _ps_file(tmp_path, "helpers.ps1", HELPERS, str(INSTALL_PS1), mode, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@needs_pwsh
def test_install_ps1_parses(tmp_path):
    body = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
            "$args[0], [ref]$null, [ref]$e); $e.Count")
    result = _ps_file(tmp_path, "parse.ps1", body, str(INSTALL_PS1))
    assert result.stdout.strip() == "0", result.stdout + result.stderr


@needs_pwsh
@pytest.mark.parametrize(("mode", "env_yes"), [("switch", None), ("none", "1"), ("none", "true")])
def test_install_ps1_yes_answers_every_prompt_with_its_default(tmp_path, mode, env_yes):
    out = _pwsh(tmp_path, mode, env_yes)
    assert "assume=True" in out and "interactive=True" in out
    assert "Port [9099]: (default)" in out and "answer=[]" in out


@needs_pwsh
@pytest.mark.parametrize("env_yes", [None, "0", ""])
def test_install_ps1_without_yes_and_a_piped_input_is_not_interactive(tmp_path, env_yes):
    """stdin is not a console here (as over Windows SSH without a pty)."""
    out = _pwsh(tmp_path, "none", env_yes)
    assert "assume=False" in out and "interactive=False" in out


def test_install_ps1_irm_iex_takes_the_param_block():
    """A piped script (irm | iex) can carry a param block; the switch is
    then simply not set, which is why SMARTHA_YES exists."""
    source = INSTALL_PS1.read_text()
    head = source.split("$ErrorActionPreference", 1)[0]
    assert head.rstrip().endswith("param([switch]$Yes)")
    assert "$env:SMARTHA_YES=1; irm https://raw.githubusercontent.com/DAB-LABS/smart-sniffer/main/install.ps1 | iex" in source
