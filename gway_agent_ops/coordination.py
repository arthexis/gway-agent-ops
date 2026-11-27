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
    _require(type(data) is dict and set(data) == {"schema", "repository", "members", "epoch", "general_pickup_accepted"}
             and data["schema"] == 1
             and type(data["general_pickup_accepted"]) is bool
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

    def acquire(self, ref, at, *, token=None):
        repo, identity = keys(ref, at)
        token = token or secrets.token_hex(16)
        _require(type(token) is str and _TOKEN.fullmatch(token))
        claim = {"identity": identity, "node": self.node, "token": token}
        parent, data = self.store.read()
        # Only a locally durable, identical intent can adopt a previously
        # uncertain CAS. A different node or invocation cannot adopt it.
        if data["claims"].get(repo) == claim:
            return claim
        if repo in data["claims"] or identity in data["history"]:
            return None
        _require(len(data["history"]) < MAX_HISTORY)
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
        if data["claims"].get(repo) != claim:
            # Never remove a later owner's reservation. If this identity
            # was reserved and is no longer active, release already settled.
            _require(identity in data["history"] and not any(
                x["identity"] == identity for x in data["claims"].values()))
            return
        remaining = dict(data["claims"])
        del remaining[repo]
        self.store.write(parent, {**data, "claims": remaining})


def configured():
    config = load_config()
    return Coordinator(config) if config is not None else None


def coordination_status():
    try:
        config = load_config()
        return {"mode": "configured" if config else "legacy",
                "general_pickup_accepted": bool(config and config["general_pickup_accepted"]),
                "members": config["members"] if config else []}
    except (OSError, ValueError, TypeError):
        return {"mode": "unavailable", "general_pickup_accepted": False, "members": []}


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


def _intent(root, ref, at, coordinator):
    """Persist the exact token before any remote write, under the local gate."""
    from .dispatch import _private_directory
    from .reporting import _key, _write
    folder = Path(root) / "coordination-intents"
    _private_directory(folder)
    marker = folder / (_key(ref, at) + ".json")
    if marker.is_symlink():
        raise CoordinationError("coordination_unavailable")
    repo, identity = keys(ref, at)
    if marker.exists():
        record = json.loads(marker.read_text(encoding="utf-8"))
        _require(type(record) is dict and set(record) == {"schema", "epoch", "repo", "claim"}
                 and record["schema"] == 1 and record["epoch"] == coordinator.config["epoch"]
                 and record["repo"] == repo and _claim_valid(record["claim"])
                 and record["claim"]["identity"] == identity
                 and record["claim"]["node"] == coordinator.node)
        return record["claim"]["token"]
    token = secrets.token_hex(16)
    _write(marker, {"schema": 1, "epoch": coordinator.config["epoch"], "repo": repo,
                    "claim": {"identity": identity, "node": coordinator.node, "token": token}})
    return token


def _reserve_new(coordinator, root, ref, at):
    return coordinator.acquire(ref, at, token=_intent(root, ref, at, coordinator))


def reserve_submission(ref, at, *, root):
    """Under the existing node submission gate; None means legacy mode."""
    coordinator = configured()
    if coordinator is None:
        # Removing configuration cannot bypass an already-persisted intent.
        from .reporting import _key
        intent = Path(root) / "coordination-intents" / (_key(ref, at) + ".json")
        _require(not intent.exists() and not intent.is_symlink())
        return None
    claim = _reserve_new(coordinator, root, ref, at)
    if claim is None:
        from .dispatch import Busy
        raise Busy("shared repository or invocation already reserved")
    return claim


def reserve_admission(ref, at, *, marker):
    """Adopt only the exact durable reservation made before systemd launch."""
    coordinator = configured()
    if marker.is_symlink():
        raise CoordinationError("coordination_unavailable")
    if marker.exists():
        record = json.loads(marker.read_text(encoding="utf-8"))
        _require(record.get("issue") == ref and record.get("at") == at)
        claim = record.get("coordination_claim")
        if coordinator is None:
            _require(claim is None)
            return None
        coordinator.verify(ref, at, claim)
        return claim
    if coordinator is None:
        from .reporting import _key
        intent = marker.parent.parent / "coordination-intents" / (_key(ref, at) + ".json")
        _require(not intent.exists() and not intent.is_symlink())
        return None
    claim = _reserve_new(coordinator, marker.parent.parent, ref, at)
    if claim is None:
        from .dispatch import Busy
        raise Busy("shared repository or invocation already reserved")
    return claim


def general_pickup_ready():
    """Operator attestation is separate from local canary and live claim checks."""
    config = load_config()
    return bool(config and config["general_pickup_accepted"])


def reconcile_terminal(ref, at, *, root=None):
    """Explicit owner recovery of an exact, definitively settled local claim.

    Never launches work, never expires a claim, never guesses interruption.
    A local intent without a launch/acceptance is not terminal evidence.
    """
    import fcntl
    from .dispatch import DEFAULT_STATE, _private_directory
    from .capacity import repo_lock
    from .observation import observe
    from .reporting import _key
    root = Path(root or DEFAULT_STATE)
    at = timestamp(at)
    coordinator = configured()
    _require(coordinator is not None)
    _private_directory(root)
    requests = root / "requests"
    _private_directory(requests)
    gate = os.open(requests / "submission.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd = os.open(repo_lock(root, ref), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = observe(ref, at, state_dir=root)
            _require(result.get("state") in ("released", "preacceptance_failed") or
                     (result.get("state") in ("finished", "codex_failed")
                      and type(result.get("exit_code")) is int))
            marker = requests / (_key(ref, at) + ".json")
            claim = None
            if marker.is_file() and not marker.is_symlink():
                row = json.loads(marker.read_text(encoding="utf-8"))
                _require(row.get("issue") == ref and row.get("at") == at)
                claim = row.get("coordination_claim")
            journal = root / "accepted.jsonl"
            if journal.exists():
                _require(not journal.is_symlink())
                for line in journal.read_text(encoding="utf-8").splitlines():
                    row = json.loads(line)
                    if row.get("issue") == ref and row.get("at") == at:
                        other = row.get("coordination_claim")
                        _require(claim is None or other == claim)
                        claim = other
            _require(_claim_valid(claim))
            coordinator.release(ref, at, claim)
            return {"issue": ref, "at": at, "state": "coordination_released"}
        finally:
            os.close(fd)
    finally:
        os.close(gate)
