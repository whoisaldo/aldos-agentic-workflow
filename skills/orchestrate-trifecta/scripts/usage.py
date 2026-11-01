#!/usr/bin/env python3
"""Print the last usage reading for every role in the trifecta config.

Usage gateways record the rate-limit headers of every response that passes
through them. A role only has fresh numbers if a request went through its
source recently, so each reading carries its age and is marked STALE after
stale_minutes. Read-only: it never signs in, switches accounts or starts tasks.

The config is TRIFECTA_CONFIG, or trifecta.json next to SKILL.md. Copy
trifecta.example.json to start. Without a valid config nothing is fetched.
"""
import argparse
import datetime as dt
import http.client
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = SKILL_DIR / "trifecta.json"
MAX_NAP_SECONDS = 110 * 60  # under the two-hour cap on a background command
FIELD_KEYS = {"five_hour_used", "five_hour_reset", "weekly_used", "weekly_reset", "status"}
# TRIFECTA_<ROLE>_GATEWAY_URL and _KEY_FILE override the usage source of that role.
ENV_OVERRIDES = [("TRIFECTA_COORDINATOR", "coordinator"), ("TRIFECTA_BUILDER", "builder1"), ("TRIFECTA_REVIEWER", "reviewer1")]
ROLE_KEYS = {
    "coordinator": {"label", "providerInstanceId", "model", "options", "usage"},
    "builder": {"label", "providerInstanceId", "model", "options", "usage", "switch_at_percent"},
    "reviewer": {"label", "providerInstanceId", "model", "options", "usage", "weight"},
}
SOURCE_KEYS = {"type", "url", "key_file", "provider", "used_unit", "fields"}
TOP_KEYS = {"coordinator", "builders", "reviewers", "max_parallel_builders", "never_use", "stale_minutes", "usage_sources"}


def config_path(environ):
    return Path(environ["TRIFECTA_CONFIG"]).expanduser() if environ.get("TRIFECTA_CONFIG") else DEFAULT_CONFIG


def default_weight(reviewer):
    """7 for a Codex/GPT reviewer and 3 for a Claude reviewer, the 70/30 split."""
    ident = f"{reviewer.get('providerInstanceId', '')} {reviewer.get('model', '')}".lower()
    if "codex" in ident or "gpt" in ident:
        return 7
    if "claude" in ident:
        return 3
    return None


def unknown_keys(obj, allowed, where, errors):
    for key in sorted(set(obj) - allowed):
        if not key.startswith("_"):  # "_comment" keys are allowed anywhere
            errors.append(f"{where}: unknown key {key!r}")


def validate(raw):
    """Return (config with defaults filled in, list of errors)."""
    if not isinstance(raw, dict):
        return None, ["the config must be a JSON object"]
    errors = []
    unknown_keys(raw, TOP_KEYS, "config", errors)
    config = {
        "max_parallel_builders": raw.get("max_parallel_builders", 2),
        "never_use": raw.get("never_use", []),
        "stale_minutes": raw.get("stale_minutes", 20),
        "usage_sources": raw.get("usage_sources", {}),
    }
    if not isinstance(config["max_parallel_builders"], int) or config["max_parallel_builders"] < 1:
        errors.append("max_parallel_builders: must be a whole number of at least 1")
    if not isinstance(config["stale_minutes"], (int, float)) or config["stale_minutes"] <= 0:
        errors.append("stale_minutes: must be a positive number")
    if not isinstance(config["never_use"], list) or not all(isinstance(x, str) for x in config["never_use"]):
        errors.append("never_use: must be a list of provider instance ids")
        config["never_use"] = []

    sources = config["usage_sources"]
    if not isinstance(sources, dict):
        errors.append("usage_sources: must be an object of named sources")
        sources = config["usage_sources"] = {}
    for name, source in sources.items():
        where = f"usage_sources.{name}"
        if not isinstance(source, dict):
            errors.append(f"{where}: must be an object")
            continue
        unknown_keys(source, SOURCE_KEYS, where, errors)
        if source.get("type") != "cliproxyapi":
            errors.append(f"{where}.type: only \"cliproxyapi\" is supported")
        if not str(source.get("url", "")).startswith(("http://", "https://")):
            errors.append(f"{where}.url: must be an http(s) URL")
        for key in ("key_file", "provider"):
            if not isinstance(source.get(key), str) or not source.get(key):
                errors.append(f"{where}.{key}: required")
        source.setdefault("used_unit", "fraction")
        if source["used_unit"] not in {"fraction", "percent"}:
            errors.append(f"{where}.used_unit: \"fraction\" or \"percent\"")
        fields = source.get("fields")
        if not isinstance(fields, dict) or not all(isinstance(v, str) for v in fields.values()):
            errors.append(f"{where}.fields: map of reading fields to header names")
        else:
            unknown_keys(fields, FIELD_KEYS, f"{where}.fields", errors)
            if not {"five_hour_used", "weekly_used"} & set(fields):
                errors.append(f"{where}.fields: needs five_hour_used or weekly_used")

    def role(obj, kind, where, defaults):
        if not isinstance(obj, dict):
            errors.append(f"{where}: must be an object")
            return None
        unknown_keys(obj, ROLE_KEYS[kind], where, errors)
        entry = {**defaults, "options": {}, **obj}
        required = ["label"] if kind == "coordinator" else ["label", "providerInstanceId", "model"]
        for key in required:
            if not isinstance(entry.get(key), str) or not entry.get(key):
                errors.append(f"{where}.{key}: required")
        if not isinstance(entry["options"], dict):
            errors.append(f"{where}.options: must be an object")
        if "usage" in entry and (not isinstance(entry["usage"], str) or entry["usage"] not in sources):
            errors.append(f"{where}.usage: no usage source named {entry['usage']!r}")
        if entry.get("providerInstanceId") in config["never_use"]:
            errors.append(f"{where}.providerInstanceId: listed in never_use")
        return entry

    config["coordinator"] = role(raw.get("coordinator"), "coordinator", "coordinator", {})
    for kind, key in (("builder", "builders"), ("reviewer", "reviewers")):
        items = raw.get(key)
        if not isinstance(items, list) or not items:
            errors.append(f"{key}: must be a non-empty list")
            items = []
        defaults = {"switch_at_percent": 85} if kind == "builder" else {}
        config[key] = [role(item, kind, f"{key}[{i}]", defaults) for i, item in enumerate(items)]
    for i, builder in enumerate(config["builders"]):
        percent = builder and builder["switch_at_percent"]
        if builder and (not isinstance(percent, (int, float)) or not 0 < percent <= 100):
            errors.append(f"builders[{i}].switch_at_percent: a number from 1 to 100")
    for i, reviewer in enumerate(config["reviewers"]):
        if reviewer is None:
            continue
        reviewer.setdefault("weight", default_weight(reviewer))
        if not isinstance(reviewer["weight"], (int, float)) or reviewer["weight"] <= 0:
            errors.append(f"reviewers[{i}].weight: required, a positive number")
    return config, errors


def apply_env_overrides(config, environ):
    for prefix, role_id in ENV_OVERRIDES:
        name = next((r.get("usage") for r in roles(config) if r["id"] == role_id), None)
        if name and name in config["usage_sources"]:
            source = config["usage_sources"][name]
            source["url"] = environ.get(prefix + "_GATEWAY_URL") or source.get("url")
            source["key_file"] = environ.get(prefix + "_KEY_FILE") or source.get("key_file")


def load_config(environ):
    """Return (path, config or None, problems). Never touches the network."""
    path = config_path(environ)
    if not path.is_file():
        return path, None, [f"no config at {path}: copy trifecta.example.json there, or set TRIFECTA_CONFIG"]
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        return path, None, [f"cannot read {path}: {error}"]
    config, errors = validate(raw)
    if errors:
        return path, None, errors
    apply_env_overrides(config, environ)
    _, errors = validate(json.loads(json.dumps(config)))  # recheck overridden URLs
    return path, (None if errors else config), errors


def roles(config):
    result = [{"id": "coordinator", "kind": "coordinator", **config["coordinator"]}]
    result += [{"id": f"builder{i}", "kind": "builder", **b} for i, b in enumerate(config["builders"], 1)]
    result += [{"id": f"reviewer{i}", "kind": "reviewer", **r} for i, r in enumerate(config["reviewers"], 1)]
    return result


def fetch_auth_files(url, key):
    request = urllib.request.Request(url.rstrip("/") + "/v0/management/auth-files", headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)["files"]


def request_error(error):
    """A printable category for a failed request. Exception text can hold the key,
    URL credentials or a response body, so it is never printed."""
    if isinstance(error, urllib.error.HTTPError):
        return f"gateway answered HTTP {error.code}"
    if isinstance(error, http.client.InvalidURL):
        return "gateway URL is invalid"
    if isinstance(error, (OSError, http.client.HTTPException)):
        return "gateway unreachable"
    if isinstance(error, json.JSONDecodeError):
        return "gateway response is not JSON"
    if isinstance(error, (KeyError, TypeError, AttributeError)):
        return "unexpected gateway response"
    if isinstance(error, ValueError):  # e.g. a key with a newline fails header validation
        return "request not sent, check the key file"
    return "request failed"


def request_files(source, fetch):
    """(auth files, None), or (None, a problem that is safe to print)."""
    try:
        key = json.loads(Path(source["key_file"]).expanduser().read_text())["management_key"]
    except OSError:
        return None, "key file unreadable"
    except (ValueError, KeyError, TypeError):
        key = None
    if not isinstance(key, str):
        return None, "key file has no management_key string"
    try:
        return fetch(source["url"], key), None
    except Exception as error:  # a down gateway must not stop the coordinator
        if isinstance(error, urllib.error.HTTPError):
            error.close()  # it holds the response body, which is never read
        return None, f"{request_error(error)} ({type(error).__name__})"


def latest_reading(auth):
    """The newest non-empty signal set among the account and per-model views."""
    readings = [auth.get("quota") or {}] + list((auth.get("model_quotas") or {}).values())
    readings = [r for r in readings if r.get("signals") and r.get("observed_at")]
    return max(readings, key=lambda r: dt.datetime.fromisoformat(r["observed_at"]), default=None)


def local_time(moment):
    return moment.astimezone().isoformat(timespec="minutes")


def window(used, reset, now):
    """A used fraction and its reset; a window whose reset has passed is empty."""
    if used is None:
        return None
    if reset and reset <= now:
        return {"used": 0.0, "resets_at": None, "rolled_over": True}
    return {"used": round(used, 2), "resets_at": local_time(reset) if reset else None}


def summarize(source, reading, now, stale_minutes):
    """The printed reading, plus (used, reset) for each window as it was read."""
    signals, fields = reading["signals"], source["fields"]
    scale = 100 if source["used_unit"] == "percent" else 1

    def used(key):
        value = signals.get(fields[key]) if key in fields else None
        return None if value is None else float(value) / scale

    def reset(key):
        value = signals.get(fields[key]) if key in fields else None
        return dt.datetime.fromtimestamp(int(float(value)), dt.timezone.utc) if value else None

    observed = dt.datetime.fromisoformat(reading["observed_at"])
    out = {"age_minutes": int((now - observed).total_seconds() // 60)}
    windows = []
    for name in ("five_hour", "weekly"):
        raw = (used(name + "_used"), reset(name + "_reset"))
        out[name] = window(*raw, now)
        if raw[0] is not None:
            windows.append(raw)
    out["status"] = signals.get(fields["status"]) if "status" in fields else None
    out["stale"] = out["age_minutes"] > stale_minutes
    return out, windows


def blocking(windows, status, threshold, now):
    """(blocked, latest reset) over every window that was at or over threshold
    when read and has not reset since. A rejection that names no such window
    holds until every window in the reading has reset."""
    over = [w for w in windows if w[0] * 100 >= threshold]
    if status == "rejected" and not over:
        over = windows or [(None, None)]  # rejected with no window numbers: blocked, reset unknown
    pending = [reset for _, reset in over if reset is None or reset > now]
    return bool(pending), max(filter(None, pending), default=None)


def read_roles(config, now, fetch=fetch_auth_files):
    result, responses = {}, {}
    for role in roles(config):
        entry = {"kind": role["kind"], "label": role["label"], "providerInstanceId": role.get("providerInstanceId")}
        result[role["id"]] = entry
        if not role.get("usage"):
            entry["error"] = "no usage source configured"
            continue
        name = role["usage"]
        source = config["usage_sources"][name]
        target = (source["url"], source["key_file"])
        if target not in responses:  # several roles may share one gateway
            responses[target] = request_files(source, fetch)
        files, problem = responses[target]
        try:
            auth = None if problem else next((f for f in files if f.get("provider") == source["provider"]), None)
            reading = auth and latest_reading(auth)
            if reading:
                summary, windows = summarize(source, reading, now, config["stale_minutes"])
                # Builders switch at their threshold; any role is blocked once a window runs out.
                blocked, reset = blocking(windows, summary["status"], role.get("switch_at_percent", 100), now)
                summary["blocking_reset"] = local_time(reset) if reset else None
                if role["kind"] == "builder":
                    summary["at_threshold"] = blocked
        except Exception as error:  # parse errors can quote response data; print only the class
            problem = f"unreadable reading ({type(error).__name__})"
        if problem:
            entry["error"] = f"{problem} on usage source {name}"
        elif auth is None:
            entry["error"] = f"no {source['provider']} account on usage source {name}"
        elif not reading:
            entry["error"] = "no reading yet"
        else:
            entry.update(summary)
    return result


def pct(w):
    if w is None:
        return "none"
    text = f"{round(w['used'] * 100)}%"
    if w.get("rolled_over"):
        return text + " (window reset since the reading)"
    return text + (f", resets {w['resets_at']}" if w.get("resets_at") else "")


def sleep_until_reset(role_id, readings, now, sleep):
    """Exit 0 once every blocking window has reset, 3 if the caller should run this again."""
    entry = readings[role_id]
    resets_at = entry.get("blocking_reset")
    if not resets_at:
        # No reading, or no blocking window left: nothing to wait for.
        reason = f" ({entry['error']})" if "error" in entry else ""
        print(f"{role_id}: no pending reset on record{reason}; probe the account now")
        return 0
    remaining = (dt.datetime.fromisoformat(resets_at) - now()).total_seconds() + 60
    nap = max(0, min(remaining, MAX_NAP_SECONDS))
    print(f"{role_id}: resets {resets_at}; sleeping {int(nap // 60)} min", flush=True)
    sleep(nap)
    if remaining > MAX_NAP_SECONDS:
        print(f"{role_id}: reset still ahead; run this again")
        return 3
    print(f"{role_id}: the window has reopened; probe it and move builders back")
    return 0


def shown_url(url):
    """The URL without user info, query or fragment, which can carry credentials."""
    parts = urllib.parse.urlsplit(url)
    return parts._replace(netloc=parts.netloc.rpartition("@")[2], query="", fragment="").geturl()


def describe(config, environ, path):
    """The role table printed by --check-config."""
    total = sum(r["weight"] for r in config["reviewers"])
    lines = [f"config: {path}", "",
             f"{'ROLE':<12} {'LABEL':<32} {'PROVIDER INSTANCE':<26} {'MODEL':<18} {'OPTIONS':<26} {'USAGE':<20} RULE"]
    for role in roles(config):
        if role["kind"] == "builder":
            rule = f"switch at {role['switch_at_percent']}%"
        elif role["kind"] == "reviewer":
            rule = f"weight {role['weight']} ({round(100 * role['weight'] / total)}% of reviews)"
        else:
            rule = "coordinates; the thread you are in"
        lines.append(f"{role['id']:<12} {role['label']:<32} {role.get('providerInstanceId') or '-':<26} "
                     f"{role.get('model') or '-':<18} {json.dumps(role['options'], sort_keys=True):<26} "
                     f"{role.get('usage') or '-':<20} {rule}")
    lines += ["", f"max_parallel_builders: {config['max_parallel_builders']}",
              f"stale_minutes: {config['stale_minutes']}",
              f"never_use: {', '.join(config['never_use']) or 'none'}", "", "usage sources:"]
    for name, source in config["usage_sources"].items():
        key_file = Path(source["key_file"]).expanduser()
        note = "" if key_file.is_file() else "  (key file not found yet)"
        lines.append(f"  {name}: {source['type']} {shown_url(source['url'])} provider={source['provider']} "
                     f"unit={source['used_unit']} key_file={source['key_file']}{note}")
    overrides = [k for prefix, _ in ENV_OVERRIDES for k in (prefix + "_GATEWAY_URL", prefix + "_KEY_FILE") if environ.get(k)]
    if overrides:
        lines.append("environment overrides: " + ", ".join(overrides))
    return "\n".join(lines)


def main(argv=None, environ=None, fetch=fetch_auth_files, now=None, sleep=time.sleep):
    environ = os.environ if environ is None else environ
    now = now or (lambda: dt.datetime.now(dt.timezone.utc))
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="Print the readings as JSON")
    parser.add_argument("--check-config", action="store_true", help="Validate the config and print the role table; no network")
    parser.add_argument("--sleep-until-reset", metavar="ROLE",
                        help="Sleep until a minute after the last reset of ROLE's blocking windows (coordinator, builder1, reviewer1, ...); "
                             "exit 3 means the reset is still ahead, run it again")
    args = parser.parse_args(argv)
    path, config, problems = load_config(environ)

    if args.check_config:
        if problems:
            print(f"config: {path}\n" + "\n".join(f"error: {p}" for p in problems), file=sys.stderr)
            return 1
        print(describe(config, environ, path))
        return 0
    if problems:
        if args.json:
            print(json.dumps({"config": str(path), "configured": False, "missing": problems}, indent=2))
        else:
            print("trifecta usage: not configured, nothing was fetched\n" + "\n".join(f"  {p}" for p in problems))
        # A missing config is the fresh-clone state; a broken one is an error.
        return 1 if args.sleep_until_reset or path.is_file() else 0

    if args.sleep_until_reset:
        ids = [r["id"] for r in roles(config)]
        if args.sleep_until_reset not in ids:
            parser.error(f"unknown role {args.sleep_until_reset!r}; choose from {', '.join(ids)}")
        readings = read_roles(config, now(), fetch)
        return sleep_until_reset(args.sleep_until_reset, readings, now, sleep)
    readings = read_roles(config, now(), fetch)
    if args.json:
        print(json.dumps({"config": str(path), "configured": True, "stale_minutes": config["stale_minutes"],
                          "roles": readings}, indent=2))
        return 0
    for role_id, r in readings.items():
        name = f"{role_id:<12} {r['label']} ({r['providerInstanceId'] or 'this thread'})"
        if "error" in r:
            print(f"{name}: unknown, {r['error']}")
            continue
        age = f"read {r['age_minutes']} min ago" + (", STALE" if r["stale"] else "")
        status = f"; status {r['status']}" if r.get("status") else ""
        threshold = "; AT THRESHOLD, start nothing large here" if r.get("at_threshold") else ""
        print(f"{name}: 5-hour {pct(r['five_hour'])}; weekly {pct(r['weekly'])}{status}; {age}{threshold}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
