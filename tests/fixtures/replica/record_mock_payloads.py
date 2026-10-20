"""Record what tools/mock-agent.py serves for the integration's seed replica
files, over HTTP, as the payload goldens the in-process Replica engine is
checked against (tests/test_replica_engine.py).

    python3 tests/fixtures/replica/record_mock_payloads.py

Recorded before the engine was written, from the unchanged mock. Each case
is one mock run with some seed files preloaded, in order: /api/health,
/api/drives, every /api/drives/{id}, /api/filesystems and /api/pools.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SEED = ROOT / "custom_components" / "smart_sniffer" / "replicas"
OUT = Path(__file__).resolve().parent / "mock_payloads.json"

CASES = {stem.stem: [f"shipped.{stem.stem}"] for stem in sorted(SEED.glob("*.json"))}
CASES["board"] = ["shipped.sata-hdd-healthy", "shipped.nvme-wear", "shipped.zfs-pool-degraded",
                  "shipped.usb-smart-blocked"]


def _mock():
    spec = importlib.util.spec_from_file_location("ss_mock_record", ROOT / "tools" / "mock-agent.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["ss_mock_record"] = module
    spec.loader.exec_module(module)
    return module


def _get(base: str, path: str):
    try:
        with urllib.request.urlopen(base + path, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return {"_status": err.code}


def record() -> dict:
    out = {}
    for name, files in CASES.items():
        mock = _mock()
        server, _zc, _args = mock.build_server(
            ["--port", "0", "--host", "127.0.0.1", "--no-mdns", "--replicas", str(SEED),
             "--preload", ",".join(files), "--agent-id", "replica-golden-0001", "--hostname", ""])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{mock.MockHandler.port}"
        try:
            case = {"files": files, "health": _get(base, "/api/health"),
                    "drives": _get(base, "/api/drives"), "drive": {}}
            for summary in case["drives"]:
                case["drive"][summary["id"]] = _get(base, f"/api/drives/{summary['id']}")
            case["filesystems"] = _get(base, "/api/filesystems")
            case["pools"] = _get(base, "/api/pools")
            out[name] = case
        finally:
            server.shutdown()
            server.server_close()
    return out


if __name__ == "__main__":
    OUT.write_text(json.dumps(record(), indent=1, sort_keys=True) + "\n")
    print(f"wrote {OUT}")
