#!/usr/bin/env python3
"""Fleet deployment and task helpers. Python 3.11+, standard library only."""
import argparse
import base64
import contextlib
import csv
import datetime as dt
import difflib
import errno
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request

if os.name == "nt":
    import msvcrt
else:
    import fcntl

ROOT = Path(os.environ.get("FLEET_REPO_ROOT", str(Path(__file__).resolve().parents[1])))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def config_path():
    """FLEET_CONFIG wins. Synced runtimes carry the fleet.json released with them; a machine's own checkout may be older."""
    if os.environ.get("FLEET_CONFIG"):
        return Path(os.environ["FLEET_CONFIG"]).expanduser()
    shipped = Path(__file__).resolve().parents[1] / "fleet.json"
    return shipped if shipped.exists() else ROOT / "fleet.json"


def load_config():
    return read_json(config_path())


def encode(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def expand(path):
    user_home = Path(os.environ.get("FLEET_USER_HOME", str(Path.home())))
    return user_home / path[2:] if path.startswith("~/") else Path(path)


def state_dir(config):
    default = r"D:\AgentWork\fleet-state" if os.name == "nt" else config["state_dir"]
    return expand(os.environ.get("FLEET_STATE_DIR", default))


def atomic_write(path, text, mode=0o600, exclusive=False):
    """Publish a complete file. With exclusive, raise FileExistsError instead of replacing one that exists."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".fleet-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.chmod(temporary, mode)
        # A hard link fails if the name is taken, even by a file another process created a moment ago.
        (os.link if exclusive else os.replace)(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def file_lock(path):
    """Hold an exclusive lock on a file beside the state it guards; other fleet processes wait for it."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), "rb+") as handle:
        if os.name != "nt":
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield  # Closing the file releases the lock.
            return
        # Windows gives up on LK_LOCK after about ten seconds; keep waiting, as flock does.
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                break
            except OSError as error:
                # Only contention is worth waiting out; a drive that refuses locks should fail, not hang.
                if error.errno not in (getattr(errno, "EDEADLOCK", errno.EDEADLK), errno.EACCES):
                    raise
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def allow_windows_user(path):
    """Let the interactive user read private trees created by elevated SSH.

    Python 3.13's mode-0700 Windows ACL grants OWNER RIGHTS. An elevated SSH
    process can create directories owned by Administrators, which excludes the
    same user's ordinary desktop token. Grant that user's SID explicitly.
    """
    if os.name != "nt":
        return
    identity = subprocess.check_output(["whoami.exe", "/user", "/fo", "csv", "/nh"], text=True)
    sid = next(csv.reader(identity.strip().splitlines()))[1]
    if not re.fullmatch(r"S-1-(?:[0-9]+-)*[0-9]+", sid):
        raise ValueError("Could not identify the Windows account SID")
    result = subprocess.run(["icacls.exe", str(path), "/grant:r", f"*{sid}:(OI)(CI)F", "/T", "/C"], capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f"Could not grant the interactive Windows user access to {path}")


def tree_entries(root):
    root = Path(root)
    # Path ordering is case-insensitive on Windows. Sort raw relative parts so
    # the digest stays identical to the original case-sensitive Mac release.
    for path in sorted(root.rglob("*"), key=lambda p: tuple(p.relative_to(root).parts)):
        relative = path.relative_to(root)
        if any(p in {".git", "__pycache__", ".pytest_cache", "node_modules"} for p in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"Source symlink requires review: {path}")
        if path.is_file() and path.name != ".DS_Store":
            yield relative.as_posix(), path


def tree_hash(root):
    h = hashlib.sha256()
    for name, path in tree_entries(root):
        h.update(name.encode() + b"\0" + bytes.fromhex(digest(path)))
    return h.hexdigest()


def profile(config, name):
    p = dict(config["profiles"][name])
    p["home"] = (state_dir(config) / "profiles" / name if os.name == "nt" and p.get("managed") else expand(p.get("windows_home", p["home"]) if os.name == "nt" else p["home"]))
    protected = {expand("~/" + n).resolve() for n in [".codex", ".codex_work", ".claude", ".claude_secondary"]}
    protected |= {expand(v["home"]).resolve() for v in config["profiles"].values() if not v.get("managed")}
    if p.get("managed") and p["home"].resolve() in protected:
        raise ValueError("A managed profile cannot target an existing native account home")
    return p


def this_machine(config):
    """The fleet.json name of this machine: FLEET_MACHINE, else a machine whose hostname (or name) matches."""
    if os.environ.get("FLEET_MACHINE"):
        return os.environ["FLEET_MACHINE"]
    host = socket.gethostname().split(".")[0].lower()
    return next((name for name, m in config.get("machines", {}).items() if m.get("hostname", name).lower() == host), None)


def skill_allowed(config, p, source):
    """Share instructions by capability; credentials and task routing stay separate."""
    if (source / ".claude-only").exists():
        personal = config.get("skill_policy", {}).get("personal_identities", ["personal"])
        return p["driver"] == "claude" and p["identity"] in personal
    return True


def deep_merge(existing, managed):
    result = dict(existing)
    for key, value in managed.items():
        result[key] = deep_merge(result.get(key, {}), value) if isinstance(value, dict) else value
    return result


def merge_toml_top(existing, values):
    """Replace only owned top-level keys; preserve every unrelated table/key."""
    if existing.strip():
        parsed = tomllib.loads(existing)
        if all(parsed.get(k) == v for k, v in values.items()):
            return existing
    lines = existing.splitlines(keepends=True)
    boundary = next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
    seen = set()
    for i in range(boundary):
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", lines[i])
        if match and match.group(1) in values:
            key = match.group(1)
            lines[i] = f"{key} = {json.dumps(values[key])}\n"
            seen.add(key)
    additions = [f"{k} = {json.dumps(v)}\n" for k, v in values.items() if k not in seen]
    if boundary and not lines[boundary - 1].endswith("\n"):
        lines[boundary - 1] += "\n"
    output = "".join(lines[:boundary] + additions + ["\n"] + lines[boundary:])
    tomllib.loads(output)
    return output


def release(config):
    state = state_dir(config)
    sources = {p.name: p for p in (ROOT / "skills").iterdir() if p.is_dir() and (p / "SKILL.md").exists()}
    lock = ROOT / "skills/external.lock.json"
    for entry in read_json(lock).get("skills", []) if lock.exists() else []:
        src = ROOT / entry["path"] if "path" in entry else expand(entry["local_path"])
        if not src.exists():
            raise ValueError(f"External skill missing: {entry['name']}")
        if tree_hash(src) != entry["sha256"]:
            raise ValueError(f"External skill changed: {entry['name']}. Review and update its lock before releasing.")
        if entry["name"] in sources:
            raise ValueError(f"Duplicate skill name: {entry['name']}")
        sources[entry["name"]] = src
    hashes = {name: tree_hash(path) for name, path in sorted(sources.items())}
    revision = hashlib.sha256(encode(hashes).encode()).hexdigest()[:20]
    target = state / "releases" / revision
    if target.exists():
        verify_release(target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=target.parent))
        try:
            for name, src in sources.items():
                for relative, path in tree_entries(src):
                    dest = staging / "skills" / name / relative
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, dest)
            atomic_write(staging / "manifest.json", encode({"revision": revision, "skills": hashes}))
            verify_release(staging)
            for path in staging.rglob("*"):
                if path.is_file():
                    path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
            for path in sorted(staging.rglob("*"), reverse=True):
                if path.is_dir():
                    path.chmod(0o555)
            os.rename(staging, target)
            target.chmod(0o555)
        except BaseException:
            # Only our uncommitted staging directory is eligible for cleanup.
            for path in staging.rglob("*"):
                path.chmod(0o700 if path.is_dir() else 0o600)
            staging.chmod(0o700)
            shutil.rmtree(staging)
            raise
    allow_windows_user(target)
    atomic_write(state / "latest-release.json", encode({"revision": revision}))
    return target


def verify_release(path):
    manifest = read_json(path / "manifest.json")
    for name, expected in manifest["skills"].items():
        if tree_hash(path / "skills" / name) != expected:
            raise ValueError(f"Release content differs from its manifest: {name}")
    return manifest


def gateway_config(config, name="personal"):
    return config["gateway"] if name == "personal" else config["additional_gateways"][name]


def gateway_keys(config, name="personal"):
    client = state_dir(config) / "gateway-keys" / (name + ".json")
    path = client if client.exists() or os.name == "nt" else expand(gateway_config(config, name)["runtime_dir"]) / "private/client.json"
    return read_json(path)


def render_profile(config, name, revision):
    p = profile(config, name)
    if not p.get("managed"):
        raise ValueError(f"{name} is an existing native profile and is a read-only deployment target")
    home = p["home"]
    gateway_name = p.get("gateway", "personal")
    key = gateway_keys(config, gateway_name)["api_key"]
    url = gateway_config(config, gateway_name)["url"]
    files = {}
    if p["driver"] == "codex":
        path = home / "config.toml"
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        files[path] = merge_toml_top(old, {"model": p["model"], "model_reasoning_effort": p["effort"], "openai_base_url": url + "/v1", "cli_auth_credentials_store": "file"})
        servers = {"openaiDeveloperDocs": {"url": "https://developers.openai.com/mcp"}}
        blender = expand("~/.local/bin/blender-mcp")
        if os.name != "nt" and blender.exists():
            servers["blender"] = {"command": str(blender), "startup_timeout_sec": 90, "tool_timeout_sec": 600}
        parsed = tomllib.loads(files[path])
        for server, settings in servers.items():
            if server not in parsed.get("mcp_servers", {}):
                files[path] += f"\n[mcp_servers.{server}]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in settings.items())
        files[home / "auth.json"] = encode({"OPENAI_API_KEY": key})
        files[home / "AGENTS.md"] = (ROOT / "AGENTS.md").read_text(encoding="utf-8") + "\nThis is the isolated personal Codex gateway profile. Native ChatGPT features may require the personal-codex profile.\n"
    else:
        path = home / "settings.json"
        old = read_json(path) if path.exists() else {}
        preferences = ROOT / "profiles/claude-common.json"
        managed = read_json(preferences) if preferences.exists() else {}
        personal_preferences = ROOT / "profiles/claude-personal.json"
        if p["identity"] == "personal" and personal_preferences.exists():
            managed = deep_merge(managed, read_json(personal_preferences))
            # Language-server plugins are machine-local; Windows has no Swift toolchain.
            if os.name == "nt":
                managed.get("enabledPlugins", {}).pop("swift-lsp@claude-plugins-official", None)
        managed = deep_merge(managed, {"model": p["model"], "effortLevel": p["effort"], "env": {"ANTHROPIC_BASE_URL": url, "ANTHROPIC_AUTH_TOKEN": key}})
        if p["identity"] == "personal":
            managed["env"]["CODEX_HOME"] = str(expand(config["profiles"]["personal-codex"]["home"]))
        files[path] = encode(deep_merge(old, managed))
        details = "Claude-to-Codex image/3D/computer-use bridges use the native personal Codex home." if p["identity"] == "personal" else "Do not send university task content to a personal or employer account. Cross-provider bridge skills are excluded from this profile."
        files[home / "CLAUDE.md"] = (ROOT / "AGENTS.md").read_text(encoding="utf-8") + f"\nThis is the isolated {p['identity']} Claude gateway profile. {details}\n"
    manifest = verify_release(revision)
    links = {}
    for skill in manifest["skills"]:
        source = revision / "skills" / skill
        if not skill_allowed(config, p, source):
            continue
        links[home / "skills" / skill] = source
    # Codex installs and upgrades its own built-in skills on every platform.
    # They are not part of a fleet release, including on native macOS clients.
    files[home / ".fleet-profile.json"] = encode({"name": name, "revision": revision.name, "identity": p["identity"], "driver": p["driver"]})
    return p, files, links


def link_matches(path, source):
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        return path.resolve() == source.resolve()
    if os.name == "nt" and path.is_dir():
        return tree_hash(path) == tree_hash(source)
    return False


def install_skill_link(path, source):
    """Use one canonical skill path, including for Windows Codex discovery."""
    if os.name == "nt":
        # Directory junctions work without Developer Mode or symlink privilege.
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(path), str(source)], capture_output=True, text=True)
        if result.returncode:
            raise ValueError(f"Could not create skill junction: {path}")
    else:
        path.symlink_to(source, target_is_directory=True)


def profile_plan(config, name, revision):
    p, files, links = render_profile(config, name, revision)
    changes = {path: text for path, text in files.items() if not path.exists() or path.read_text(encoding="utf-8") != text}
    changed_links = {path: source for path, source in links.items() if not link_matches(path, source)}
    for path in changed_links:
        if path.exists() or path.is_symlink():
            raise ValueError(f"Existing skill is preserved; use a new profile: {path}")
    for path in changes:
        if path.is_symlink():
            raise ValueError(f"Refusing to replace a symlinked profile file: {path}")
    if changes or changed_links:
        for folder in ["sessions", "projects"]:
            path = p["home"] / folder
            if path.exists() and any(path.iterdir()):
                raise ValueError(f"{name} has session history. Create a new profile instead of updating it in place.")
    return p, changes, changed_links


def deploy_profile(config, name, revision, apply=False):
    p, changes, links = profile_plan(config, name, revision)
    if not apply:
        key = gateway_keys(config, p.get("gateway", "personal"))["api_key"]
        for path, text in changes.items():
            old = path.read_text(encoding="utf-8") if path.exists() else ""
            print("".join(difflib.unified_diff(old.replace(key, "<gateway-key>").splitlines(True), text.replace(key, "<gateway-key>").splitlines(True), fromfile=str(path), tofile=str(path))), end="")
        for path, source in links.items():
            print(f"LINK {path} -> {source}")
        return len(changes) + len(links)
    p["home"].mkdir(mode=0o700, parents=True, exist_ok=True)
    p["home"].chmod(0o700)
    backup = state_dir(config) / "backups" / dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f") / name
    for path, text in changes.items():
        if path.exists():
            destination = backup / path.relative_to(p["home"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        atomic_write(path, text)
    for path, source in links.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        install_skill_link(path, source)
    allow_windows_user(p["home"])
    print(f"{name}: {len(changes)} files, {len(links)} skills installed; home {p['home']}")
    return len(changes) + len(links)


def http_json(url, key=None, method="GET", data=None, timeout=15):
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    payload = None if data is None else json.dumps(data).encode()
    if payload:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def t3_ssh_owned(t3_home):
    """SSH launch states that own the live T3 server. T3's desktop client stops a
    server it marked "managed" whenever its tunnel goes stale, ending every session."""
    runtime = t3_home / "userdata" / "server-runtime.json"
    if not runtime.exists():
        return []
    pid = str(read_json(runtime).get("pid", ""))
    owned = []
    for state in sorted((t3_home / "ssh-launch").glob("*")):
        managed, pid_file = state / "managed", state / "pid"
        if managed.is_file() and managed.read_text().strip() == "managed" and pid_file.is_file() and pid_file.read_text().strip() == pid:
            owned.append(state.name)
    return owned


def doctor(config, selected=None):
    failures = []
    baseline = state_dir(config) / "baseline.json"
    if baseline.exists():
        data = read_json(baseline)
        changed = [p for p, expected in data["files"].items() if not Path(p).is_file() or digest(p) != expected]
        failures += ["Original profile changed: " + p for p in changed]
        print(f"Original profiles: {len(data['files']) - len(changed)}/{len(data['files'])} baseline hashes match")
        runtime = expand("~/.t3/userdata/server-runtime.json")
        same = runtime.exists() and all(read_json(runtime).get(k) == data["t3_runtime"].get(k) for k in ["pid", "startedAt"])
        try:
            current = read_json(runtime)
            port = current.get("port", 3773)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
                if response.status != 200:
                    raise ValueError("T3 did not return a healthy response")
            print("T3 runtime:", "same server" if same else "reachable; restarted since the initial audit")
        except (OSError, ValueError) as error:
            failures.append(f"T3 is unavailable: {error}")
    for key in t3_ssh_owned(expand("~/.t3")):
        failures.append(
            f"T3 server is owned by a remote SSH client (state {key}), which stops it on reconnect. "
            f"Mark it external: printf 'external\\n' > ~/.t3/ssh-launch/{key}/managed && rm ~/.t3/ssh-launch/{key}/pid"
        )
    if (state_dir(config) / "latest-release.json").exists():
        # Links follow the latest release after `fleet sync`, so check them against it, not each profile's first release.
        from skills_sync import status as skills_status
        skills_report = skills_status(config, selected)
        failures.extend(skills_report["problems"])
        print(f"Shared skills: {skills_report['revision']}, {skills_report['profiles']} installed profiles, {len(skills_report['problems'])} pending")
        worker_status = state_dir(config) / "fleet-sync-status.json"
        if worker_status.exists():
            worker = read_json(worker_status)
            stale = config.get("sync", {}).get("stale_after_minutes", 10)
            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(worker["checked_at"])
            if age.total_seconds() > stale * 60:
                failures.append(f"Automatic skill synchronization has not checked in for over {stale} minutes")
            for machine, result in worker["machines"].items():
                if "error" in result:
                    failures.append(f"Skill synchronization failed on {machine}: {result['error']}")
                elif result["revision"] != skills_report["revision"]:
                    failures.append(f"{machine}: outdated skill release {result['revision']}")
    for name, raw in config["profiles"].items():
        if not raw.get("managed"):
            continue
        if selected and name not in selected:
            continue
        p = profile(config, name)
        record = p["home"] / ".fleet-profile.json"
        if not record.exists():
            failures.append(f"{name} has not been installed")
            continue
        revision = state_dir(config) / "releases" / read_json(record)["revision"]
        verify_release(revision)
        print(f"{name}: profile created from {revision.name}, identity {p['identity']}")
    deployed = state_dir(config) / "skills-deployment.json"
    if deployed.exists():
        record = read_json(deployed)
        revision = state_dir(config) / "releases" / record["release"]
        verify_release(revision)
        # Once sync owns native homes, their links follow newer releases and the sync status above covers them.
        if not config.get("sync", {}).get("include_native"):
            for home in record["homes"]:
                for path in (Path(home) / "skills").iterdir():
                    if path.name in read_json(revision / "manifest.json")["skills"] and not link_matches(path, revision / "skills" / path.name):
                        failures.append(f"Migrated skill differs: {path}")
            for destination, source in record.get("entries", {}).items():
                path = Path(destination)
                if (source and not link_matches(path, Path(source))) or (source is None and (path.exists() or path.is_symlink())):
                    failures.append(f"Migrated skill is missing or changed: {path}")
        print(f"Shared skill migration: {len(record['homes'])} homes, release {revision.name}")
    gateways = {**({"personal": config["gateway"]} if "gateway" in config else {}), **config.get("additional_gateways", {})}
    selected_gateways = {config["profiles"][n].get("gateway", "personal") for n in selected} if selected else set(gateways)
    checked_keys = {}
    for name, gateway in gateways.items():
        if name not in selected_gateways:
            continue
        try:
            keys = gateway_keys(config, name)
            checked_keys[name] = keys["api_key"]
            models = http_json(gateway["url"] + "/v1/models", keys["api_key"])
            counts = {}
            if keys.get("management_key"):
                active_config = http_json(gateway["url"] + "/v0/management/config", keys["management_key"])
                routing = active_config.get("routing", {})
                expected = {"strategy": "fill-first", "session-affinity": True, "session-affinity-ttl": "24h", "session-affinity-subagents": True}
                if any(routing.get(k) != v for k, v in expected.items()):
                    failures.append(f"{name}: session affinity settings differ from fleet configuration")
                auth = http_json(gateway["url"] + "/v0/management/auth-files", keys["management_key"])
                for entry in auth.get("files", []):
                    kind = entry.get("provider", entry.get("type", "unknown"))
                    if not entry.get("disabled", False):
                        counts[kind] = counts.get(kind, 0) + 1
                        expected_email = gateway.get("expected_email")
                        if expected_email and entry.get("email") != expected_email:
                            failures.append(f"{name}: account email does not match its identity")
                for kind in gateway.get("providers", ["claude", "codex"]):
                    if counts.get(kind, 0) != 1:
                        failures.append(f"{name}: exactly one enabled {kind} account is required")
                unexpected = set(counts) - set(gateway.get("providers", ["claude", "codex"]))
                if unexpected:
                    failures.append(f"{name}: unexpected account providers {unexpected}")
            print(f"Gateway {name}: reachable; {len(models.get('data', []))} models; account counts {counts or 'checked on gateway host'}")
            try:
                http_json(gateway["url"] + "/v1/models")
                failures.append(f"{name}: gateway allowed a request without its client key")
            except urllib.error.HTTPError as error:
                if error.code != 401:
                    failures.append(f"{name}: unexpected unauthenticated status {error.code}")
        except (OSError, ValueError, urllib.error.URLError) as error:
            failures.append(f"Gateway {name} check failed: {error}")
    for name, gateway in gateways.items():
        if name not in checked_keys:
            continue
        for other, wrong_key in checked_keys.items():
            if other == name:
                continue
            try:
                http_json(gateway["url"] + "/v1/models", wrong_key)
                failures.append(f"{name}: accepted the {other} identity's client key")
            except urllib.error.HTTPError as error:
                if error.code != 401:
                    failures.append(f"{name}: unexpected cross-identity status {error.code}")
            except urllib.error.URLError as error:
                failures.append(f"{name}: identity isolation check failed: {error}")
    for failure in failures:
        print("PENDING:", failure)
    return 1 if failures else 0


def make_task(config, repo, slug, selected, done):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug):
        raise ValueError("Use a lowercase task slug with letters, digits, and hyphens")
    profile(config, selected)
    repo = Path(repo).resolve()
    origin = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    identifier = hashlib.sha256(str(repo).encode()).hexdigest()[:8]
    path = state_dir(config) / "worktrees" / f"{repo.name}-{identifier}" / slug
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", "fleet/" + slug, str(path), head], check=True)
    record = {"task": slug, "machine": socket.gethostname(), "profile": selected, "origin": origin, "base": head, "worktree": str(path), "done": done, "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    atomic_write(state_dir(config) / "tasks" / f"{identifier}-{slug}.json", encode(record))
    print(encode(record))


@contextlib.contextmanager
def lease(config, resource):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", resource):
        raise ValueError("Invalid resource name")
    lock = state_dir(config) / "leases" / resource
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
    except FileExistsError:
        raise ValueError(f"Resource already reserved: {lock}. Inspect its owner before removing a stale lease.")
    try:
        atomic_write(lock / "owner.json", encode({"pid": os.getpid(), "host": socket.gethostname(), "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}))
        yield
    finally:
        (lock / "owner.json").unlink(missing_ok=True)
        lock.rmdir()


def run_profile(config, name, arguments):
    p = profile(config, name)
    env = dict(os.environ)
    env["CODEX_HOME" if p["driver"] == "codex" else "CLAUDE_CONFIG_DIR"] = str(p["home"])
    if p.get("managed") and p["driver"] == "claude" and p["identity"] == "personal":
        env["CODEX_HOME"] = str(expand(config["profiles"]["personal-codex"]["home"]))
    runtime = state_dir(config) / "toolchain-runtime.json"
    if not runtime.exists():
        runtime = state_dir(config) / "windows-runtime.json"
    binary = read_json(runtime)[p["driver"]]["binary"] if p.get("managed") and runtime.exists() else shutil.which(p["driver"])
    if not binary and p["driver"] == "claude":
        binary = str(expand("~/.local/bin/claude"))
    if not binary:
        raise ValueError(f"{p['driver']} CLI is not installed")
    return subprocess.call([binary] + arguments, env=env)


def relay(config, host, command, payload, what):
    """Run a fleet command on another machine with the request on stdin, so values never reach a command line."""
    machine = config["machines"][host]
    if machine.get("os") == "windows":
        remote = machine.get("fleet_command") or machine.get("work_root", "D:\\AgentWork") + "\\bin\\fleet.cmd"
    else:
        path = machine.get("fleet_command") or machine.get("repo", "~/fleet").rstrip("/") + "/scripts/fleet"
        # The remote shell expands a leading ~/; quote the rest.
        remote = "~/" + shlex.quote(path[2:]) if path.startswith("~/") else shlex.quote(path)
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", machine["ssh"], remote, *command, "--relay"]
    try:
        return subprocess.run(argv, input=payload, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        # A Tailscale SSH browser check waits silently for a login nobody is there to give.
        raise ValueError(f"{host} did not answer the {what} within 60 seconds. If SSH into {host} requires a browser check, "
                         "its policy must accept SSH from this machine; report that to the owner.") from None


PAGE_SENT = "Page sent."
PAGE_TYPES = ("discord-webhook", "slack-webhook", "ntfy", "openclaw", "command")


def notify_settings(config):
    if not config.get("notify"):
        raise ValueError("Paging is not configured. Add a \"notify\" section to fleet.json (see docs/fleet-cli.md).")
    return {"max_chars": 500, "max_sentences": 3, "interval_minutes": 30, "channels": [], **config["notify"]}


def page_text(config, text):
    settings = notify_settings(config)
    limit, most = settings["max_chars"], settings["max_sentences"]
    text = " ".join(text.split())
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s]
    if not 1 <= len(sentences) <= most or len(text) > limit:
        raise ValueError(f"A page is one to {most} sentences and at most {limit} characters; this has {len(sentences)} sentences and {len(text)} characters")
    return text


def page_sender(config):
    """The paging agent's own session keys its limit, so one agent never uses up another's page."""
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        agent, home = os.environ["CLAUDE_CODE_SESSION_ID"], os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")
    elif os.environ.get("CODEX_THREAD_ID"):
        agent, home = os.environ["CODEX_THREAD_ID"], os.environ.get("CODEX_HOME", "~/.codex")
    else:
        agent, home = str(Path.cwd().resolve()), None
    homes = {profile(config, name)["home"].resolve(): name for name in config["profiles"]}
    return {"agent": agent, "machine": this_machine(config) or socket.gethostname().split(".")[0].lower(),
            "profile": homes.get(expand(home).resolve(), "unknown profile") if home else "unknown profile", "folder": Path.cwd().name}


def http_post(url, data, headers, timeout=30):
    """The only network call pages make; tests replace it."""
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status


def resolve_ref(config, ref):
    """A channel's URL, topic or token, from the vault or the environment. fleet.json holds only the reference."""
    if "env" in ref:
        if not os.environ.get(ref["env"]):
            raise ValueError(f"The environment variable {ref['env']} is not set")
        return os.environ[ref["env"]]
    scope, name = ref["secret"].split("/", 1)
    return secret(config, {"action": "get", "scope": scope, "name": name})["value"]


def describe_ref(ref):
    return f"env {ref['env']}" if "env" in ref else f"vault secret {ref['secret']}"


def page_request(config, channel, title, text, resolve=True):
    """What a channel sends. With resolve=False, references stay unread so a dry run shows where values come from."""
    value = (lambda ref: resolve_ref(config, ref)) if resolve else (lambda ref: "<" + describe_ref(ref) + ">")
    kind = channel["type"]
    if kind == "discord-webhook":
        # Agent text must never ping a role or @everyone.
        body = {"content": f"**{title}**\n{text}", "allowed_mentions": {"parse": []}}
        return {"url": value(channel["url"]), "headers": {"Content-Type": "application/json"}, "body": json.dumps(body, ensure_ascii=False).encode()}
    if kind == "slack-webhook":
        return {"url": value(channel["url"]), "headers": {"Content-Type": "application/json"}, "body": json.dumps({"text": f"*{title}*\n{text}"}, ensure_ascii=False).encode()}
    if kind == "ntfy":
        server = channel.get("server", "https://ntfy.sh").rstrip("/")
        # HTTP headers travel as Latin-1, so a title that is not plain ASCII goes as an RFC 2047 word, which ntfy decodes.
        shown = title if title.isascii() else "=?UTF-8?B?" + base64.b64encode(title.encode()).decode() + "?="
        headers = {"Title": shown, "Content-Type": "text/plain; charset=utf-8"}
        if channel.get("priority"):
            headers["Priority"] = channel["priority"]
        if channel.get("token"):
            headers["Authorization"] = "Bearer " + value(channel["token"])
        return {"url": server + "/" + value(channel["topic"]), "headers": headers, "body": text.encode()}
    if kind == "openclaw":
        binary = channel.get("binary") or shutil.which("openclaw") or str(expand("~/.local/bin/openclaw"))
        argv = [binary, "message", "send", "--channel", channel["channel"], "--target", channel["target"]]
        if channel.get("account"):
            argv += ["--account", channel["account"]]
        return {"argv": argv + ["--message", f"{title}\n{text}"]}
    if kind == "command":
        return {"argv": [str(expand(channel["argv"][0]))] + channel["argv"][1:], "stdin": f"{title}\n{text}\n"}
    raise ValueError(f"Unknown notify channel type {kind!r}; use one of {', '.join(PAGE_TYPES)}")


def send_page(config, channel, title, text, http, run):
    request = page_request(config, channel, title, text)
    # A sender's error or output can quote the URL, token or environment it was given, so only its class or status is kept.
    try:
        if "url" in request:
            status = http(request["url"], request["body"], request["headers"])
        else:
            status = run(request["argv"], input=request.get("stdin"), capture_output=True, text=True, timeout=60).returncode
    except urllib.error.HTTPError as error:
        raise ValueError(f"HTTP {error.code}") from None
    except Exception as error:
        raise ValueError(type(error).__name__) from None
    if "url" in request and not 200 <= status < 300:
        raise ValueError(f"HTTP {status}")
    if "argv" in request and status:
        raise ValueError(f"exit status {status}")


def page_log(pages, entry):
    pages.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(os.open(pages / "log.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def deliver_page(config, sender, text, dry_run=False, http=None, run=None):
    """Try each channel in order until one accepts the page. Only a sent page counts against the agent's limit."""
    settings = notify_settings(config)
    text = page_text(config, text)
    if not settings["channels"]:
        raise ValueError("fleet.json notify has no channels")
    title = f"{sender['machine']} · {sender['profile']} · {sender['folder']}"
    if dry_run:
        for channel in settings["channels"]:
            request = page_request(config, channel, title, text, resolve=False)
            shown = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in request.items()}
            print(encode({"channel": channel.get("name", channel["type"]), "type": channel["type"], **shown}), end="")
        print("Dry run: nothing was sent and the page limit is unchanged.")
        return
    pages = state_dir(config) / "pages"
    record = pages / (hashlib.sha256(sender["agent"].encode()).hexdigest()[:16] + ".json")
    interval = settings["interval_minutes"]
    # The lock spans check, send and record, so two pages from one agent at once still send only one.
    with file_lock(record.with_suffix(".lock")):
        now = dt.datetime.now(dt.timezone.utc)
        log = {"at": now.isoformat(), **sender, "text": text}
        if record.exists():
            wait = dt.datetime.fromisoformat(read_json(record)["sent_at"]) + dt.timedelta(minutes=interval) - now
            if wait.total_seconds() > 0:
                page_log(pages, {**log, "status": "refused"})
                raise ValueError(f"This agent already paged the owner in the last {interval} minutes. Put the blocker in your report instead.")
        failures = []
        for channel in settings["channels"]:
            name = channel.get("name", channel["type"])
            try:
                send_page(config, channel, title, text, http or http_post, run or subprocess.run)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                failures.append(f"{name}: {error}")
                continue
            atomic_write(record, encode({**sender, "sent_at": now.isoformat()}))
            page_log(pages, {**log, "status": "sent", "channel": name})
            print(PAGE_SENT)
            return
        page_log(pages, {**log, "status": "failed", "errors": failures})
        raise ValueError("No notify channel accepted the page: " + "; ".join(failures))


def page(config, text=None, dry_run=False):
    """Send from the notify host; other machines relay the page to it over SSH."""
    if text is None:
        payload = json.loads(sys.stdin.read())
        return deliver_page(config, payload["sender"], payload["text"])
    sender = page_sender(config)
    host = notify_settings(config)["host"]
    if dry_run or this_machine(config) == host:
        return deliver_page(config, sender, text, dry_run=dry_run)
    page_text(config, text)
    result = relay(config, host, ["page"], encode({"sender": sender, "text": text}), "page")
    # Tailscale SSH can report exit status 0 even when the command fails, so only the confirmation counts.
    if PAGE_SENT not in result.stdout:
        raise ValueError(result.stderr.strip().removeprefix("fleet: ") or f"{host} did not confirm the page")
    print(PAGE_SENT)


SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
SECRET_LIMIT = 64 * 1024


def secret_scopes(config):
    scopes = config.get("secrets", {}).get("scopes")
    return sorted(scopes) if scopes else sorted({p["identity"] for p in config["profiles"].values()})


def secret_path(config, scope, name):
    scopes = secret_scopes(config)
    if scope not in scopes:
        raise ValueError(f"The scope must be one of: {', '.join(scopes)}")
    if not SECRET_NAME.fullmatch(name or ""):
        raise ValueError("A secret name is an environment variable name, such as DISCORD_EXAMPLE_BOT_TOKEN")
    return state_dir(config) / "secrets" / scope / (name + ".json")


def secret_local(config, request):
    """Read or write the vault on this machine. Listings never include values."""
    root = state_dir(config) / "secrets"
    if request["action"] == "list":
        records = [read_json(path) for path in sorted(root.glob("*/*.json")) if request.get("scope") in (None, path.parent.name)]
        return {"secrets": [{k: v for k, v in record.items() if k != "value"} for record in records]}
    scope, name = request["scope"], request["name"]
    path = secret_path(config, scope, name)
    missing = f"No {scope} secret named {name}. Run `fleet secret list` to see what is stored."
    if request["action"] == "get":
        if not path.exists():
            raise ValueError(missing)
        return {"value": read_json(path)["value"]}
    if request["action"] == "set":
        value = request["value"].rstrip("\r\n")
        if not value or len(value.encode()) > SECRET_LIMIT:
            raise ValueError(f"Pipe the value on stdin; it must be non-empty and at most {SECRET_LIMIT // 1024} KB")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # One writer per secret at a time, so the overwrite guard and the kept copy always see the value being replaced.
    with file_lock(root / ".locks" / scope / (name + ".lock")):
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        exists = path.exists()
        if request["action"] == "remove":
            if not exists:
                raise ValueError(missing)
            # Removal keeps a private copy, like a replacement, in case the wrong key was removed.
            atomic_write(root / scope / "removed" / f"{name}-{stamp}.json", path.read_text(encoding="utf-8"))
            path.unlink()
            return {"saved": f"Removed {scope} {name}; a copy is kept in the vault's removed/ folder."}
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        sender = request["sender"]
        record = {"scope": scope, "name": name, "note": request.get("note"), "created_at": now, "updated_at": now,
                  "updated_by": f"{sender['machine']} · {sender['profile']} · {sender['folder']}", "value": value}
        taken = f"{scope} {name} already exists. Pass --replace only to rotate it; the vault keeps the old value."
        if exists:
            if not request.get("replace"):
                raise ValueError(taken)
            old = read_json(path)
            record["created_at"], record["note"] = old["created_at"], record["note"] or old["note"]
            atomic_write(root / scope / "replaced" / f"{name}-{stamp}.json", encode(old))
        elif not record["note"]:
            raise ValueError("Describe a new secret with --note: what it is for and which account or app issued it")
        (root / scope).mkdir(mode=0o700, exist_ok=True)
        try:
            # A first write never replaces a file, even one written by a process that did not take the lock.
            atomic_write(path, encode(record), exclusive=not exists)
        except FileExistsError:
            raise ValueError(taken) from None
    return {"saved": f"Saved {scope} {name}."}


def secret(config, request):
    """Keep one vault on its host; other machines reach it over SSH, the same way pages do."""
    if not config.get("secrets"):
        raise ValueError("The vault is not configured. Add a \"secrets\" section with a host to fleet.json (see docs/fleet-cli.md).")
    host = config["secrets"]["host"]
    if this_machine(config) == host:
        return secret_local(config, request)
    result = relay(config, host, ["secret"], encode(request), "vault request")
    # Tailscale SSH can report exit status 0 even when the command fails, so only a JSON reply counts.
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ValueError(result.stderr.strip().removeprefix("fleet: ") or f"{host} did not answer the vault request") from None


CLAUDE_DEBUG_PROMPT = "Answer in one short sentence."


def claude_debug_entry(config, name):
    homes = config.get("debug", {}).get("claude", {})
    if not homes:
        raise ValueError("claude-debug is not configured. Add debug.claude entries with each direct home and its account email to fleet.json (see docs/fleet-cli.md).")
    if name not in homes:
        raise ValueError(f"No debug.claude entry named {name}; configured: {', '.join(sorted(homes))}")
    return homes[name]


def claude_debug_home(config, name):
    return expand(claude_debug_entry(config, name)["home"])


def claude_debug_account_file(config, name):
    home = claude_debug_home(config, name)
    return expand("~/.claude.json") if home == expand("~/.claude") else home / ".claude.json"


def claude_debug_run(config, name, arguments, **options):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE"))}
    # The caller's gateway route and session variables must not reach a direct request.
    env.update(options.pop("env", {}))
    home = claude_debug_home(config, name)
    # Match how T3 and a terminal address the login, since macOS keys it on this string: no CLAUDE_CONFIG_DIR for the
    # default ~/.claude, otherwise the expanded path without resolving symlinks.
    if home != expand("~/.claude"):
        env["CLAUDE_CONFIG_DIR"] = str(home)
    binary = shutil.which("claude", path=env.get("PATH")) or str(expand("~/.local/bin/claude"))
    return subprocess.run([binary] + arguments, env=env, **options)


def claude_debug_status(config, name):
    account = claude_debug_account_file(config, name)
    expected = claude_debug_entry(config, name)["email"]
    signed_out = f"The direct {name} Claude login is signed out. The owner signs it in from a terminal with `fleet claude-debug login {name}`."
    if not account.exists():
        raise ValueError(signed_out)
    result = claude_debug_run(config, name, ["auth", "status", "--json"], capture_output=True, text=True, timeout=60)
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError:
        status = None
    if not isinstance(status, dict) or not status.get("loggedIn"):
        raise ValueError(signed_out)
    # Claude Code answers from the account it recorded at sign-in; nothing here confirms it with the server.
    email = status.get("email")
    if not isinstance(email, str) or not email:
        raise ValueError(f"The direct {name} Claude login did not report which account it is, so it cannot be checked against {expected}. Nothing was sent.")
    if email.lower() != expected.lower():
        raise ValueError(f"The direct {name} Claude login is {email}, not {expected}. Nothing was sent.")
    return f"The direct {name} Claude login is signed in as {email}, without the gateway."


def claude_debug_login(config, name):
    # Print the sign-in URL instead of opening a browser on a headless machine.
    stubs = state_dir(config) / "claude-debug" / "login-bin"
    for stub in ["open", "xdg-open"]:
        atomic_write(stubs / stub, "#!/bin/sh\nexit 0\n", 0o700)
    entry = claude_debug_entry(config, name)
    args = ["auth", "login", "--" + entry.get("login", "claudeai"), "--email", entry["email"]]
    if entry.get("sso"):
        args.append("--sso")
    env = {"BROWSER": "/usr/bin/true", "PATH": str(stubs) + os.pathsep + os.environ.get("PATH", "")}
    claude_debug_run(config, name, args, env=env, check=True)
    return claude_debug_status(config, name)


def claude_debug_ask(config, name, prompt, reason, model, effort=None, fast=False, max_usd=0.25):
    if not (reason or "").strip():
        raise ValueError("Say with --reason what the owner asked you to check")
    claude_debug_status(config, name)
    # An empty folder with only project settings leaves out the home's CLAUDE.md, skills, plugins and hooks.
    args = ["-p", "--output-format", "json", "--system-prompt", CLAUDE_DEBUG_PROMPT, "--setting-sources", "project", "--tools", "",
            "--strict-mcp-config", "--no-session-persistence", "--max-budget-usd", str(max_usd), "--model", model]
    if effort:
        args += ["--effort", effort]
    if fast:
        args += ["--settings", json.dumps({"fastMode": True})]
    with tempfile.TemporaryDirectory(prefix="fleet-claude-debug-") as cwd:
        result = claude_debug_run(config, name, args + [prompt], cwd=cwd, capture_output=True, text=True, timeout=300)
    try:
        cost = json.loads(result.stdout).get("total_cost_usd")
    except json.JSONDecodeError:
        cost = None
    record = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "home": name, "reason": reason,
              "model": model, "effort": effort, "fast": fast, "max_usd": max_usd, "exit": result.returncode, "cost_usd": cost,
              "sender": page_sender(config)}
    log = state_dir(config) / "claude-debug" / "requests.jsonl"
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    doctor_parser = sub.add_parser("doctor")
    doctor_parser.add_argument("--profile", action="append", help="Check only these installed profiles and their gateways")
    sub.add_parser("release")
    sub.add_parser("profiles")
    command = sub.add_parser("sync", help="Preview or apply fleet-owned skill updates without changing accounts")
    command.add_argument("--apply", action="store_true")
    command.add_argument("--check", action="store_true")
    command.add_argument("--profile", action="append")
    command.add_argument("--release")
    for action in ["diff", "apply"]:
        command = sub.add_parser(action)
        command.add_argument("--profile", required=True)
        command.add_argument("--release")
    command = sub.add_parser("run")
    command.add_argument("--profile", required=True)
    command.add_argument("args", nargs=argparse.REMAINDER)
    command = sub.add_parser("task")
    command.add_argument("repo")
    command.add_argument("slug")
    command.add_argument("--profile", required=True)
    command.add_argument("--done", required=True)
    command = sub.add_parser("lease")
    command.add_argument("resource")
    command.add_argument("args", nargs=argparse.REMAINDER)
    command = sub.add_parser("page", help="Notify the owner about a blocker only they can clear")
    command.add_argument("text", nargs="?", help="A few sentences: what is blocked and the action the owner must take")
    command.add_argument("--dry-run", action="store_true", help="Show each channel and payload without sending or using the page limit")
    command.add_argument("--relay", action="store_true", help=argparse.SUPPRESS)
    command = sub.add_parser("secret", help="Store and fetch API keys and tokens in the fleet vault, outside Git")
    command.add_argument("action", nargs="?", choices=["list", "get", "set", "remove"])
    command.add_argument("name", nargs="?", help="Environment variable name, such as DISCORD_EXAMPLE_BOT_TOKEN")
    command.add_argument("--scope", help="Identity the key belongs to (fleet.json secrets.scopes, or the profile identities)")
    command.add_argument("--note", help="For set: what the key is for and which account or app issued it")
    command.add_argument("--replace", action="store_true", help="For set: rotate an existing key; the old value is kept")
    command.add_argument("--relay", action="store_true", help=argparse.SUPPRESS)
    command = sub.add_parser("claude-debug", help="Debugging only, when the owner asks: tiny direct requests to a Claude account without the gateway",
                             description="Send a tiny, generic prompt straight to a direct Claude login configured in fleet.json debug.claude, "
                                         "bypassing the gateway. Only when the owner asks for it in the current thread. "
                                         "Prompts carry no code, files or task content. Only the owner runs `login` and `logout`.")
    command.add_argument("action", choices=["status", "ask", "login", "logout"])
    command.add_argument("name", help="A debug.claude entry in fleet.json")
    command.add_argument("prompt", nargs="?", help="For ask: a short generic prompt, such as 'Say ok.'")
    command.add_argument("--reason", help="For ask: what the owner asked you to check; it is logged with the request")
    command.add_argument("--model", default="claude-haiku-5-5", help="For ask (default: claude-haiku-5-5)")
    command.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    command.add_argument("--fast", action="store_true", help="For ask: request fast mode")
    command.add_argument("--max-usd", type=float, default=0.25, help="For ask: spending cap for the request (default: 0.25)")
    command = sub.add_parser("init", help="Write a starter fleet.json for a new owner")
    command.add_argument("--owner")
    command.add_argument("--state-dir")
    command.add_argument("--machine", action="append", default=[], metavar="SPEC",
                         help="Repeatable: NAME,os=macos|windows|linux,user=USER,address=ADDRESS,ssh=ALIAS,roles=ROLE+ROLE")
    command.add_argument("--identity", action="append", default=[], help="Repeatable account identity, such as personal or work")
    command.add_argument("--notify", choices=["discord-webhook", "slack-webhook", "ntfy", "openclaw", "command", "none"])
    command.add_argument("--output", type=Path, default=Path("fleet.json"))
    command.add_argument("--force", action="store_true", help="Replace an existing output file")
    command = sub.add_parser("config", help="Check fleet.json")
    command.add_argument("action", choices=["check"])
    command.add_argument("--config", type=Path, help="Default: FLEET_CONFIG, else the checkout's fleet.json")
    args = parser.parse_args()
    if args.command in {"init", "config"}:
        import fleetconfig
        if args.command == "init":
            output, _ = fleetconfig.init(args.output, args.owner, args.state_dir, args.machine, args.identity, args.notify, args.force)
            print(f"Wrote {output}. Edit its placeholders, then run `fleet config check --config {output}`.")
            return 0
        path = args.config or config_path()
        problems = fleetconfig.check_file(path)
        for problem in problems:
            print(problem)
        print(f"{path}: {len(problems)} problem(s)" if problems else f"{path}: valid")
        return int(bool(problems))
    config = load_config()
    if args.command == "doctor":
        return doctor(config, args.profile)
    if args.command == "sync":
        from skills_sync import sync, status
        if args.check:
            report = status(config, args.profile)
            print(encode(report), end="")
            return int(bool(report["problems"]))
        print(encode(sync(config, args.profile, args.release, apply=args.apply)), end="")
        return 0
    if args.command == "release":
        print(release(config))
    elif args.command == "profiles":
        for name in config["profiles"]:
            p = profile(config, name)
            print(f"{name:24} {p['identity']:14} {p['home']} {'managed' if p.get('managed') else 'native; preserved'}")
    elif args.command in {"diff", "apply"}:
        revision = args.release or read_json(state_dir(config) / "latest-release.json")["revision"]
        if not re.fullmatch(r"[0-9a-f]{20}", revision):
            raise ValueError("Invalid release id")
        deploy_profile(config, args.profile, state_dir(config) / "releases" / revision, apply=args.command == "apply")
    elif args.command == "task":
        make_task(config, args.repo, args.slug, args.profile, args.done)
    elif args.command == "page":
        if not args.relay and not args.text:
            raise ValueError("Describe the blocker in a few sentences")
        page(config, None if args.relay else args.text, dry_run=args.dry_run)
    elif args.command == "secret":
        if args.relay:
            print(encode(secret_local(config, json.loads(sys.stdin.read()))), end="")
            return 0
        if not args.action or (args.action != "list" and not (args.name and args.scope)):
            raise ValueError("Use `fleet secret list`, or get/set/remove with a NAME and --scope")
        request = {"action": args.action, "scope": args.scope, "name": args.name}
        if args.action == "set":
            value = getpass.getpass(f"{args.name}: ") if sys.stdin.isatty() else sys.stdin.read()
            request.update(value=value, note=args.note, replace=args.replace, sender=page_sender(config))
        reply = secret(config, request)
        if args.action == "get":
            print(reply["value"])
        elif args.action in ("set", "remove"):
            print(reply["saved"])
        elif not reply["secrets"]:
            print("The vault has no secrets" + (f" in the {args.scope} scope." if args.scope else "."))
        else:
            for record in reply["secrets"]:
                print(f"{record['scope']:13} {record['name']:40} {record['updated_at'][:10]}  {record['note']}")
    elif args.command == "claude-debug":
        if args.action == "ask":
            if not args.prompt:
                raise ValueError("Give ask a short generic prompt")
            return claude_debug_ask(config, args.name, args.prompt, args.reason, args.model, args.effort, args.fast, args.max_usd)
        if args.action == "login":
            print(claude_debug_login(config, args.name))
        elif args.action == "logout":
            claude_debug_run(config, args.name, ["auth", "logout"], check=True)
        else:
            print(claude_debug_status(config, args.name))
    else:
        arguments = args.args[1:] if args.args[:1] == ["--"] else args.args
        if args.command == "run":
            return run_profile(config, args.profile, arguments)
        if not arguments:
            raise ValueError("A lease needs a command after --")
        with lease(config, args.resource):
            return subprocess.call(arguments)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"fleet: {error}", file=sys.stderr)
        sys.exit(1)
