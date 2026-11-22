#!/usr/bin/env python3
"""Move installed custom skills to a fleet release during an authorized idle window.

This changes skill entries only. Account settings, credentials, session histories,
built-in skills, plugins and organization-synced directories are never copied.
"""
import argparse
import datetime as dt
import os
from pathlib import Path
import shutil

from fleetlib import (allow_windows_user, atomic_write, encode, expand,
                      install_skill_link, link_matches, load_config, profile, read_json,
                      skill_allowed, state_dir, t3_get, tree_hash, verify_release)

# T3's own rule for a live protocol 2 shell thread also counts these statuses.
LIVE_T3_STATUSES = {"queued", "preparing", "starting", "running", "waiting"}
# Statuses a protocol 2 thread has when nothing runs. Any status outside both sets counts as live.
SETTLED_T3_STATUSES = {"completed", "idle", "failed", "interrupted", "cancelled"}


def t3_thread_live(t):
    """A turn that is running or waiting, in a protocol 1 or protocol 2 shell."""
    return bool((t.get("session") or {}).get("activeTurnId")
                or (t.get("latestTurn") or {}).get("state") == "running"
                or t.get("activeRunId") or t.get("activityRunStatus")
                or t.get("status") in LIVE_T3_STATUSES
                or ("status" in t and t["status"] not in SETTLED_T3_STATUSES)
                or t.get("pendingRuntimeRequest") or t.get("pendingBackgroundTasks"))


def shell_threads(shell):
    """Every thread a shell snapshot lists, archived ones included. A snapshot or thread this
    check cannot read refuses, so an unexpected server answer never counts as idle.

    A protocol 2 server's HTTP shell leaves archived threads out, and T3 lets a running thread be
    archived while its provider detaches. Do not archive a running thread during an idle window."""
    threads = shell.get("threads") if isinstance(shell, dict) else None
    archived = shell.get("archivedThreads", []) if isinstance(shell, dict) else None
    if not isinstance(threads, list) or not isinstance(archived, list):
        raise ValueError("T3's shell snapshot has an unexpected shape; preserved all profiles")
    for t in threads + archived:
        if not isinstance(t, dict) or not isinstance(t.get("id"), str) or not t["id"] \
                or not any(k in t for k in ("status", "session", "latestTurn")):
            raise ValueError("T3's shell lists a thread this check cannot read; preserved all profiles")
    return threads + archived


def assert_idle(state, allowed=()):
    auth = read_json(state / "private/t3-session.json")
    shell = t3_get("/api/orchestration/shell", auth["access_token"])
    active = [t["id"] for t in shell_threads(shell) if t3_thread_live(t) and t["id"] not in allowed]
    if active:
        raise ValueError("Other T3 turns are active; preserved all profiles: " + ", ".join(active))


def fingerprint(path):
    if hasattr(path, "is_junction") and path.is_junction():
        return {"junction": str(path.resolve())}
    if path.is_symlink():
        return {"link": os.readlink(path)}
    if path.is_dir():
        return {"tree": tree_hash(path)}
    if path.exists():
        raise ValueError(f"Expected a skill directory: {path}")
    return None


def plan(config, release, names, shared=False):
    manifest = verify_release(release)
    changes = []
    homes = [(name, profile(config, name)) for name in names]
    if shared:
        homes.append(("shared", {"home": expand("~/.agents"), "driver": "codex", "identity": "shared"}))
    known_roots = [expand("~/agent-skills"), expand("~/.agents/skills"), state_dir(config) / "releases"]
    for name, p in homes:
        if not p["home"].exists() and name != "shared":
            continue
        for skill in manifest["skills"]:
            source = release / "skills" / skill
            path = p["home"] / "skills" / skill
            excluded = not skill_allowed(config, p, source)
            if excluded and not path.exists() and not path.is_symlink():
                continue
            canonical = os.name != "nt" or path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
            if not excluded and canonical and link_matches(path, source):
                continue
            before = fingerprint(path)
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                target = path.resolve()
                if not any(target.is_relative_to(root.resolve()) for root in known_roots):
                    raise ValueError(f"Unrecognized skill link, review before migrating: {path}")
            elif before and not (path / "SKILL.md").is_file():
                raise ValueError(f"Not an installed skill, preserved: {path}")
            changes.append({"profile": name, "path": str(path), "source": None if excluded else str(source), "before": before})
    return changes


def apply(config, release, changes, idle_check):
    # Check every entry before touching the first; detect edits after the plan.
    idle_check()
    for entry in changes:
        if fingerprint(Path(entry["path"])) != entry["before"]:
            raise ValueError("Skill changed after planning: " + entry["path"])
    if not changes:
        return None
    state = state_dir(config)
    backup = state / "backups" / ("skills-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    backup.mkdir(parents=True, mode=0o700)
    receipt = {"release": release.name, "changes": changes, "complete": False}
    atomic_write(backup / "manifest.json", encode(receipt))
    for i, entry in enumerate(changes):
        path = Path(entry["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if entry["before"] is not None:
            shutil.move(str(path), str(backup / str(i)))
        if entry["source"]:
            source = Path(entry["source"])
            install_skill_link(path, source)
        entry["applied"] = True
        atomic_write(backup / "manifest.json", encode(receipt))
    for home in {Path(e["path"]).parent.parent for e in changes}:
        allow_windows_user(home / "skills")
        if (home / ".fleet-profile.json").exists():
            record = read_json(home / ".fleet-profile.json")
            atomic_write(backup / (home.name + "-profile.json"), encode(record))
            record["revision"] = release.name
            atomic_write(home / ".fleet-profile.json", encode(record))
    receipt["complete"] = True
    atomic_write(backup / "manifest.json", encode(receipt))
    previous = read_json(state / "skills-deployment.json") if (state / "skills-deployment.json").exists() else {}
    entries = dict(previous.get("entries", {}))
    entries.update({e["path"]: e["source"] for e in changes})
    homes = set(previous.get("homes", [])) | {str(Path(e["path"]).parent.parent) for e in changes}
    atomic_write(state / "skills-deployment.json", encode({"release": release.name, "backup": str(backup), "homes": sorted(homes), "entries": entries}))
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", action="append")
    parser.add_argument("--shared", action="store_true")
    parser.add_argument("--release")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--idle", action="store_true", help="The user authorized this maintenance window")
    parser.add_argument("--allow-thread", action="append", default=[], help="Only the maintenance thread itself may remain active")
    parser.add_argument("--control-host", action="store_true", help="A control laptop without a managed T3 operator session")
    args = parser.parse_args()
    config = load_config()
    state = state_dir(config)
    revision = args.release or read_json(state / "latest-release.json")["revision"]
    if len(revision) != 20 or any(c not in "0123456789abcdef" for c in revision):
        raise ValueError("Invalid release id")
    release = state / "releases" / revision
    changes = plan(config, release, args.profile or list(config["profiles"]), args.shared)
    for entry in changes:
        print(("INSTALL " if entry["source"] else "REMOVE PERSONAL BRIDGE ") + entry["path"])
    print(f"{len(changes)} skill entries; settings, authentication and history preserved")
    if args.apply:
        if not args.idle:
            raise ValueError("Apply requires an explicitly authorized idle window (--idle)")
        if args.control_host and (state / "private/t3-session.json").exists():
            raise ValueError("This host has a T3 session; use the live idle check")
        check = (lambda: None) if args.control_host else lambda: assert_idle(state, args.allow_thread)
        print("Backup:", apply(config, release, changes, check))


if __name__ == "__main__":
    main()
