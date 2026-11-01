#!/usr/bin/env python3
"""Provision only fleet-owned gateways; never restart an existing service."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import plistlib
import secrets
import socket
import subprocess
import sys
import tarfile
import urllib.parse
import urllib.request

from fleetlib import atomic_write, expand, gateway_config, load_config, read_json


def init(config, identity):
    if sys.platform != "darwin":
        raise ValueError("Gateways run on devbox1; other machines use client profiles")
    gateway = gateway_config(config, identity)
    root = expand(gateway["runtime_dir"])
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    root.chmod(0o700)
    for folder in ["auth", "private", "logs", "login-state", "login-bin"]:
        (root / folder).mkdir(mode=0o700, exist_ok=True)
    version = config["gateway"]["version"]
    binary = expand(config["gateway"]["runtime_dir"]) / "releases" / ("v" + version) / "cli-proxy-api"
    if not binary.exists():
        binary.parent.mkdir(parents=True, exist_ok=True)
        archive = root / "private/release.tar.gz"
        asset = f"CLIProxyAPI_{version}_darwin_aarch64.tar.gz"
        urllib.request.urlretrieve(f"https://github.com/router-for-me/CLIProxyAPI/releases/download/v{version}/{asset}", archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != config["gateway"]["release_sha256"]:
            raise ValueError("Release checksum does not match the pinned manifest")
        with tarfile.open(archive) as source:
            member = source.getmember("cli-proxy-api")
            if not member.isfile():
                raise ValueError("Unexpected binary archive entry")
            binary.write_bytes(source.extractfile(member).read())
            binary.chmod(0o755)
    key_file = root / "private/client.json"
    if not key_file.exists():
        atomic_write(key_file, json.dumps({"api_key": secrets.token_urlsafe(40), "management_key": secrets.token_urlsafe(40)}))
    keys = read_json(key_file)
    address = urllib.parse.urlsplit(gateway["url"])
    host, port = address.hostname, address.port
    config_file = root / "config.yaml"
    if not config_file.exists():
        with socket.socket() as listener:
            listener.bind((host, port))
        content = {"config-version": 8, "server": {"host": host, "port": port, "trusted-proxies": [], "discovery": {"enabled": False}}, "management": {"allow-remote": True, "secret-key": keys["management_key"], "disable-auto-update-panel": True}, "access": {"api-keys": [keys["api_key"]]}, "oauth": {"auth-dir": str(root / "auth")}, "routing": {"strategy": "fill-first", "session-affinity": True, "session-affinity-ttl": "24h", "session-affinity-subagents": True}, "observability": {"logs": {"debug": False, "request-log": False, "logging-to-file": False, "logs-max-total-size-mb": 20, "error-logs-max-files": 5}, "usage": {"usage-statistics-enabled": True}}}
        atomic_write(config_file, json.dumps(content, indent=2) + "\n")
    label = gateway["service_label"]
    if label not in {"org.sharedfleet.fleet-gateway", "org.sharedfleet.fleet-gateway-secondary"}:
        raise ValueError("Refusing an unrelated service label")
    plist = Path.home() / "Library/LaunchAgents" / (label + ".plist")
    if not plist.exists():
        value = {"Label": label, "ProgramArguments": [str(binary), "-config", str(config_file)], "WorkingDirectory": str(root), "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 15, "StandardOutPath": str(root / "logs/stdout.log"), "StandardErrorPath": str(root / "logs/stderr.log"), "Umask": 63, "EnvironmentVariables": {"HOME": str(Path.home()), "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"}}
        atomic_write(plist, plistlib.dumps(value).decode())
    target = f"gui/{os.getuid()}/{label}"
    if subprocess.run(["launchctl", "print", target], capture_output=True).returncode:
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=True)
        print("Started", label)
    else:
        print("Preserved running service", label)


def login(config, identity, provider):
    gateway = gateway_config(config, identity)
    root = expand(gateway["runtime_dir"])
    if any(read_json(p).get("type") == provider for p in (root / "auth").glob("*.json")):
        raise ValueError("An account is already installed. Reauthorization requires an idle gateway maintenance window.")
    if provider == "codex":
        if identity != "personal":
            raise ValueError("Codex is only configured in the personal gateway")
        binary = root / "releases" / ("v" + config["gateway"]["version"]) / "cli-proxy-api"
        subprocess.run([str(binary), "-config", str(root / "config.yaml"), "-codex-device-login", "-no-browser"], check=True)
        return
    helper_home = root / "login-state"
    for name in ["open", "xdg-open"]:
        atomic_write(root / "login-bin" / name, "#!/bin/sh\nexit 0\n", 0o700)
    email = gateway["expected_email"]
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(helper_home), BROWSER="/usr/bin/true")
    env["PATH"] = str(root / "login-bin") + os.pathsep + env.get("PATH", "")
    args = [str(Path.home() / ".local/bin/claude"), "auth", "login", "--claudeai", "--email", email]
    if identity == "secondary":
        args.append("--sso")
    subprocess.run(args, env=env, check=True)
    service = "Claude Code-credentials-" + hashlib.sha256(str(helper_home).encode()).hexdigest()[:8]
    raw = subprocess.run(["security", "find-generic-password", "-s", service, "-w"], capture_output=True, text=True, check=True)
    credentials = json.loads(raw.stdout)["claudeAiOauth"]
    account = next(read_json(p)["oauthAccount"] for p in helper_home.glob("*.json") if "oauthAccount" in read_json(p))
    if account["emailAddress"].lower() != email.lower():
        raise ValueError("The signed-in email does not match this gateway identity")
    value = {"type": "claude", "id_token": "", "access_token": credentials["accessToken"], "refresh_token": credentials["refreshToken"], "expired": dt.datetime.fromtimestamp(credentials["expiresAt"] / 1000, dt.timezone.utc).isoformat(), "last_refresh": dt.datetime.now(dt.timezone.utc).isoformat(), "email": email, "account_uuid": account["accountUuid"], "organization_uuid": account["organizationUuid"], "organization_name": account.get("organizationName", ""), "claude_device_ids": [secrets.token_hex(32)]}
    atomic_write(root / "auth" / ("claude-" + identity + ".json"), json.dumps(value, indent=2) + "\n")
    # Delete only this helper home's local credential. Do not call logout,
    # which could revoke the token now owned by the gateway.
    subprocess.run(["security", "delete-generic-password", "-s", service], capture_output=True, check=True)
    print("Connected", identity, "Claude; gateway is the sole refresh-token owner")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "login"])
    parser.add_argument("--identity", choices=["personal", "secondary"], default="personal")
    parser.add_argument("--provider", choices=["claude", "codex"], default="claude")
    args = parser.parse_args()
    try:
        config = load_config()
        init(config, args.identity) if args.action == "init" else login(config, args.identity, args.provider)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
