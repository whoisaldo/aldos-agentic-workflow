#!/usr/bin/env python3
"""Small, explicitly selected T3 conversations for file access and cache checks."""
import argparse
import datetime as dt
import json
from pathlib import Path
import uuid

from fleetlib import ROOT, atomic_write, encode, http_json, load_config, read_json, state_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "followup", "status", "archive"])
    parser.add_argument("--run", required=True)
    parser.add_argument("--profile", action="append", choices=["fleet-codex", "fleet-claude", "fleet-secondary"])
    args = parser.parse_args()
    if not args.run.replace("-", "").isalnum():
        raise ValueError("Use letters, digits and hyphens for the run name")
    config = load_config()
    state = state_dir(config)
    key = read_json(state / "private/t3-session.json")["access_token"]
    record = state / "private" / ("t3-check-" + args.run + ".json")
    def call(path, data=None):
        return http_json("http://127.0.0.1:3773" + path, key, "GET" if data is None else "POST", data, timeout=45)
    def dispatch(data):
        return call("/api/orchestration/dispatch", {"commandId": str(uuid.uuid4()), **data})
    def now():
        return dt.datetime.now(dt.timezone.utc).isoformat()
    def turn(t, text):
        dispatch({"type": "thread.turn.start", "threadId": t["id"], "message": {"messageId": str(uuid.uuid4()), "role": "user", "text": text, "attachments": []}, "runtimeMode": "auto-accept-edits", "interactionMode": "default", "createdAt": now()})
    if args.action == "start":
        if record.exists() or not args.profile:
            raise ValueError("Use a fresh run name and explicitly select profiles")
        shell = call("/api/orchestration/shell")
        project = next(p for p in shell["projects"] if Path(p["workspaceRoot"]) == ROOT)
        data = {"created_at": now(), "marker": "FLEET_" + uuid.uuid4().hex, "threads": {}}
        marker_file = ROOT / ".fleet" / (args.run + ".txt")
        atomic_write(marker_file, data["marker"])
        for name in args.profile:
            p = config["profiles"][name]
            t = {"id": str(uuid.uuid4()), "instance": p["t3_instance"], "followup": False}
            data["threads"][name] = t
            atomic_write(record, encode(data))
            dispatch({"type": "thread.create", "threadId": t["id"], "projectId": project["id"], "title": "Fleet check " + args.run + " " + name, "modelSelection": {"instanceId": t["instance"], "model": p["model"].replace("[1m]", "")}, "runtimeMode": "auto-accept-edits", "interactionMode": "default", "branch": None, "worktreePath": None, "createdAt": now()})
            turn(t, f"This is an isolated connectivity test. Read only .fleet/{args.run}.txt using a file-reading tool. Reply with its exact contents only. Do not edit files, create worktrees, or investigate anything else.")
            print("Started", name, t["id"])
        return
    data = read_json(record)
    for name, t in data["threads"].items():
        if args.profile and name not in args.profile:
            continue
        snapshot = call("/api/orchestration/threads/" + t["id"])
        thread = snapshot["thread"]
        atomic_write(state / "private" / ("t3-check-snapshot-" + t["id"] + ".json"), encode(snapshot))
        if args.action == "followup":
            if thread["latestTurn"]["state"] != "completed" or t["followup"]:
                raise ValueError("First turn must be complete; followup runs only once")
            turn(t, "Repeat the exact marker from the preceding turn and nothing else. Do not use tools.")
            t["followup"] = True
            atomic_write(record, encode(data))
            print("Followup", name)
        elif args.action == "archive":
            if (thread.get("session") or {}).get("activeTurnId"):
                raise ValueError("Owned check is still active")
            if len([m for m in thread["messages"] if m["role"] == "user"]) > 2:
                raise ValueError("Check thread contains additional user work; preserved")
            dispatch({"type": "thread.archive", "threadId": t["id"]})
            print("Archived owned check", name)
        else:
            messages = [m for m in thread["messages"] if m["role"] == "assistant"]
            contexts = [a["payload"] for a in thread["activities"] if a["kind"] == "context-window.updated"]
            print(json.dumps({"profile": name, "id": t["id"], "state": (thread.get("latestTurn") or {}).get("state"), "marker_retained": bool(messages and messages[-1]["text"].strip() == data["marker"]), "session": thread.get("session"), "context": contexts[-1:]}))


if __name__ == "__main__":
    main()
