"""Validated Remote deployment target snapshots; no Remote import."""
import json
import re

REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SHA = re.compile(r"[0-9a-f]{40}")

def validate_snapshot(payload):
    if not isinstance(payload, dict) or set(payload) != {"schema", "revision", "targets"}:
        raise ValueError("invalid registry document")
    if payload["schema"] != 1 or not isinstance(payload["revision"], str) or not SHA.fullmatch(payload["revision"]):
        raise ValueError("invalid source revision")
    targets = payload["targets"]
    if not isinstance(targets, list) or not targets:
        raise ValueError("missing targets")
    names, repos = set(), set()
    for item in targets:
        if not isinstance(item, dict) or set(item) != {"name", "repository"}:
            raise ValueError("invalid target")
        name, repo = item["name"], item["repository"]
        if not isinstance(name, str) or not name or name in names or not isinstance(repo, str) or not REPO.fullmatch(repo) or repo in repos:
            raise ValueError("duplicate or invalid target")
        names.add(name)
        repos.add(repo)
    return payload["revision"], sorted(repos)

def parse_snapshot(raw):
    return validate_snapshot(json.loads(raw))
