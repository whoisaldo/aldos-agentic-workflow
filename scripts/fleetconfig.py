#!/usr/bin/env python3
"""Validate fleet.json and write a starter one. Python 3.11+, standard library only."""
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "fleet.schema.json"
CHANNEL_TYPES = ("discord-webhook", "slack-webhook", "ntfy", "openclaw", "command")
# The settings each channel type reads. Its url, topic and token are references, since fleet.json is committed.
CHANNEL_KEYS = {"discord-webhook": {"url"}, "slack-webhook": {"url"}, "ntfy": {"server", "topic", "token", "priority"},
                "openclaw": {"channel", "account", "target", "binary"}, "command": {"argv"}}
OSES = ("macos", "windows", "linux")
TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "number": (int, float), "integer": int}


def load_schema():
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def type_ok(value, name):
    if isinstance(value, bool) and name in ("integer", "number"):
        return False
    return isinstance(value, TYPES[name])


def schema_errors(value, schema, root, path="fleet.json"):
    """The subset of JSON Schema 2020-12 that fleet.schema.json uses, with plain-language messages."""
    if "$ref" in schema:
        schema = root["$defs"][schema["$ref"].removeprefix("#/$defs/")]
    errors = []
    if "const" in schema and value != schema["const"]:
        return [f"{path}: must be {json.dumps(schema['const'])}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: must be one of {', '.join(map(str, schema['enum']))}"]
    if "type" in schema and not type_ok(value, schema["type"]):
        article = "an" if schema["type"][0] in "aeiou" else "a"
        return [f"{path}: must be {article} {schema['type']}"]
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: must not be empty")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: {json.dumps(value)} does not match {schema['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: must be at most {schema['maximum']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: needs at least {schema['minItems']} item(s)")
        if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            errors.append(f"{path}: has duplicate items")
        for i, item in enumerate(value):
            errors += schema_errors(item, schema.get("items", {}), root, f"{path}[{i}]")
    if isinstance(value, dict):
        if len(value) < schema.get("minProperties", 0):
            errors.append(f"{path}: needs at least {schema['minProperties']} entr{'y' if schema['minProperties'] == 1 else 'ies'}")
        if len(value) > schema.get("maxProperties", len(value)):
            errors.append(f"{path}: takes only one of {', '.join(schema.get('properties', {}))}")
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required key {json.dumps(key)}")
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if "propertyNames" in schema:
                errors += [e.replace(f"{path}.{key}: ", f"{path}: key ") for e in schema_errors(key, schema["propertyNames"], root, f"{path}.{key}")]
            if key in properties:
                errors += schema_errors(item, properties[key], root, f"{path}.{key}")
            elif extra is False:
                known = ", ".join(properties)
                errors.append(f"{path}: unknown key {json.dumps(key)} (known keys: {known})")
            elif isinstance(extra, dict):
                errors += schema_errors(item, extra, root, f"{path}.{key}")
    for part in schema.get("allOf", []):
        if "if" in part and not schema_errors(value, part["if"], root, path):
            errors += schema_errors(value, part.get("then", {}), root, path)
    return errors


def secret_scopes(config):
    scopes = config.get("secrets", {}).get("scopes")
    return sorted(scopes) if scopes else sorted({p.get("identity") for p in config.get("profiles", {}).values() if isinstance(p, dict) and p.get("identity")})


def inline_values(config):
    """A channel URL, topic or token written into fleet.json gets this explanation in place of the schema's type error."""
    notify = config.get("notify")
    channels = notify.get("channels") if isinstance(notify, dict) else None
    errors = []
    for i, channel in enumerate(channels if isinstance(channels, list) else []):
        for key in ("url", "topic", "token"):
            if isinstance(channel, dict) and isinstance(channel.get(key), str):
                errors.append(f"fleet.json.notify.channels[{i}].{key}: fleet.json is committed, so it never holds a URL, topic or token. Store it with "
                              f"`fleet secret set` and write {{\"secret\": \"SCOPE/NAME\"}}, or use {{\"env\": \"VAR\"}}")
    return errors


def semantic_errors(config, failed=()):
    """Rules that cross sections, which an editor schema cannot express. A rule reads only sections that passed the
    schema, so it can rely on their types; failed names the top-level sections that did not."""
    errors = []
    sound = lambda *sections: not set(sections) & set(failed)
    machines = config.get("machines", {})

    def machine_ref(path, name):
        if name not in machines:
            errors.append(f"{path}: {json.dumps(name)} is not a machine in fleet.json machines ({', '.join(machines) or 'none'})")

    if sound("profiles", "additional_gateways"):
        gateways = set(config.get("additional_gateways", {})) | ({"personal"} if "gateway" in config else set())
        for name, p in config.get("profiles", {}).items():
            if not p.get("managed"):
                continue
            for key in ("model", "effort"):
                if key not in p:
                    errors.append(f"fleet.json.profiles.{name}: a managed profile needs {json.dumps(key)}")
            if p.get("gateway", "personal") not in gateways:
                errors.append(f"fleet.json.profiles.{name}: gateway {json.dumps(p.get('gateway', 'personal'))} is not configured "
                              "(add \"gateway\" or an \"additional_gateways\" entry)")
    for section in ("notify", "secrets", "sync"):
        if section in config and sound(section, "machines"):
            machine_ref(f"fleet.json.{section}.host", config[section]["host"])
    if sound("sync", "machines"):
        sync = config.get("sync", {})
        for i, name in enumerate(sync.get("machines", [])):
            machine_ref(f"fleet.json.sync.machines[{i}]", name)
            if name == sync.get("host"):
                errors.append(f"fleet.json.sync.machines[{i}]: the sync host updates itself; list only the other machines")
        users = {m["user"].lower() for m in machines.values() if len(m.get("user", "")) > 2}
        if any(re.search(rf"(^|[.-]){re.escape(user)}([.-]|$)", sync.get("service_label", "").lower()) for user in users):
            errors.append("fleet.json.sync.service_label: must not contain a machine's username; use a neutral reverse-DNS label such as org.sharedfleet.skill-sync")
    if not sound("notify"):
        return errors
    scopes = secret_scopes(config) if sound("secrets", "profiles") else None
    names = set()
    for i, channel in enumerate(config.get("notify", {}).get("channels", [])):
        path = f"fleet.json.notify.channels[{i}]"
        name = channel.get("name", channel["type"])
        if name in names:
            errors.append(f"{path}: channel name {json.dumps(name)} is used twice; give each channel a distinct \"name\"")
        names.add(name)
        for key in ("url", "topic", "token"):
            ref = channel.get(key)
            if scopes is not None and isinstance(ref, dict) and "/" in ref.get("secret", ""):
                scope = ref["secret"].split("/", 1)[0]
                if "secrets" not in config:
                    errors.append(f"{path}.{key}: names a vault secret, but fleet.json has no \"secrets\" section")
                elif scope not in scopes:
                    errors.append(f"{path}.{key}: scope {json.dumps(scope)} is not one of the vault scopes ({', '.join(scopes)})")
        for key in sorted(set(channel) - CHANNEL_KEYS[channel["type"]] - {"name", "type"}):
            errors.append(f"{path}.{key}: is not used by a {channel['type']} channel")
    return errors


def validate(config, schema=None):
    """Return readable problems with a parsed fleet.json; an empty list means it is valid."""
    if not isinstance(config, dict):
        return ["fleet.json: must be a JSON object"]
    schema = schema or load_schema()
    generic = schema_errors(config, schema, schema)
    # Types first: the cross-section rules skip any top-level section the schema rejected, so a wrong type is reported, never raised.
    failed = {match.group(1) for match in (re.match(r"fleet\.json\.([^.\[:]+)", e) for e in generic) if match}
    specific = inline_values(config) + semantic_errors(config, failed)
    # A specific explanation replaces the generic schema message for the same value.
    covered = {e.split(": ", 1)[0] for e in specific}
    return [e for e in generic if e.split(": ", 1)[0] not in covered] + specific


def check_file(path):
    path = Path(path)
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"{path}: not found"]
    except json.JSONDecodeError as error:
        return [f"{path}: not valid JSON (line {error.lineno}, column {error.colno}: {error.msg})"]
    except RecursionError:
        return [f"{path}: nested too deeply to be a fleet.json"]
    return validate(config)


PLACEHOLDER_MACHINE = {"name": "devbox1", "os": "macos", "user": "your-user", "address": "100.64.0.1", "ssh": "devbox1", "roles": ["development"]}


def parse_machine(spec):
    """NAME,os=macos,user=me,address=100.64.0.1,ssh=alias,roles=development+macos"""
    name, *pairs = [part.strip() for part in spec.split(",")]
    machine = {"name": name}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or key not in ("os", "user", "address", "ssh", "roles", "repo", "work_root"):
            raise ValueError(f"--machine {spec!r}: use NAME,os=OS,user=USER,address=ADDRESS,ssh=ALIAS,roles=ROLE+ROLE")
        machine[key] = [r for r in value.split("+") if r] if key == "roles" else value
    if machine.get("os", "macos") not in OSES:
        raise ValueError(f"--machine {spec!r}: os must be one of {', '.join(OSES)}")
    return machine


def channel_template(kind, scope):
    """A notify channel with placeholder references. The values themselves go in the vault."""
    secret = lambda name: {"secret": f"{scope}/{name}"}
    return {
        "discord-webhook": {"name": "discord", "type": kind, "url": secret("FLEET_PAGE_DISCORD_WEBHOOK")},
        "slack-webhook": {"name": "slack", "type": kind, "url": secret("FLEET_PAGE_SLACK_WEBHOOK")},
        "ntfy": {"name": "ntfy", "type": kind, "server": "https://ntfy.sh", "topic": secret("FLEET_PAGE_NTFY_TOPIC")},
        "openclaw": {"name": "openclaw", "type": kind, "channel": "discord", "target": "user:YOUR_USER_ID"},
        "command": {"name": "command", "type": kind, "argv": ["/usr/bin/logger", "-t", "fleet-page"]},
    }[kind]


def starter_config(owner, state_dir, machines, identities, notify):
    machines = machines or [dict(PLACEHOLDER_MACHINE)]
    identities = list(dict.fromkeys(identities or ["personal"]))
    host = machines[0]["name"]
    config = {"$schema": "./fleet.schema.json", "schema_version": 1, "owner": owner or "Your name",
              "state_dir": state_dir or "~/Services/fleet", "machines": {}, "profiles": {}}
    for i, m in enumerate(machines):
        windows = m.get("os") == "windows"
        entry = {"os": m.get("os", "macos"), "user": m.get("user", "your-user"), "address": m.get("address", f"100.64.0.{min(i + 1, 3)}"),
                 "ssh": m.get("ssh", m["name"]), "repo": m.get("repo", "D:\\AgentWork\\fleet" if windows else "~/fleet"),
                 "roles": m.get("roles") or (["windows"] if windows else ["development"])}
        if windows:
            entry["work_root"] = m.get("work_root", "D:\\AgentWork")
        config["machines"][m["name"]] = entry
    for i, identity in enumerate(identities):
        suffix = "" if i == 0 else "_" + identity
        for driver, home in (("codex", "~/.codex"), ("claude", "~/.claude")):
            config["profiles"][f"{identity}-{driver}"] = {"driver": driver, "identity": identity, "home": home + suffix, "managed": False}
    config["protected_services"] = {}
    config["skill_policy"] = {"personal_identities": identities[:1]}
    config["secrets"] = {"host": host, "scopes": identities}
    config["notify"] = {"host": host, "max_chars": 500, "max_sentences": 3, "interval_minutes": 30,
                        "channels": [] if notify in (None, "none") else [channel_template(notify, identities[0])]}
    config["sync"] = {"host": host, "machines": [m["name"] for m in machines[1:]], "interval_seconds": 120,
                      "stale_after_minutes": 10, "service_label": "org.sharedfleet.skill-sync", "include_native": False, "shared_homes": []}
    return config


def ask(prompt, default):
    answer = input(f"{prompt} [{default}]: ").strip()
    return answer or default


def init(output, owner=None, state_dir=None, machines=(), identities=(), notify=None, force=False, interactive=None):
    output = Path(output)
    if output.exists() and not force:
        raise ValueError(f"{output} already exists. Choose another --output, or pass --force to replace it.")
    machines = [parse_machine(spec) for spec in machines]
    if interactive is None:
        interactive = sys.stdin.isatty()
    if interactive:
        owner = owner or ask("Owner", "Your name")
        state_dir = state_dir or ask("Private state directory", "~/Services/fleet")
        if not machines:
            first = {key: ask(f"First machine {key}", value if isinstance(value, str) else "+".join(value))
                     for key, value in PLACEHOLDER_MACHINE.items()}
            first["roles"] = [r for r in first["roles"].split("+") if r]
            machines = [first]
        if not identities:
            identities = [i for i in ask("Identities, comma-separated", "personal,work").replace(" ", "").split(",") if i]
        notify = notify or ask(f"Notify channel ({', '.join(CHANNEL_TYPES)}, none)", "none")
    if notify not in (None, "none", *CHANNEL_TYPES):
        raise ValueError(f"--notify must be one of {', '.join(CHANNEL_TYPES)} or none")
    config = starter_config(owner, state_dir, machines, identities, notify)
    if output.resolve().parent != ROOT:
        config["$schema"] = SCHEMA.as_uri()  # A relative path only finds the schema beside the checkout's own fleet.json.
    problems = validate(config)
    if problems:
        raise ValueError("The starter configuration would not pass `fleet config check`:\n  " + "\n  ".join(problems))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return output, config
