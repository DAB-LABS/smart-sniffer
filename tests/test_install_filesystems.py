"""Checks on the disk usage picker in install.sh.

The picker reads /proc/self/mountinfo, asks df for sizes and prompts on the
terminal, so it cannot run against the test host's real mounts. These tests
pull the picker's functions out of install.sh, run them in bash with `cat` and
`df` replaced by shell functions that serve a fixture, answer "all" at every
prompt, and read back the YAML the picker would write.

Device names, UUIDs and sizes below are invented.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_INSTALL_SH = Path(__file__).resolve().parents[1] / "install.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


def _function(source: str, name: str) -> str:
    """Return the text of a top-level bash function, from `name() {` to `}`."""
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", source, re.MULTILINE | re.DOTALL)
    assert match, f"{name} not found in install.sh"
    return match.group(0)


def _harness(mountinfo: str, tmp_path: Path) -> str:
    source = _INSTALL_SH.read_text()
    fixture = tmp_path / "mountinfo"
    fixture.write_text(mountinfo)
    answers = tmp_path / "tty"
    answers.write_text("all\n")
    functions = "\n".join(
        _function(source, name)
        for name in (
            "unescape_mountinfo_path",
            "btrfs_usage_for",
            "is_synology_volume_mp",
            "fs_source_admitted",
            "pick_filesystems",
        )
    )
    return f"""
set -e
OSTYPE=linux-gnu
RED=''; YELLOW=''; GREEN=''; BOLD=''; NC=''
info()    {{ echo "  --> $*"; }}
success() {{ echo "  ok $*"; }}
warn()    {{ echo "  warn $*"; }}
TTY_IN='{answers}'
cat() {{
  if [ "$1" = /proc/self/mountinfo ]; then command cat '{fixture}'; else command cat "$@"; fi
}}
df() {{
  local mp="${{@: -1}}"
  echo "Filesystem 1B-blocks Used Available Use% Mounted on"
  echo "/dev/fake 4000000000000 1000000000000 3000000000000 25% $mp"
}}
blkid() {{ return 1; }}
{functions}
pick_filesystems
echo "---YAML---"
printf '%s\\n' "$FS_YAML"
"""


def _picked(mountinfo: str, tmp_path: Path) -> list[tuple[str, str]]:
    """Run the picker over a mountinfo fixture; return (path, device) pairs."""
    script = _harness(mountinfo, tmp_path)
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr + result.stdout
    yaml = result.stdout.split("---YAML---", 1)[1]
    paths = re.findall(r'- path: "([^"]*)"', yaml)
    devices = re.findall(r'device: "([^"]*)"', yaml)
    return list(zip(paths, devices))


def _bash_true(expr: str) -> bool:
    source = _INSTALL_SH.read_text()
    script = _function(source, "is_synology_volume_mp") + _function(
        source, "fs_source_admitted"
    ) + expr
    return subprocess.run(["bash", "-c", script], check=False).returncode == 0


# DSM with SHR: the data volume sits on LVM, so its device is /dev/vg1000/lv.
_SYNOLOGY_SHR = """\
21 1 9:0 / / rw,noatime shared:1 - ext4 /dev/md0 rw,data=ordered
22 21 0:5 / /dev rw,nosuid shared:2 - devtmpfs devtmpfs rw
25 21 0:20 / /proc rw,nosuid shared:5 - proc proc rw
30 21 0:25 / /tmp rw,nosuid shared:9 - tmpfs tmpfs rw
40 21 253:0 / /volume1 rw,relatime shared:20 - btrfs /dev/vg1000/lv rw,space_cache=v2
41 40 253:0 /@docker /volume1/@docker rw,relatime shared:21 - btrfs /dev/vg1000/lv rw
45 21 8:113 / /volumeUSB1/usbshare rw,relatime shared:30 - vfat /dev/usb1p1 rw
"""

# DSM 7 with an SSD cache layer: /dev/mapper/cachedev_N. /volume2 is mounted
# from a btrfs subvolume and shares its device with a root="/" mount at a
# longer path, which would otherwise hide /volume2 as a bind mount.
_SYNOLOGY_CACHEDEV = """\
21 1 9:0 / / rw,noatime shared:1 - ext4 /dev/md0 rw
40 21 253:1 / /volume1 rw,relatime shared:20 - btrfs /dev/mapper/cachedev_0 rw
41 40 253:1 / /volume1/@appstore rw,relatime shared:21 - btrfs /dev/mapper/cachedev_0 rw
50 21 253:2 /@syno /volume2 rw,relatime shared:22 - btrfs /dev/mapper/cachedev_1 rw
51 21 253:2 / /volume2/@tmp/snapshot rw,relatime shared:23 - btrfs /dev/mapper/cachedev_1 rw
"""

# A plain Linux host. Every entry the picker offered before GH #62 must still
# be offered, with nothing added.
_GENERIC = """\
21 1 8:2 / / rw,relatime shared:1 - ext4 /dev/sda2 rw
22 21 8:1 / /boot/efi rw,relatime shared:2 - vfat /dev/sda1 rw
23 21 259:1 / /srv rw,relatime shared:3 - xfs /dev/nvme0n1p1 rw
24 21 259:1 /data /export/data rw,relatime shared:4 - xfs /dev/nvme0n1p1 rw
25 21 0:40 / /tank rw,relatime shared:5 - zfs tank rw
26 21 0:41 / /mnt/nas rw,relatime shared:6 - cifs //nas/share rw
27 21 0:20 / /run rw,nosuid shared:7 - tmpfs tmpfs rw
28 21 7:3 / /snap/core/1 ro,nodev shared:8 - squashfs /dev/loop3 ro
"""


def test_synology_shr_volume_is_offered(tmp_path: Path) -> None:
    picked = _picked(_SYNOLOGY_SHR, tmp_path)
    assert ("/", "/dev/md0") in picked
    assert ("/volume1", "/dev/vg1000/lv") in picked
    assert ("/volumeUSB1/usbshare", "/dev/usb1p1") in picked
    # The @docker subvolume is not a Synology volume and its device does not
    # pass the filter, so it stays out, as before.
    assert all(path != "/volume1/@docker" for path, _ in picked)
    assert len(picked) == len(set(picked))


def test_synology_volume_never_hidden_as_bind_mount(tmp_path: Path) -> None:
    picked = [path for path, _ in _picked(_SYNOLOGY_CACHEDEV, tmp_path)]
    assert "/volume1" in picked
    assert "/volume2" in picked
    # Bind-mount hiding still applies to everything else.
    assert "/volume1/@appstore" not in picked
    assert picked.count("/volume1") == 1


def test_generic_linux_unchanged(tmp_path: Path) -> None:
    picked = [path for path, _ in _picked(_GENERIC, tmp_path)]
    assert picked == ["/", "/boot/efi", "/srv", "/tank"]


@pytest.mark.parametrize(
    ("mp", "expected"),
    [
        ("/volume1", True),
        ("/volume12", True),
        ("/volumeUSB1/usbshare", True),
        ("/volumeUSB2/usbshare1-1", True),
        ("/volume", False),
        ("/volume1/docker", False),
        ("/volumes/1", False),
        ("/mnt/volume1", False),
        ("/volumeUSB1", False),
        ("/volumeUSB1/usbshare/x", False),
    ],
)
def test_synology_volume_pattern(mp: str, expected: bool) -> None:
    assert _bash_true(f'is_synology_volume_mp "{mp}"') is expected


def test_device_filter_admits_what_it_did_before() -> None:
    for dev in ("/dev/sda1", "/dev/nvme0n1p2", "/dev/md2", "/dev/mapper/vg-root",
                "/dev/vda1", "/dev/xvda1", "/dev/hda1", "/dev/disk3s1"):
        assert _bash_true(f'fs_source_admitted "{dev}" ext4 /srv'), dev
    assert _bash_true("fs_source_admitted tank zfs /tank")
    assert not _bash_true("fs_source_admitted //nas/share cifs /mnt/nas")
    assert not _bash_true("fs_source_admitted /dev/vg1000/lv btrfs /srv")
    assert _bash_true("fs_source_admitted /dev/vg1000/lv btrfs /volume1")
