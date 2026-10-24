#!/usr/bin/env python3
"""Render every CPU-only stage in a new private scratch directory, without Docker.

The resulting plans are explicitly offline and cannot be applied. The printed
directory is retained so a reviewer can inspect or run Compose config validation.
"""
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from samurai_stack import engine
from samurai_stack.cli import default_config
from samurai_stack.safety import write_json


def main():
    if not sys.platform.startswith("linux"):
        raise SystemExit("Run this scratch integration renderer on Linux")
    scratch = Path(tempfile.mkdtemp(prefix="samurai-offline-"))
    c = default_config("192.168.50.20")
    c["install_root"] = str(scratch / "application")
    c["storage"] = {"mode": "local", "data_root": str(scratch / "data"),
                    "allow_existing_contents": False,
                    "expected_mount": {"target": str(scratch), "source": "offline-fixture", "fstype": "ext4"}}
    c["hardware"] = {"acceleration": "cpu"}
    c["features"].update(photos=True, music=True)
    write_json(scratch / "site.json", c)
    results = []
    for stage in sorted(engine.STAGES):
        document = engine.plan(c, stage, offline=True)
        write_json(scratch / f"stage-{stage}.plan.json", document)
        if stage:
            path = scratch / f"stage-{stage}.compose.json"
            write_json(path, document["compose_preview"])
        results.append({"stage": stage, "review_hash": document["review_hash"],
                        "services": sorted((document["compose_preview"] or {}).get("services", {}))})
    print(json.dumps({"scratch_directory": str(scratch), "offline": True,
                      "containers_started": 0, "results": results}, indent=2))


if __name__ == "__main__":
    main()
