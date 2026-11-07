"""Emit the installed AgentOps MCP provider manifest for administrative deployment.

The output is data only. The deployment orchestrator publishes it to the
root-controlled provider installation directory after verifying the release.
"""
import argparse
import json
from pathlib import Path
import re


def manifest(release):
    release = Path(release).resolve(strict=True)
    revision = release.name
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("release must be a pinned SHA directory")
    executable = release / "bin" / "agent-mcp"
    if not executable.is_file() or not executable.stat().st_mode & 0o111:
        raise ValueError("installed MCP executable missing")
    return {"source_id": "agentops", "revision": revision,
            "executable": str(executable), "argv": [str(executable)]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", required=True)
    args = parser.parse_args()
    print(json.dumps(manifest(args.release), sort_keys=True))


if __name__ == "__main__":
    main()
