"""Cross-node issue admission via a dedicated private GitHub coordination ref.

The coordinator is opt-in until an operator verifies every execution-capable
node uses the same protected ref. GitHub's non-forced ref update is the
linearization point: sibling commits cannot both fast-forward the same ref.
No claim expires by time; an uncertain write/worker retains its reservation.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import time

from .dispatch import ISSUE, timestamp

CONFIG = Path("/etc/gway-mcp-providers/agentops.coordination.json")
BRANCH = "agentops-coordination-v1"
STATE = "agentops-coordination.json"
MAX_BYTES = 512 * 1024
MAX_HISTORY = 4096
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")
_NODE = re.compile(r"gway-[0-9]{3}\Z")
_REPO = re.compile(r"arthexis/[A-Za-z0-9_.-]{1,100}\Z")


class CoordinationError(ValueError):
    """No admission may occur after an uncertain coordinator operation."""


def _require(condition):
    if not condition:
        raise CoordinationError("coordination_unavailable")


def _sha(value):
    _require(type(value) is str and _HEX40.fullmatch(value))
    return value


def _key(value):
    return hashlib.sha256(value.encode()).hexdigest()


def keys(ref, at):
    _require(type(ref) is str and ISSUE.fullmatch(ref))
    return _key(ref.split("#", 1)[0].casefold()), _key(ref.casefold() + "\n" + timestamp(at))


def load_config(path=CONFIG, *, node=None):
    """Absent configuration is legacy/manual-only; malformed configuration fails closed."""
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == 0
                 and not info.st_mode & 0o022 and info.st_nlink == 1
                 and info.st_size <= 4096)
        raw = stream.read(4097)
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise CoordinationError("coordination_unavailable") from None
    _require(type(data) is dict and set(data) == {"schema", "repository", "members", "epoch"}
             and data["schema"] == 1
             and type(data["repository"]) is str and _REPO.fullmatch(data["repository"])
             and type(data["epoch"]) is str and _TOKEN.fullmatch(data["epoch"])
             and type(data["members"]) is list and 1 <= len(data["members"]) <= 32
             and all(type(n) is str and _NODE.fullmatch(n) for n in data["members"])
             and len(set(data["members"])) == len(data["members"])
             and (node or socket.gethostname()) in data["members"])
    return data


def _claim_valid(claim):
    return (type(claim) is dict and set(claim) == {"identity", "node", "token"}
            and type(claim["identity"]) is str and _HEX64.fullmatch(claim["identity"])
            and type(claim["node"]) is str and _NODE.fullmatch(claim["node"])
            and type(claim["token"]) is str and _TOKEN.fullmatch(claim["token"]))


def validate_state(data, config):
    _require(type(data) is dict and set(data) == {"schema", "epoch", "members", "claims", "history"}
             and data["schema"] == 1 and data["epoch"] == config["epoch"]
             and data["members"] == config["members"]
             and type(data["claims"]) is dict and type(data["history"]) is list
             and len(data["history"]) <= MAX_HISTORY
             and all(type(x) is str and _HEX64.fullmatch(x) for x in data["history"])
             and len(set(data["history"])) == len(data["history"])
             and all(type(k) is str and _HEX64.fullmatch(k) and _claim_valid(v)
                     and v["node"] in config["members"] and v["identity"] in data["history"]
                     for k, v in data["claims"].items()))
    return data


class GitHubRefStore:
    """Fixed private repository, dedicated state-only branch, bounded authenticated API.

    Never force update, delete or create the ref. Provision it separately with
    a genesis state and protection against deletion and force pushes.
    """
    def __init__(self, config, *, runner=subprocess.run, timeout=12):
        self.config = config
        self.runner = runner
        self.timeout = timeout
        self.base = "repos/" + config["repository"]
        self.deadline = None

    def _api(self, path, payload=None, *, method="GET"):
        if self.deadline is None:
            self.deadline = time.monotonic() + self.timeout
        remaining = self.deadline - time.monotonic()
        _require(remaining > 0)
        args = ["gh", "api", "--hostname", "github.com", "--method", method, path]
        if payload is not None:
            args += ["--input", "-"]
        try:
            result = self.runner(args, input=json.dumps(payload) if payload is not None else None,
                                 capture_output=True, text=True, check=False,
                                 timeout=min(remaining, 5))
            _require(result.returncode == 0 and len(result.stdout) <= MAX_BYTES * 2)
            return json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            raise CoordinationError("coordination_unavailable") from None

    def read(self):
        self.deadline = time.monotonic() + self.timeout
        # Do not transmit operational state to a public repository.
        meta = self._api(self.base)
        _require(meta.get("private") is True
                 and str(meta.get("full_name", "")).casefold() == self.config["repository"].casefold())
        head = self._api(self.base + "/git/ref/heads/" + BRANCH)
        _require(head.get("ref") == "refs/heads/" + BRANCH and head.get("object", {}).get("type") == "commit")
        parent = _sha(head["object"]["sha"])
        blob = self._api(self.base + "/contents/" + STATE + "?ref=" + parent)
        _require(blob.get("type") == "file" and blob.get("encoding") == "base64"
                 and type(blob.get("size")) is int and 0 < blob["size"] <= MAX_BYTES)
        try:
            raw = base64.b64decode(blob["content"].replace("\n", ""), validate=True)
            _require(len(raw) == blob["size"])
            data = json.loads(raw)
        except (KeyError, ValueError, TypeError, UnicodeError):
            raise CoordinationError("coordination_unavailable") from None
        return parent, validate_state(data, self.config)

    def write(self, parent, data):
        """An unsuccessful/unknown response is never permission to launch."""
        _sha(parent)
        validate_state(data, self.config)
        content = json.dumps(data, sort_keys=True, separators=(",", ":"))
        _require(len(content.encode()) <= MAX_BYTES)
        # Dedicated branch contains only the state file. Each candidate commit
        # is a child of the exact parent read. A sibling write cannot fast-forward.
        tree = self._api(self.base + "/git/trees",
                         {"tree": [{"path": STATE, "mode": "100644", "type": "blob", "content": content}]},
                         method="POST")
        commit = self._api(self.base + "/git/commits",
                           {"message": "AgentOps coordination state", "tree": _sha(tree.get("sha")),
                            "parents": [parent]}, method="POST")
        candidate = _sha(commit.get("sha"))
        result = self._api(self.base + "/git/refs/heads/" + BRANCH,
                           {"sha": candidate, "force": False}, method="PATCH")
        _require(result.get("ref") == "refs/heads/" + BRANCH
                 and result.get("object", {}).get("sha") == candidate)


class Coordinator:
    def __init__(self, config, *, node=None, store=None):
        self.config = config
        self.node = node or socket.gethostname()
        _require(self.node in config["members"])
        self.store = store or GitHubRefStore(config)

    def acquire(self, ref, at):
        repo, identity = keys(ref, at)
        parent, data = self.store.read()
        if repo in data["claims"] or identity in data["history"]:
            return None
        _require(len(data["history"]) < MAX_HISTORY)
        claim = {"identity": identity, "node": self.node, "token": secrets.token_hex(16)}
        updated = {**data, "claims": {**data["claims"], repo: claim},
                   "history": [*data["history"], identity]}
        self.store.write(parent, updated)
        return claim

    def verify(self, ref, at, claim):
        repo, identity = keys(ref, at)
        _require(_claim_valid(claim) and claim["identity"] == identity and claim["node"] == self.node)
        _, data = self.store.read()
        _require(data["claims"].get(repo) == claim)
        return True

    def release(self, ref, at, claim):
        repo, identity = keys(ref, at)
        _require(_claim_valid(claim) and claim["identity"] == identity and claim["node"] == self.node)
        parent, data = self.store.read()
        _require(data["claims"].get(repo) == claim)
        remaining = dict(data["claims"])
        del remaining[repo]
        self.store.write(parent, {**data, "claims": remaining})


def configured():
    config = load_config()
    return Coordinator(config) if config is not None else None


def coordination_status():
    try:
        config = load_config()
        return {"mode": "configured" if config else "legacy", "active": False,
                "members": config["members"] if config else []}
    except (OSError, ValueError, TypeError):
        return {"mode": "unavailable", "active": False, "members": []}


def release_if_configured(ref, at, claim):
    """Call only after definitive terminal evidence under the local execution gate.

    A failed release retains the global claim; never infer that it is free.
    """
    if claim is None:
        return True
    try:
        coordinator = configured()
        if coordinator is None:
            return False
        coordinator.release(ref, at, claim)
        return True
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return False
