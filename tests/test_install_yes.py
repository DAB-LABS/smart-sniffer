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
import pty
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
out=""; url=""; config=""
echo "curl-args $*" >> "$STUB_LOG"
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift ;; -K) config="$2"; shift ;; http*) url="$1" ;; esac; shift
done
[ "$config" = "-" ] && cat >> "$STUB_DIR/curl-config"
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


def _run(tmp_path: Path, args: list[str], env: dict | None = None, answers: str | None = None,
         stubs_extra: dict | None = None) -> tuple[subprocess.CompletedProcess, str]:
    """answers: run on a terminal (a pty as stdin) with these lines typed
    ahead, instead of with no terminal at all."""
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    for name, body in {**STUBS, **(stubs_extra or {})}.items():
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
        + ("exec bash \"$INSTALL_SH\" \"$@\"" if answers is not None
           else "exec setsid -w bash \"$INSTALL_SH\" \"$@\" < /dev/null")
    )
    run_env = {
        "PATH": f"{stubs}:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(tmp_path), "VERSION": "1.0.0", "STUB_DIR": str(stubs),
        "STUB_LOG": str(log), "INSTALL_SH": str(INSTALL_SH), "TERM": "dumb",
        **(env or {}),
    }
    master = slave = None
    stdin = subprocess.DEVNULL
    if answers is not None:
        master, slave = pty.openpty()
        os.write(master, answers.encode())
        stdin = slave
    try:
        result = subprocess.run(["timeout", "120", "unshare", "-m", "bash", "-c", inner, "install", *args],
                                capture_output=True, text=True, env=run_env, check=False,
                                stdin=stdin, timeout=150, encoding="utf-8")
    finally:
        for fd in (master, slave):
            if fd is not None:
                os.close(fd)
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
def test_an_update_with_yes_keeps_the_config_byte_for_byte(tmp_path):
    """v1.0 (owner, 2026-11-03): an older config without exclude_devices,
    filesystems or advertise_interface. With --yes no follow-up picker runs
    and the file is kept exactly; the update says so."""
    folder = tmp_path / "agent"
    folder.mkdir()
    existing = "port: 9099\ntoken: \"x\"\nscan_interval: 60s\n"
    (folder / "config.yaml").write_text(existing)
    result, _ = _run(tmp_path, ["--yes", f"--install-dir={folder}"])
    out = result.stdout + result.stderr
    assert result.returncode == 0, out[-3000:]
    assert (folder / "config.yaml").read_text() == existing
    assert "--yes: config.yaml is kept exactly as it is." in out
    for picker in ("Monitor (", "Advertise on (", "Drive Scanner", "Disk usage monitoring is now available"):
        assert picker not in out, picker
    assert not list(folder.glob("config.yaml.bak*"))


# The Kali VM case (2026-11-03): its only disk is a QEMU virtual disk whose
# transport lsblk reports as empty (shown "unknown", a yellow drive).
QEMU_ONLY = {
    "lsblk": """#!/bin/bash
echo 'NAME="sda" SIZE="32G" MODEL="QEMU HARDDISK" TRAN="" VENDOR="QEMU"'
""",
    "smartctl": STUBS["smartctl"].replace('"rotation_rate":7200,"model_name":"FAKE HDD"',
                                          '"rotation_rate":0,"model_name":"QEMU HARDDISK"'),
}


@needs_namespace
def test_the_kali_case_update_with_yes_leaves_the_file_identical(tmp_path):
    folder = tmp_path / "agent"
    folder.mkdir()
    existing = ('port: 9099\nscan_interval: 60s\ntoken: "abcd"\nadvertise_interface: eth0\n\n'
                'filesystems:\n  - path: "/"\n    uuid: ""\n    device: "/dev/sda1"\n    fstype: "ext4"\n')
    (folder / "config.yaml").write_text(existing)
    result, _ = _run(tmp_path, ["--yes", f"--install-dir={folder}"], stubs_extra=QEMU_ONLY)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out[-3000:]
    assert (folder / "config.yaml").read_bytes() == existing.encode()
    assert "Drive Scanner" not in out and "No drives selected" not in out


@needs_namespace
def test_a_fresh_install_with_only_yellow_drives_monitors_them_all(tmp_path):
    """The drive picker never defaults to no drives: all yellow means all."""
    folder = tmp_path / "agent"
    result, _ = _run(tmp_path, ["--yes", f"--install-dir={folder}"], stubs_extra=QEMU_ONLY)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out[-3000:]
    assert re.search(r"Monitor \(1 / all / none\) \[all\]: \(default\)", out), out[-3000:]
    assert "all are selected by" in out
    assert "exclude_devices" not in (folder / "config.yaml").read_text()


@needs_namespace
@pytest.mark.parametrize(("choice", "confirm", "excluded"), [
    ("", "", False),        # Enter at the picker: all
    ("none", "", False),    # none, then Enter at the question: No, nothing excluded
    ("none", "n", False),
    ("none", "y", True),    # only a typed yes excludes every drive
])
def test_the_drive_picker_never_excludes_everything_by_default(tmp_path, choice, confirm, excluded):
    """Interactive (a pty): N at Keep, then port, token, interval, then the
    drive picker."""
    folder = tmp_path / "agent"
    folder.mkdir()
    (folder / "config.yaml").write_text("port: 9099\n")
    typed = "n\n\n\n\n" + choice + "\n" + (confirm + "\n" if choice == "none" else "")
    result, _ = _run(tmp_path, [f"--install-dir={folder}"], answers=typed + "\n" * 30,
                     stubs_extra=QEMU_ONLY)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out[-3000:]
    config = (folder / "config.yaml").read_text()
    assert ("exclude_devices" in config) is excluded, config
    if choice == "none":
        assert "Exclude every drive? [y/N]:" in out


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


# --- Answering N at "Keep current settings?" (v1.0, B1) ------------------------------
#
# The file is saved as config.yaml.bak-<date>-<time> before it is rewritten,
# and every top-level key the prompts do not ask about is carried over:
# device_overrides above all (docs/discover.md used to warn it was lost).

KEEP_N_EXISTING = (
    f"port: 9198\ntoken: \"{TOKEN}\"\nscan_interval: 5m\nstandby_mode: idle\n"
    "# overrides from --discover\n"
    "device_overrides:\n  - device: /dev/sata1\n    protocol: sat\n"
    "# a note between items\n"
    "  - device: /dev/sda\n    protocol: megaraid,0\n"
    "agent_id: pinned-box-0042\nmdns: false\n"
    "smartctl_path: /opt/bin/smartctl\n"
    "exclude_devices:\n  - /dev/sdz\n"
    "filesystems:\n  - path: /srv\n"
)


def _keys(config: str) -> list[str]:
    return re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:", config, re.M)


def _carry(text: str, managed: str) -> str:
    """carry_config_blocks from install.sh, run on its own."""
    source = INSTALL_SH.read_text()
    func = re.search(r"^carry_config_blocks\(\) \{\n.*?^\}\n", source, re.M | re.S).group(0)
    result = subprocess.run(["bash", "-c", func + 'carry_config_blocks /dev/stdin "$1"', "x", managed],
                            input=text, capture_output=True, text=True, check=True, timeout=20)
    return result.stdout


def test_carry_config_blocks_keeps_only_the_other_keys():
    out = _carry(KEEP_N_EXISTING, "port scan_interval token advertise_interface exclude_devices "
                                  "filesystems standby_mode")
    assert out == ("device_overrides:\n  - device: /dev/sata1\n    protocol: sat\n"
                   "  - device: /dev/sda\n    protocol: megaraid,0\n"
                   "agent_id: pinned-box-0042\nmdns: false\nsmartctl_path: /opt/bin/smartctl\n"), out
    assert TOKEN not in out


@pytest.mark.parametrize(("text", "want"), [
    # The compact sequence style, at column 0.
    ("port: 1\ndevice_overrides:\n- device: /dev/sdc\n  protocol: sat\n",
     "device_overrides:\n- device: /dev/sdc\n  protocol: sat\n"),
    # Flow style, and a key nobody knows.
    ("device_overrides: [{device: /dev/sdc, protocol: sat}]\nmy_note: hi\ntoken: x\n",
     "device_overrides: [{device: /dev/sdc, protocol: sat}]\nmy_note: hi\n"),
    # Nothing else in the file: nothing carried.
    ("port: 1\ntoken: \"x\"\n", ""),
    # A key that only starts like a managed one is not that key.
    ("port_note: x\n", "port_note: x\n"),
])
def test_carry_config_blocks_shapes(text, want):
    assert _carry(text, "port scan_interval token") == want


@needs_namespace
def test_answering_n_keeps_device_overrides_and_saves_a_backup(tmp_path):
    folder = tmp_path / "agent"
    folder.mkdir()
    (folder / "config.yaml").write_text(KEEP_N_EXISTING)
    # N, then Enter at every question that follows.
    result, log = _run(tmp_path, [f"--install-dir={folder}"], answers="n\n" + "\n" * 30)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out[-3000:]
    assert "Keep current settings? [Y/n]:" in out and "Keeping existing configuration." not in out
    config = (folder / "config.yaml").read_text()

    # The answers: the defaults, standby asked (the stand-in drive spins).
    assert config.startswith("port: 9099\nscan_interval: 60s\n"), config
    assert "standby_mode: standby" in config and "standby_mode: idle" not in config
    assert TOKEN not in config and "/srv" not in config and "/dev/sdz" not in config
    # Carried over, once each.
    assert ("device_overrides:\n  - device: /dev/sata1\n    protocol: sat\n"
            "  - device: /dev/sda\n    protocol: megaraid,0\n") in config
    assert "agent_id: pinned-box-0042" in config and "mdns: false" in config
    assert "smartctl_path: /opt/bin/smartctl" in config
    keys = _keys(config)
    assert len(keys) == len(set(keys)), keys
    assert "Kept from the previous config: device_overrides, agent_id, mdns, smartctl_path" in out

    # The backup: the old file byte for byte, root only.
    backups = sorted(folder.glob("config.yaml.bak-*"))
    assert len(backups) == 1, backups
    assert re.fullmatch(r"config\.yaml\.bak-\d{8}-\d{6}", backups[0].name)
    assert backups[0].read_text() == KEEP_N_EXISTING
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert f"Previous config saved as {backups[0]}" in out
    assert "systemctl start smartha-agent" in log


@needs_namespace
def test_answering_n_keeps_standby_mode_when_it_is_not_asked(tmp_path):
    """No spinning drive: the standby question is not asked, so the old
    standby_mode is a key the prompts did not ask about."""
    folder = tmp_path / "agent"
    folder.mkdir()
    (folder / "config.yaml").write_text("port: 9099\nstandby_mode: idle\ndevice_overrides:\n"
                                        "  - device: /dev/sata1\n    protocol: sat\n")
    ssd = {"smartctl": STUBS["smartctl"].replace('"rotation_rate":7200', '"rotation_rate":0')}
    result, _ = _run(tmp_path, [f"--install-dir={folder}"], answers="n\n" + "\n" * 30, stubs_extra=ssd)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out[-3000:]
    assert "Enable standby-aware polling" not in out
    config = (folder / "config.yaml").read_text()
    assert config.count("standby_mode:") == 1 and "standby_mode: idle" in config
    assert "device_overrides:" in config


@needs_namespace
def test_a_fresh_install_writes_no_backup(tmp_path):
    folder = tmp_path / "agent"
    result, _ = _run(tmp_path, ["--yes", f"--install-dir={folder}"])
    assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]
    assert set(_keys((folder / "config.yaml").read_text())) <= _agent_keys()
    assert not list(folder.glob("config.yaml.bak*"))
    assert "Kept from the previous config" not in result.stdout


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


# --- install.ps1 refuses an unknown parameter ([CmdletBinding()], v1.0) --------------
#
# Binding is checked on a copy of the script's header only (help, attribute,
# param block) followed by a marker line, so install.ps1's body never runs
# with a parameter that binds. The real file is run only with a parameter
# that must be refused at binding, and with the irm | iex form, where a
# stand-in Invoke-RestMethod stops it at its first network call.


def _header_probe(tmp_path: Path) -> Path:
    source = INSTALL_PS1.read_text()
    head = source.split('$ErrorActionPreference = "Stop"', 1)[0]
    assert "[CmdletBinding()]\nparam([switch]$Yes)" in head
    probe = tmp_path / "header-probe.ps1"
    probe.write_text(head.replace("#Requires -RunAsAdministrator", "") + '"BODY-RAN yes=$Yes"\n')
    return probe


@needs_pwsh
@pytest.mark.parametrize(("arg", "yes"), [("-Yes", "True"), ("-Ye", "True"), ("-y", "True"),
                                          (None, "False")])
def test_install_ps1_yes_binds(tmp_path, arg, yes):
    probe = _header_probe(tmp_path)
    result = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(probe),
                             *([arg] if arg else [])], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, timeout=120, check=False)
    assert f"BODY-RAN yes={yes}" in result.stdout, result.stdout + result.stderr


@needs_pwsh
@pytest.mark.parametrize("arg", ["-Yess", "-Foo", "-Force"])
def test_install_ps1_refuses_an_unknown_parameter_before_anything_runs(tmp_path, arg):
    for script in (_header_probe(tmp_path), INSTALL_PS1):
        env = {k: v for k, v in os.environ.items() if k not in ("SMARTHA_YES", "VERSION", "TEMP")}
        result = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(script), arg],
                                capture_output=True, text=True, env=env,
                                stdin=subprocess.DEVNULL, timeout=120, check=False)
        out = result.stdout + result.stderr
        assert result.returncode != 0
        assert f"A parameter cannot be found that matches parameter name '{arg[1:]}'" in out, out
        assert "BODY-RAN" not in out and "Windows Installer" not in out
        assert "Detected platform" not in out


@needs_pwsh
def test_install_ps1_irm_iex_form_still_runs_its_normal_flow(tmp_path):
    body = (
        "function Invoke-RestMethod { Write-Host 'STOP-AT-NETWORK'; throw 'no network in tests' }\n"
        "function Invoke-WebRequest { Write-Host 'STOP-AT-NETWORK'; throw 'no network in tests' }\n"
        "$env:SMARTHA_YES = '1'\n"
        "Remove-Item Env:VERSION -ErrorAction SilentlyContinue\n"
        "Get-Content -Raw $args[0] | Invoke-Expression\n"
    )
    result = _ps_file(tmp_path, "iex.ps1", body, str(INSTALL_PS1),
                      env={k: v for k, v in os.environ.items() if k != "VERSION"})
    out = result.stdout + result.stderr
    assert "SMART Sniffer Agent - Windows Installer" in out, out
    assert "Detected platform: windows/" in out and "Fetching latest release version" in out
    assert "STOP-AT-NETWORK" in out and "Could not determine latest version" in out
    assert "parameter" not in out.lower() or "matches parameter name" not in out


# --- install.ps1: N at "Keep existing configuration?" (v1.0, B1) ----------------------

CARRY_PS1 = r"""
$errs = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$null, [ref]$errs)
foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    if (@('Get-CarriedConfigBlocks', 'Get-ConfigBlockKeys') -contains $f.Name) { . ([scriptblock]::Create($f.Extent.Text)) }
}
$lines = Get-CarriedConfigBlocks -Path $args[1] -Managed ($args[2] -split ' ')
"COUNT=$($lines.Count)"
$lines | ForEach-Object { "LINE=$_" }
"KEYS=$(Get-ConfigBlockKeys $lines)"
"""


@needs_pwsh
def test_install_ps1_carries_the_keys_the_prompts_do_not_ask(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(KEEP_N_EXISTING)
    result = _ps_file(tmp_path, "carry.ps1", CARRY_PS1, str(INSTALL_PS1), str(config),
                      "port scan_interval token advertise_interface filesystems standby_mode")
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line[5:] for line in result.stdout.splitlines() if line.startswith("LINE=")]
    # install.ps1 has no drive picker, so exclude_devices is carried too.
    assert lines == ["device_overrides:", "  - device: /dev/sata1", "    protocol: sat",
                     "  - device: /dev/sda", "    protocol: megaraid,0",
                     "agent_id: pinned-box-0042", "mdns: false", "smartctl_path: /opt/bin/smartctl",
                     "exclude_devices:", "  - /dev/sdz"], result.stdout
    assert "KEYS=device_overrides, agent_id, mdns, smartctl_path, exclude_devices" in result.stdout
    assert TOKEN not in result.stdout


@needs_pwsh
def test_install_ps1_carries_nothing_from_a_file_of_managed_keys(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(f"port: 9099\ntoken: \"{TOKEN}\"\n")
    result = _ps_file(tmp_path, "carry.ps1", CARRY_PS1, str(INSTALL_PS1), str(config), "port token")
    assert "COUNT=0" in result.stdout, result.stdout + result.stderr


def test_install_ps1_saves_the_old_file_before_it_rewrites_it():
    source = INSTALL_PS1.read_text()
    write = source[source.index("# An existing file is saved before it is rewritten"):]
    write = write[:write.index('Write-Ok "Config written."')]
    assert write.index("Copy-Item -LiteralPath $ConfigFile -Destination $ConfigBak") < write.index("Set-Content -Path $ConfigFile")
    assert "$ConfigFile.bak-\" + (Get-Date -Format 'yyyyMMdd-HHmmss')" in write
    assert "Protect-ConfigFile $ConfigBak" in write
    assert "if ($StandbyAsked) { $Managed += 'standby_mode' }" in write


# --- The token, written and read back safely (v1.0, J13) -----------------------------

TRICKY = "a b'c\"d\\e $HOME #f:g"


def test_no_eval_and_no_token_on_a_command_line():
    source = INSTALL_SH.read_text()
    code = [line for line in source.splitlines() if not line.strip().startswith("#")]
    assert not any(re.search(r"\beval\b", line) for line in code)
    assert not any("Bearer $TOKEN" in line for line in code)
    assert 'echo "token: $(yaml_single_quote "$TOKEN")"' in source


@needs_namespace
def test_a_typed_token_is_written_quoted_and_sent_on_stdin(tmp_path):
    folder = tmp_path / "agent"
    folder.mkdir()
    (folder / "config.yaml").write_text("port: 9099\n")
    # N, Enter at the port, the token, then Enter at everything else.
    result, log = _run(tmp_path, [f"--install-dir={folder}"],
                       answers="n\n\n" + TRICKY + "\n" + "\n" * 30)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out[-3000:]
    config = (folder / "config.yaml").read_text()
    quoted = "'" + TRICKY.replace("'", "''") + "'"
    assert f"token: {quoted}\n" in config, config
    # Never an argument of curl (the process list); the header went on stdin.
    for line in log.splitlines():
        if line.startswith("curl-args"):
            assert TRICKY not in line and "Bearer" not in line, line
    sent = (tmp_path / "stubs" / "curl-config").read_text()
    escaped = TRICKY.replace("\\", "\\\\").replace('"', '\\"')
    assert f'header = "Authorization: Bearer {escaped}"' in sent, sent
    assert "Health check passed" in out
    # Every key install.sh wrote is one the agent reads (J7: no warning).
    assert set(_keys(config)) <= _agent_keys(), set(_keys(config)) - _agent_keys()


def _agent_keys() -> set[str]:
    """The top-level keys the agent reads: Config's yaml tags (config.go)."""
    source = (ROOT / "agent" / "config.go").read_text()
    return set(re.findall(r'yaml:"([a-z_]+)"', source)) | {"scan_interval"}


@needs_namespace
def test_a_token_with_a_control_character_is_asked_again(tmp_path):
    folder = tmp_path / "agent"
    folder.mkdir()
    (folder / "config.yaml").write_text("port: 9099\n")
    result, _ = _run(tmp_path, [f"--install-dir={folder}"],
                     answers="n\n\nbad\ttoken\ngood-token\n" + "\n" * 30)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out[-3000:]
    assert "cannot hold control characters" in out
    assert "token: 'good-token'" in (folder / "config.yaml").read_text()


@needs_namespace
def test_keeping_the_config_reads_a_quoted_token_back(tmp_path):
    """Kept config, token single-quoted with a doubled quote: the health check
    sends the token as written, not with its quotes."""
    folder = tmp_path / "agent"
    folder.mkdir()
    (folder / "config.yaml").write_text("port: 9099\ntoken: 'it''s # here'\n")
    result, _ = _run(tmp_path, ["--yes", f"--install-dir={folder}"])
    assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]
    sent = (tmp_path / "stubs" / "curl-config").read_text()
    assert 'header = "Authorization: Bearer it\'s # here"' in sent, sent


def test_install_ps1_update_with_yes_changes_nothing_in_the_config():
    """v1.0: every upgrade step that writes into a kept config is gated on
    $UpgradeSteps, which is false with -Yes / SMARTHA_YES."""
    source = INSTALL_PS1.read_text()
    keep = source[source.index('Write-Ok "Keeping existing configuration."'):]
    keep = keep[:keep.index("} elseif (-not (Test-Interactive)) {")]
    assert "$UpgradeSteps = -not $script:AssumeYes" in keep
    writers = [line.strip() for line in keep.splitlines()
               if re.search(r"Set-Content|Add-Content", line)]
    assert writers, keep
    # Each writer sits inside an if that tests $UpgradeSteps.
    blocks = re.findall(r"if \((.*?)\) \{(.*?)\n        \}", keep, re.S)
    gated = "".join(body for cond, body in blocks if "$UpgradeSteps" in cond)
    for line in writers:
        assert line in gated, line
