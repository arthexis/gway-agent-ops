"""Node-local durable mailbox for Watchtower's pinned internal pull protocol.

No GitHub API, network listener or credential. A granted reservation remains
exclusive at Watchtower until exact local terminal evidence is committed.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import stat

from .dispatch import DEFAULT_STATE, ISSUE, Busy, timestamp, _private_directory

CONFIG = Path("/etc/gway-agent-ops/coordination.json")
CANARY_CONFIG = Path("/etc/gway-agent-ops/coordination-canary.json")
CANARY_ROOT = DEFAULT_STATE.parent/"coordination-canary"
HEX = re.compile(r"[a-f0-9]{64}\Z")
TOKEN = re.compile(r"[a-f0-9]{32}\Z")
NODE = re.compile(r"gway-[0-9]{3}\Z")
MAX_HISTORY = 100000
MAX_BATCH = 32
DB = "watchtower-coordination.sqlite3"


class CoordinationError(ValueError):
    code = "coordination_unavailable"


def require(ok):
    if not ok:
        raise CoordinationError("coordination_unavailable")


def matches(pattern,value):
    return type(value) is str and pattern.fullmatch(value) is not None


def keys(ref,at):
    require(matches(ISSUE,ref))
    hash_ = lambda s: hashlib.sha256(s.encode()).hexdigest()
    return hash_(ref.split("#",1)[0].casefold()),hash_(ref.casefold()+"\n"+timestamp(at))


def load_config(path=CONFIG,*,node=None):
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd,"rb") as stream:
        info=os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022
                and info.st_nlink==1 and info.st_size<=4096)
        data=json.loads(stream.read(4097))
    require(type(data) is dict and set(data)=={"schema","epoch","members","general_pickup_accepted"}
            and data["schema"]==1 and matches(TOKEN,data["epoch"])
            and type(data["members"]) is list and 1<=len(data["members"])<=32
            and all(matches(NODE,n) for n in data["members"])
            and len(set(data["members"]))==len(data["members"])
            and (node or socket.gethostname()) in data["members"]
            and type(data["general_pickup_accepted"]) is bool)
    return data


def _claim(row):
    return {k:row[k] for k in ("identity","node","token")}


class Mailbox:
    def __init__(self,root,config,*,node=None):
        self.root=Path(root)
        self.path=self.root/DB
        self.config=config
        self.node=node or socket.gethostname()
        require(self.node in config["members"])

    @contextmanager
    def tx(self):
        _private_directory(self.root)
        try:
            fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        info=self.path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid==os.geteuid()
                and not info.st_mode&0o077 and info.st_nlink==1)
        db=sqlite3.connect(self.path,timeout=5)
        db.row_factory=sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS meta(epoch TEXT,node TEXT,members TEXT)")
            db.execute("""CREATE TABLE IF NOT EXISTS intents(
                        identity TEXT PRIMARY KEY, repo TEXT NOT NULL, token TEXT NOT NULL,
                        node TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN
                          ('pending','reserved','terminal','released','replayed')),
                        ref TEXT NOT NULL, at TEXT NOT NULL)""")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS active_repo ON intents(repo) WHERE state IN ('pending','reserved','terminal')")
            meta=(self.config["epoch"],self.node,json.dumps(sorted(self.config["members"])))
            rows=[tuple(r) for r in db.execute("SELECT epoch,node,members FROM meta")]
            if not rows:
                require(db.execute("SELECT count(*) FROM intents").fetchone()[0]==0)
                db.execute("INSERT INTO meta VALUES (?,?,?)",meta)
            else:
                require(rows==[meta])
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def reserve(self,ref,at):
        at=timestamp(at)
        repo,identity=keys(ref,at)
        with self.tx() as db:
            row=db.execute("SELECT * FROM intents WHERE identity=?",(identity,)).fetchone()
            if row is not None:
                require((row["repo"],row["node"])==(repo,self.node))
                if row["state"]=="reserved":
                    return _claim(row)
                if row["state"] in ("released","replayed","terminal"):
                    return None
            elif not db.execute("SELECT 1 FROM intents WHERE repo=? AND state IN ('pending','reserved','terminal')",(repo,)).fetchone():
                require(db.execute("SELECT count(*) FROM intents").fetchone()[0]<MAX_HISTORY)
                db.execute("INSERT INTO intents VALUES (?,?,?,?,'pending',?,?)",
                           (identity,repo,secrets.token_hex(16),self.node,ref,at))
        return None

    def verify(self,ref,at,claim):
        repo,identity=keys(ref,at)
        with self.tx() as db:
            row=db.execute("SELECT * FROM intents WHERE identity=?",(identity,)).fetchone()
            require(row is not None and row["repo"]==repo and row["state"]=="reserved"
                    and _claim(row)==claim)
        return True

    def terminal(self,ref,at,claim):
        repo,identity=keys(ref,at)
        with self.tx() as db:
            row=db.execute("SELECT * FROM intents WHERE identity=?",(identity,)).fetchone()
            require(row is not None and row["repo"]==repo and _claim(row)==claim
                    and row["state"] in ("reserved","terminal","released"))
            if row["state"]=="reserved":
                db.execute("UPDATE intents SET state='terminal' WHERE identity=?",(identity,))

    def snapshot(self):
        with self.tx() as db:
            # Terminal releases first, then pending requests. Reserved rows
            # need no refresh; there is no lease and no expiry.
            rows=db.execute("""SELECT repo,identity,token,state FROM intents
                WHERE state IN ('pending','terminal')
                ORDER BY CASE state WHEN 'terminal' THEN 0 ELSE 1 END,rowid LIMIT ?""",(MAX_BATCH,)).fetchall()
            return [dict(row) for row in rows]

    def apply(self,rows):
        require(type(rows) is list and len(rows)<=MAX_BATCH)
        require(all(type(r) is dict and set(r)=={"repo","identity","token","state"}
                    and matches(HEX,r["repo"]) and matches(HEX,r["identity"])
                    and matches(TOKEN,r["token"]) and r["state"] in
                    ("reserved","busy","replayed","released") for r in rows))
        require(len({r["identity"] for r in rows})==len(rows))
        with self.tx() as db:
            for decision in rows:
                row=db.execute("SELECT * FROM intents WHERE identity=?",(decision["identity"],)).fetchone()
                require(row is not None and row["repo"]==decision["repo"] and row["token"]==decision["token"])
                state=decision["state"]
                # Delayed grant or busy response cannot resurrect terminal work.
                if state=="released":
                    require(row["state"] in ("terminal","released"))
                    db.execute("UPDATE intents SET state='released' WHERE identity=?",(row["identity"],))
                elif row["state"]=="pending" and state in ("reserved","replayed"):
                    db.execute("UPDATE intents SET state=? WHERE identity=?",(state,row["identity"]))
                elif row["state"]=="reserved" and state=="replayed":
                    # Contradictory central result blocks, rather than guessing.
                    raise CoordinationError("coordination_unavailable")
        return len(rows)


def configured(root=DEFAULT_STATE):
    config=load_config()
    if config is None:
        # Removing configuration cannot bypass a previously enrolled mailbox.
        path=Path(root)/DB
        require(not path.exists() and not path.is_symlink())
        return None
    return Mailbox(root,config)


def reserve_submission(ref,at,*,root):
    box=configured(root)
    if box is None:
        return None
    claim=box.reserve(ref,at)
    if claim is None:
        raise Busy("awaiting Watchtower reservation or identity already retired")
    return claim


def reserve_admission(ref,at,*,marker):
    box=configured(marker.parent.parent)
    require(not marker.is_symlink())
    if marker.exists():
        record=json.loads(marker.read_text())
        require(record.get("issue")==ref and record.get("at")==at)
        claim=record.get("coordination_claim")
        if box is None:
            require(claim is None)
            return None
        box.verify(ref,at,claim)
        return claim
    return reserve_submission(ref,at,root=marker.parent.parent)


def release_if_configured(ref,at,claim,*,root=DEFAULT_STATE):
    """Persist terminal evidence; Watchtower acknowledges release later."""
    if claim is None:
        return True
    try:
        box=configured(root)
        require(box is not None)
        box.terminal(ref,at,claim)
        return True
    except (OSError,ValueError,RuntimeError,KeyError,TypeError,sqlite3.Error):
        return False


def coordination_status():
    try:
        config=load_config()
        return {"mode":"watchtower" if config else "legacy",
                "general_pickup_accepted":bool(config and config["general_pickup_accepted"]),
                "members":config["members"] if config else []}
    except (OSError,ValueError,TypeError):
        return {"mode":"unavailable","general_pickup_accepted":False,"members":[]}


def general_pickup_ready():
    config=load_config()
    return bool(config and config["general_pickup_accepted"])


def exchange(request,*,root=DEFAULT_STATE):
    """Internal fixed CLI. No launch, shell or credential operations."""
    require(type(request) is dict and request.get("operation") in ("snapshot","apply"))
    snapshot=request["operation"]=="snapshot"
    require(set(request)==({"operation","epoch"} if snapshot else {"operation","epoch","decisions"}))
    box=configured(root)
    if box is None or request["epoch"]!=box.config["epoch"]:
        # Explicit owner-provisioned isolated fixture. Never used by any
        # actual submission/admission path. The epoch selects only this
        # fixed separate root, never a path from the request.
        canary=load_config(CANARY_CONFIG)
        require(canary is not None and not canary["general_pickup_accepted"]
                and canary["epoch"]==request["epoch"]
                and (box is None or box.config["epoch"]!=canary["epoch"]))
        box=Mailbox(CANARY_ROOT,canary)
    result={"epoch":box.config["epoch"],"node":box.node}
    if snapshot:
        result["intents"]=box.snapshot()
    else:
        result["applied"]=box.apply(request["decisions"])
    return result


def reconcile_terminal(ref,at,*,root=None):
    """Owner-only reconciliation of a settled or never-launched exact claim."""
    import fcntl
    from .capacity import repo_lock
    from .observation import observe
    from .reporting import _key
    root=Path(root or DEFAULT_STATE)
    at=timestamp(at)
    box=configured(root)
    require(box is not None)
    requests=root/"requests"
    _private_directory(requests)
    gate=os.open(requests/"submission.lock",os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(gate,fcntl.LOCK_EX|fcntl.LOCK_NB)
        fd=os.open(repo_lock(root,ref),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            state=observe(ref,at,state_dir=root)
            # Unknown plus no request/acceptance under both locks proves
            # no launch occurred; pending (ungranted) intents cannot retire.
            require(state.get("state") in ("unknown","released","preacceptance_failed") or
                    (state.get("state") in ("finished","codex_failed")
                     and type(state.get("exit_code")) is int))
            repo,identity=keys(ref,at)
            with box.tx() as db:
                row=db.execute("SELECT * FROM intents WHERE identity=?",(identity,)).fetchone()
                require(row is not None and row["repo"]==repo and row["state"] in ("reserved","terminal","released"))
                claim=_claim(row)
            box.terminal(ref,at,claim)
            return {"issue":ref,"at":at,"state":"coordination_release_recorded"}
        finally:
            os.close(fd)
    finally:
        os.close(gate)


def canary(action):
    """Fixed synthetic acceptance fixture; cannot execute or reserve real work."""
    require(action in ("prepare","retire","replay","status"))
    cfg=load_config(CANARY_CONFIG)
    require(cfg is not None and not cfg["general_pickup_accepted"])
    active=load_config()
    require(active is None or active["epoch"]!=cfg["epoch"])
    box=Mailbox(CANARY_ROOT,cfg)
    require(box.node in ("gway-001","gway-004"))
    own=1 if box.node=="gway-001" else 2
    number=3-own if action=="replay" else own
    ref="agentops-canary/synthetic#"+str(number)
    at="2020-01-01T00:00:00Z"
    if action in ("prepare","replay"):
        box.reserve(ref,at)
    elif action=="retire":
        claim=box.reserve(ref,at)
        require(claim is not None)
        box.terminal(ref,at,claim)
    with box.tx() as db:
        rows=[dict(row) for row in db.execute("SELECT ref,state FROM intents ORDER BY ref")]
    return {"node":box.node,"epoch":cfg["epoch"],"intents":rows}
