#!/usr/bin/env python3
"""Print the last usage reading for every role in the trifecta config.

Usage gateways record the rate-limit headers of every response that passes
through them. A role only has fresh numbers if a request went through its
source recently, so each reading carries its age and is marked STALE after
stale_minutes. A Codex role can also read native Codex session logs, taking
whichever reading is newer. Read-only: it never signs in, switches accounts
or starts tasks.

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
TAIL_BYTES = 4 << 20
DEFAULT_PAGE_AT_LEFT = 3
FIELD_KEYS = {"five_hour_used", "five_hour_reset", "weekly_used", "weekly_reset",
              "fable_weekly_used", "fable_weekly_reset", "status"}
PAGE_WINDOW_KEYS = ("weekly", "fable_weekly")
WINDOW_LABELS = {"weekly": "weekly", "fable_weekly": "Fable weekly"}
# TRIFECTA_<ROLE>_GATEWAY_URL and _KEY_FILE override the usage source of that role.
ENV_OVERRIDES = [("TRIFECTA_COORDINATOR", "coordinator"), ("TRIFECTA_BUILDER", "builder1"), ("TRIFECTA_REVIEWER", "reviewer1")]
ROLE_KEYS = {
    "coordinator": {"label", "providerInstanceId", "model", "options", "usage", "page_windows"},
    "builder": {"label", "providerInstanceId", "model", "options", "usage", "switch_at_percent", "page_windows"},
    "reviewer": {"label", "providerInstanceId", "model", "options", "usage", "weight", "page_windows"},
}
SOURCE_KEYS = {"type", "url", "key_file", "provider", "used_unit", "fields", "codex_sessions"}
CODEX_LOG_SOURCE_KEYS = {"type", "codex_sessions"}
TOP_KEYS = {"coordinator", "builders", "reviewers", "max_parallel_builders", "never_use",
            "stale_minutes", "page_at_left_percent", "usage_sources"}


def expand_path(raw, environ=None):
    text = str(raw)
    if text.startswith("~/") and environ and environ.get("HOME"):
        return Path(environ["HOME"]) / text[2:]
    return Path(text).expanduser()


def config_path(environ):
    return expand_path(environ["TRIFECTA_CONFIG"], environ) if environ.get("TRIFECTA_CONFIG") else DEFAULT_CONFIG


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
        "page_at_left_percent": raw.get("page_at_left_percent", DEFAULT_PAGE_AT_LEFT),
        "usage_sources": raw.get("usage_sources", {}),
    }
    if not isinstance(config["max_parallel_builders"], int) or isinstance(config["max_parallel_builders"], bool) or config["max_parallel_builders"] < 1:
        errors.append("max_parallel_builders: must be a whole number of at least 1")
    if not isinstance(config["stale_minutes"], (int, float)) or isinstance(config["stale_minutes"], bool) or config["stale_minutes"] <= 0:
        errors.append("stale_minutes: must be a positive number")
    pal = config["page_at_left_percent"]
    if not isinstance(pal, (int, float)) or isinstance(pal, bool) or not 0 <= pal <= 100:
        errors.append("page_at_left_percent: a number from 0 to 100")
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
        kind = source.get("type")
        if kind == "codex-logs":
            unknown_keys(source, CODEX_LOG_SOURCE_KEYS, where, errors)
            source.setdefault("codex_sessions", "~/.codex/sessions")
            if not isinstance(source["codex_sessions"], str) or not source["codex_sessions"]:
                errors.append(f"{where}.codex_sessions: must be a non-empty path")
            continue
        unknown_keys(source, SOURCE_KEYS, where, errors)
        if kind != "cliproxyapi":
            errors.append(f'{where}.type: "cliproxyapi" or "codex-logs"')
            continue
        if not str(source.get("url", "")).startswith(("http://", "https://")):
            errors.append(f"{where}.url: must be an http(s) URL")
        for key in ("key_file", "provider"):
            if not isinstance(source.get(key), str) or not source.get(key):
                errors.append(f"{where}.{key}: required")
        source.setdefault("used_unit", "fraction")
        if source["used_unit"] not in {"fraction", "percent"}:
            errors.append(f'{where}.used_unit: "fraction" or "percent"')
        if "codex_sessions" in source and (not isinstance(source["codex_sessions"], str) or not source["codex_sessions"]):
            errors.append(f"{where}.codex_sessions: must be a non-empty path")
        fields = source.get("fields")
        if not isinstance(fields, dict) or not all(isinstance(v, str) for v in fields.values()):
            errors.append(f"{where}.fields: map of reading fields to header names")
        else:
            unknown_keys(fields, FIELD_KEYS, f"{where}.fields", errors)
            if not {"five_hour_used", "weekly_used", "fable_weekly_used"} & set(fields):
                errors.append(f"{where}.fields: needs five_hour_used, weekly_used or fable_weekly_used")

    def role(obj, kind, where, defaults):
        if not isinstance(obj, dict):
            errors.append(f"{where}: must be an object")
            return None
        unknown_keys(obj, ROLE_KEYS[kind], where, errors)
        entry = {**defaults, "options": {}, "page_windows": [], **obj}
        required = ["label"] if kind == "coordinator" else ["label", "providerInstanceId", "model"]
        for key in required:
            if not isinstance(entry.get(key), str) or not entry.get(key):
                errors.append(f"{where}.{key}: required")
        if not isinstance(entry["options"], dict):
            errors.append(f"{where}.options: must be an object")
        pw = entry["page_windows"]
        if not isinstance(pw, list) or not all(isinstance(x, str) and x in PAGE_WINDOW_KEYS for x in pw):
            errors.append(f"{where}.page_windows: list of weekly window names ({', '.join(PAGE_WINDOW_KEYS)})")
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
        if builder and (not isinstance(percent, (int, float)) or isinstance(percent, bool) or not 0 < percent <= 100):
            errors.append(f"builders[{i}].switch_at_percent: a number from 1 to 100")
    for i, reviewer in enumerate(config["reviewers"]):
        if reviewer is None:
            continue
        reviewer.setdefault("weight", default_weight(reviewer))
        if not isinstance(reviewer["weight"], (int, float)) or isinstance(reviewer["weight"], bool) or reviewer["weight"] <= 0:
            errors.append(f"reviewers[{i}].weight: required, a positive number")
    return config, errors


def apply_env_overrides(config, environ):
    for prefix, role_id in ENV_OVERRIDES:
        name = next((r.get("usage") for r in roles(config) if r["id"] == role_id), None)
        if name and name in config["usage_sources"]:
            source = config["usage_sources"][name]
            if source.get("type") == "cliproxyapi":
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


def request_files(source, fetch, environ=None):
    """(auth files, None), or (None, a problem that is safe to print)."""
    try:
        key = json.loads(expand_path(source["key_file"], environ).read_text())["management_key"]
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


def latest_reading(auth, signal=None):
    """The newest non-empty signal set among the account and per-model views."""
    readings = [auth.get("quota") or {}] + list((auth.get("model_quotas") or {}).values())
    readings = [r for r in readings if r.get("signals") and r.get("observed_at")
                and (signal is None or signal in r["signals"])]
    return max(readings, key=lambda r: dt.datetime.fromisoformat(r["observed_at"]), default=None)


def local_time(moment):
    return moment.astimezone().isoformat(timespec="minutes")


def age_minutes(observed, now):
    return int((now - observed).total_seconds() // 60)


def window(used, reset, now):
    """A used fraction and its reset; a window whose reset has passed is empty."""
    if used is None:
        return None
    if reset and reset <= now:
        return {"used": 0.0, "resets_at": None, "rolled_over": True}
    return {"used": round(used, 2), "resets_at": local_time(reset) if reset else None}


def summarize(source, reading, now, stale_minutes, auth=None):
    """The printed reading, plus (used, reset) for each window as it was read."""
    signals, fields = reading["signals"], source["fields"]
    scale = 100 if source["used_unit"] == "percent" else 1

    def used(sig_map, key):
        value = sig_map.get(fields[key]) if key in fields else None
        return None if value is None else float(value) / scale

    def reset(sig_map, key):
        value = sig_map.get(fields[key]) if key in fields else None
        return dt.datetime.fromtimestamp(int(float(value)), dt.timezone.utc) if value else None

    observed = dt.datetime.fromisoformat(reading["observed_at"])
    out = {"age_minutes": age_minutes(observed, now)}
    windows = []
    for name in ("five_hour", "weekly"):
        raw = (used(signals, name + "_used"), reset(signals, name + "_reset"))
        out[name] = window(*raw, now)
        if raw[0] is not None:
            windows.append(raw)
    if "fable_weekly_used" in fields:
        # A model-specific weekly allowance appears only on responses to that model.
        fable = latest_reading(auth, fields["fable_weekly_used"]) if auth else None
        if fable:
            f_sig = fable["signals"]
            f_win = window(used(f_sig, "fable_weekly_used"), reset(f_sig, "fable_weekly_reset"), now)
            if f_win is not None:
                f_win["age_minutes"] = age_minutes(dt.datetime.fromisoformat(fable["observed_at"]), now)
            out["fable_weekly"] = f_win
        else:
            out["fable_weekly"] = None
    out["status"] = signals.get(fields["status"]) if "status" in fields else None
    out["stale"] = out["age_minutes"] > stale_minutes
    return out, windows


def codex_log_reading(sessions_dir, now, stale_minutes, environ=None):
    """The weekly limit native Codex logged most recently, or None."""
    root = expand_path(sessions_dir, environ)
    if not root.is_dir():
        return None
    files = []
    for days in range(8):
        folder = root / (now.astimezone() - dt.timedelta(days=days)).strftime("%Y/%m/%d")
        if folder.is_dir():
            files += [(p.stat().st_mtime, p) for p in folder.glob("*.jsonl")]
    best = None
    for mtime, path in sorted(files, reverse=True):
        # A file last written before the best reading cannot hold a newer one.
        if best and mtime < best[0].timestamp():
            break
        with open(path, "rb") as stream:
            stream.seek(max(0, path.stat().st_size - TAIL_BYTES))
            lines = stream.read().decode("utf-8", "replace").splitlines()
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                event = json.loads(line)
                limits = event["payload"]["rate_limits"]
                observed = dt.datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            weekly = next((w for w in (limits.get("primary"), limits.get("secondary"))
                           if isinstance(w, dict) and w.get("window_minutes") == 10080), None)
            if weekly:
                if not best or observed > best[0]:
                    best = (observed, weekly)
                break
    if not best:
        return None
    observed, weekly = best
    used_pct = weekly.get("used_percent")
    used_frac = None if used_pct is None else float(used_pct) / 100
    reset_epoch = weekly.get("resets_at")
    reset_dt = dt.datetime.fromtimestamp(int(float(reset_epoch)), dt.timezone.utc) if reset_epoch else None
    minutes = age_minutes(observed, now)
    out = {"age_minutes": minutes, "five_hour": None, "weekly": window(used_frac, reset_dt, now),
           "status": None, "source": "Codex logs", "stale": minutes > stale_minutes}
    windows = [(used_frac, reset_dt)] if used_frac is not None else []
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


def page_due(role, summary, page_at_left):
    """The configured weekly windows of this role that are at or under the paging threshold."""
    due = []
    for key in role.get("page_windows") or ():
        w = summary.get(key)
        if w and not w.get("rolled_over") and round((1 - w["used"]) * 100) <= page_at_left:
            due.append(key)
    return due


def read_roles(config, now, fetch=fetch_auth_files, environ=None):
    result, responses, codex_cache = {}, {}, {}
    for role in roles(config):
        entry = {"kind": role["kind"], "label": role["label"], "providerInstanceId": role.get("providerInstanceId")}
        result[role["id"]] = entry
        if not role.get("usage"):
            entry["error"] = "no usage source configured"
            continue
        name = role["usage"]
        source = config["usage_sources"][name]
        summary, windows, problem, auth, reading = None, [], None, None, None
        if source["type"] == "cliproxyapi":
            target = (source["url"], source["key_file"])
            if target not in responses:  # several roles may share one gateway
                responses[target] = request_files(source, fetch, environ)
            files, problem = responses[target]
            try:
                auth = None if problem else next((f for f in files if f.get("provider") == source["provider"]), None)
                reading = auth and latest_reading(auth)
                if reading:
                    summary, windows = summarize(source, reading, now, config["stale_minutes"], auth=auth)
            except Exception as error:  # parse errors can quote response data; print only the class
                problem = f"unreadable reading ({type(error).__name__})"
        if source.get("codex_sessions"):
            sessions_key = source["codex_sessions"]
            if sessions_key not in codex_cache:
                try:
                    codex_cache[sessions_key] = codex_log_reading(sessions_key, now, config["stale_minutes"], environ)
                except OSError:
                    codex_cache[sessions_key] = None
            logged = codex_cache[sessions_key]
            if logged and (summary is None or logged[0]["age_minutes"] < summary["age_minutes"]):
                summary, windows = dict(logged[0]), list(logged[1])
                problem = None
        if summary is not None:
            # Builders switch at their threshold; any role is blocked once a window runs out.
            blocked, reset = blocking(windows, summary["status"], role.get("switch_at_percent", 100), now)
            summary["blocking_reset"] = local_time(reset) if reset else None
            if role["kind"] == "builder":
                summary["at_threshold"] = blocked
            summary["page_due"] = page_due(role, summary, config["page_at_left_percent"])
            entry.update(summary)
        elif problem:
            entry["error"] = f"{problem} on usage source {name}"
        elif source["type"] == "cliproxyapi" and auth is None:
            entry["error"] = f"no {source['provider']} account on usage source {name}"
        else:
            entry["error"] = "no reading yet"
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
        if role.get("page_windows"):
            rule += f"; page on {', '.join(role['page_windows'])}"
        lines.append(f"{role['id']:<12} {role['label']:<32} {role.get('providerInstanceId') or '-':<26} "
                     f"{role.get('model') or '-':<18} {json.dumps(role['options'], sort_keys=True):<26} "
                     f"{role.get('usage') or '-':<20} {rule}")
    lines += ["", f"max_parallel_builders: {config['max_parallel_builders']}",
              f"stale_minutes: {config['stale_minutes']}",
              f"page_at_left_percent: {config['page_at_left_percent']}",
              f"never_use: {', '.join(config['never_use']) or 'none'}", "", "usage sources:"]
    for name, source in config["usage_sources"].items():
        if source["type"] == "codex-logs":
            lines.append(f"  {name}: codex-logs codex_sessions={source['codex_sessions']}")
            continue
        key_file = expand_path(source["key_file"], environ)
        note = "" if key_file.is_file() else "  (key file not found yet)"
        extra = f" codex_sessions={source['codex_sessions']}" if source.get("codex_sessions") else ""
        lines.append(f"  {name}: {source['type']} {shown_url(source['url'])} provider={source['provider']} "
                     f"unit={source['used_unit']} key_file={source['key_file']}{extra}{note}")
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
        readings = read_roles(config, now(), fetch, environ)
        return sleep_until_reset(args.sleep_until_reset, readings, now, sleep)
    readings = read_roles(config, now(), fetch, environ)
    if args.json:
        print(json.dumps({"config": str(path), "configured": True, "stale_minutes": config["stale_minutes"],
                          "page_at_left_percent": config["page_at_left_percent"], "roles": readings}, indent=2))
        return 0
    for role_id, r in readings.items():
        name = f"{role_id:<12} {r['label']} ({r['providerInstanceId'] or 'this thread'})"
        if "error" in r:
            print(f"{name}: unknown, {r['error']}")
            continue
        fable = r.get("fable_weekly")
        fable_text = f"; Fable weekly {pct(fable)}, read {fable['age_minutes']} min ago" if fable else ""
        age = f"read {r['age_minutes']} min ago" + (f" from {r['source']}" if r.get("source") else "") + (", STALE" if r["stale"] else "")
        status = f"; status {r['status']}" if r.get("status") else ""
        threshold = "; AT THRESHOLD, start nothing large here" if r.get("at_threshold") else ""
        print(f"{name}: 5-hour {pct(r['five_hour'])}; weekly {pct(r['weekly'])}{fable_text}{status}; {age}{threshold}")
    for role_id, r in readings.items():
        for key in r.get("page_due") or ():
            print(f"PAGE: {role_id} ({r['label']}) {WINDOW_LABELS[key]} has {round((1 - r[key]['used']) * 100)}% left ({pct(r[key])})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
