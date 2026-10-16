"""The version agent/Makefile embeds in a self-built agent.

git describe gives "v0.8.2..."; the agent's banner adds its own "v", so the
Makefile drops the tag's. A VERSION given on the command line (as CI does)
is used as it is. A fake git on PATH stands in for the real one.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parent.parent / "agent"

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")


def _embedded(tmp_path: Path, git_script: str, *args: str) -> str:
    fake = tmp_path / "bin"
    fake.mkdir(parents=True)
    git = fake / "git"
    git.write_text("#!/bin/sh\n" + git_script + "\n")
    git.chmod(0o755)
    env = dict(os.environ, PATH=f"{fake}{os.pathsep}{os.environ['PATH']}")
    env.pop("VERSION", None)
    out = subprocess.run(
        ["make", "-n", "-C", str(AGENT), "build", *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    m = re.search(r"-X main\.version=(\S*?)\"", out)
    assert m, out
    return m.group(1)


@pytest.mark.parametrize(
    ("described", "embedded"),
    [
        ("v0.8.2", "0.8.2"),
        ("v0.8.2-31-g4439849-dirty", "0.8.2-31-g4439849-dirty"),
        ("4439849", "4439849"),
    ],
)
def test_git_tag_loses_its_v(tmp_path, described, embedded):
    assert _embedded(tmp_path, f"echo {described}") == embedded


def test_no_git_gives_dev(tmp_path):
    assert _embedded(tmp_path, "exit 128") == "dev"


def test_explicit_version_is_kept(tmp_path):
    assert _embedded(tmp_path, "echo v9.9.9", "VERSION=0.8.3") == "0.8.3"
    assert _embedded(tmp_path / "b", "echo v9.9.9", "VERSION=0.8.3-pretest6") == "0.8.3-pretest6"
