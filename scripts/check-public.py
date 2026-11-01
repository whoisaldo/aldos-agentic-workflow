#!/usr/bin/env python3
"""Check tracked public source; report locations without printing matched values."""
import argparse
import bisect
import fnmatch
import functools
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER_USERS = {'your-user', 'example', 'example-user', 'username', 'USER', 'YOU', '...'}
PRIVATE_NAMES = {'auth.json', '.credentials.json', 'credentials.json', 'cookies.json',
                 't3-session.json', 'server-runtime.json', 'bank-selection.local.json', 'trifecta.json'}
PRIVATE_SUFFIXES = {'.pem', '.key', '.sqlite', '.sqlite3', '.db', '.xlsx', '.log', '.zip'}
UUID = re.compile(r'\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b', re.I)
MAC_HOME = re.compile(r'/' r'Users/([^/\s\x22\x27`<>]+)')
WIN_HOME = re.compile(r'[A-Z]:\\+Users\\+([^\\\s\x22\x27`<>]+)', re.I)
TAILNET = re.compile(r'\b(?:[a-z0-9-]+\.)+ts\.net\b', re.I)
TAIL_IP = re.compile(r'\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b')
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b')
# Reserved documentation domains (RFC 2606) plus GitHub's private-address relay.
PLACEHOLDER_EMAIL_DOMAINS = {'example.com', 'example.org', 'example.net', 'example.edu', 'users.noreply.github.com',
                             'domain.com'}  # the resume template's conventional placeholder
PLACEHOLDER_EMAILS = {'noreply@github.com'}
RESERVED_EMAIL_TLDS = {'example', 'invalid', 'test', 'localhost'}
ASSET_SUFFIXES = {'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'avif'}  # icon@2x.png is not an address
DEFAULT_ALLOWED_EMAILS = ['*@users.noreply.github.com', 'noreply@github.com']
BACKREFERENCE = re.compile(r'\\[1-9]|\(\?P=')
# Binary blobs are decoded only when they hold text the home, tailnet or address rules could match.
BINARY_CANDIDATES = re.compile(rb'users|ts\.net|100\.', re.I)


def load_denylist(path):
    """One case-insensitive regex per line; # comments and blank lines are ignored."""
    patterns = []
    try:
        lines = Path(path).expanduser().read_text().splitlines()
    except OSError as error:  # its path is private too
        raise ValueError(f'Cannot read the deny-list file: {error.strerror}') from None
    for number, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        try:
            patterns.append((number, re.compile(line.strip(), re.I)))
        except re.error:
            raise ValueError(f'Invalid deny-list pattern on deny-list line {number}') from None
    return tuple(patterns)


def denylist_rule(pattern_line):
    """Name the deny-list line that matched, never the term or the pattern."""
    return f'denylisted-term (deny-list line {pattern_line})'


def denied_lines(text, denylist):
    return [pattern_line for pattern_line, pattern in denylist if pattern.search(text)]


@functools.lru_cache(maxsize=None)
def any_denied(denylist):
    """One regex matching wherever some pattern matches, so most lines take a single search.
    None when the patterns cannot be combined (backreferences, inline flags, clashing groups)."""
    if not denylist or any(BACKREFERENCE.search(pattern.pattern) for _, pattern in denylist):
        return None
    try:
        return re.compile('|'.join(f'(?:{pattern.pattern})' for _, pattern in denylist), re.I)
    except re.error:
        return None


def denylist_text_findings(text, denylist):
    """(rule, line number) for each pattern and text line it matches."""
    combined = any_denied(tuple(denylist))
    result = []
    for number, line in enumerate(text.splitlines(), 1):
        if combined is None or combined.search(line):
            result.extend((denylist_rule(pattern_line), number) for pattern_line in denied_lines(line, denylist))
    return result


def email_allowed(address):
    address = address.lower()
    domain = address.rsplit('@', 1)[1]
    return (address in PLACEHOLDER_EMAILS or domain in PLACEHOLDER_EMAIL_DOMAINS
            or domain.rsplit('.', 1)[1] in RESERVED_EMAIL_TLDS | ASSET_SUFFIXES)


def is_license(path):
    return (path.name.upper().startswith(('LICENSE', 'LICENCE', 'COPYING', 'NOTICE'))
            or path.parts[:2] == ('bank', 'licenses'))


def has_personal_email(text):
    return any(not email_allowed(m.group()) for m in EMAIL.finditer(text))


def shown_path(name, denylist=()):
    """The path as printed. From the first part holding a denied term or a real-looking
    email on, it is replaced by a short hash of the whole path."""
    parts = name.split('/')
    for index in range(len(parts)):
        prefix = '/'.join(parts[:index + 1])
        if denied_lines(prefix, denylist) or has_personal_email(prefix):
            return '/'.join(parts[:index] + [f'<redacted name {hashlib.sha256(name.encode()).hexdigest()[:10]}>'])
    return name


def shown_ref(name):
    """A ref name as printed: always a short hash, since only a ref name that fails is ever printed."""
    return f'<redacted ref {hashlib.sha256(name.encode()).hexdigest()[:10]}>'


def ref_name_findings(names, denylist=()):
    """('ref-name', rule, shown name, 0) for each ref name holding a denied term or a real-looking email."""
    result = []
    for name in dict.fromkeys(names):
        rules = [denylist_rule(pattern_line) for pattern_line in denied_lines(name, denylist)]
        rules += ['personal-email'] * has_personal_email(name)
        result.extend(('ref-name', rule, shown_ref(name), 0) for rule in rules)
    return result


def content_kind(name):
    """The parts of a name that content checks depend on: (check emails, check example ids)."""
    path = PurePosixPath(name)
    return (path.parts[0] != 'vendor' and not is_license(path),
            '/examples/' in name and path.suffix.lower() in {'.json', '.md', '.txt'})


def name_findings(name, denylist=()):
    result = []
    path = PurePosixPath(name)
    if (path.name in PRIVATE_NAMES or path.suffix.lower() in PRIVATE_SUFFIXES
            or path.name == '.env' or path.name.startswith('.env.') and not path.name.endswith('.example')
            or name.endswith('/canvas-planning/config.json')
            or any(part in {'.t3', '.openclaw', 'sessions', 'backups'} for part in path.parts)):
        result.append(('private-state-file', name, 0))
    result.extend((denylist_rule(pattern_line), name, 0) for pattern_line in denied_lines(name, denylist))
    if content_kind(name)[0] and has_personal_email(name):
        result.append(('personal-email', name, 0))
    return result


def content_findings(data, kind, denylist=()):
    """(rule, line number) for one blob's bytes; the name matters only through content_kind."""
    check_email, example_ids = kind
    binary = b'\0' in data  # decoded media bytes produce false text matches
    if binary and not example_ids and not BINARY_CANDIDATES.search(data):
        return []
    result = []
    text = data.decode('utf-8', errors='ignore')
    if denylist and not binary:
        result.extend(denylist_text_findings(text, denylist))
    check_email = check_email and not binary
    for number, line in enumerate(text.splitlines(), 1):
        if check_email and has_personal_email(line):
            result.append(('personal-email', number))
        if any(m.group(1) not in PLACEHOLDER_USERS for regex in (MAC_HOME, WIN_HOME) for m in regex.finditer(line)):
            result.append(('personal-home', number))
        if any(m.group().lower() != 'tailnet-name.ts.net' and not m.group().lower().endswith('.tailnet-name.ts.net') for m in TAILNET.finditer(line)):
            result.append(('private-tailnet-name', number))
        for match in TAIL_IP.finditer(line):
            try:
                address = ipaddress.ip_address(match.group())
            except ValueError:
                continue
            if str(address) not in {'100.64.0.1', '100.64.0.2', '100.64.0.3'}:
                result.append(('private-tailnet-address', number))
        if example_ids and any(not m.group().startswith('00000000-0000-0000-0000-') for m in UUID.finditer(line)):
            result.append(('example-session-id', number))
    return result


def findings(name, data, denylist=()):
    """Supplement Gitleaks with public-repo privacy and state-file checks."""
    return name_findings(name, denylist) + [(rule, name, line) for rule, line in content_findings(data, content_kind(name), denylist)]


def git_env(**extra):
    """The environment of every git call, Gitleaks' included. A local replace ref could make a commit seem to
    descend from history it does not, and git push ignores replace refs."""
    return {**os.environ, 'GIT_NO_REPLACE_OBJECTS': '1', **extra}


def git(*args, root=ROOT, input=None):
    """Git's output. A failure names only the subcommand: git's message can quote paths and revisions."""
    process = subprocess.run(['git', *args], cwd=root, input=input, capture_output=True, env=git_env())
    if process.returncode:
        raise ValueError(f'git {args[0]} failed with exit status {process.returncode}; its message is withheld')
    return process.stdout


def current_files(root=ROOT, denylist=()):
    result = {}
    for raw in git('ls-files', '-z', root=root).split(b'\0'):
        if not raw:
            continue
        name = raw.decode()
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f'Unreviewed link: {shown_path(name, denylist)}')
        if path.is_file():
            result[name] = path.read_bytes()
    return result


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def inventory_findings(files):
    """files maps tracked names to their bytes."""
    if 'bank/files.json' not in files:
        return []  # Old history may predate the bank.
    digests = {name: sha256(data) for name, data in files.items() if name != 'bank/files.json'}
    return check_inventory(json.loads(files['bank/files.json']), digests)


def check_inventory(inventory, digests):
    """Compare bank/files.json with the digests of every other tracked file."""
    result = []
    if set(inventory) != set(digests):
        result.append(('inventory-file-set', 'bank/files.json', 0))
    for name, digest in inventory.items():
        if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
            result.append(('inventory-invalid-hash', 'bank/files.json', 0))
        elif digests.get(name) != digest:
            result.append(('inventory-byte-mismatch', name, 0))
    return result


def history_revisions(root=ROOT, ranges=(), exclude=()):
    """(revisions, boundary). The revisions are those reachable from HEAD, or the union of
    the given ranges less what the exclude commits reach; never every ref, so local checkpoints and
    unrelated branches stay out. The boundary holds the excluded parents of the ranges: the base they were built on."""
    revisions, boundary = {}, {}
    hidden = ''.join(f'^{commit}\n' for commit in exclude).encode()  # on stdin: there can be thousands
    for spec in [r.split() for r in ranges] or [['HEAD']]:
        for line in git('rev-list', '--boundary', *spec, '--stdin', '--', root=root, input=hidden).decode().split():
            (boundary if line.startswith('-') else revisions)[line.lstrip('-')] = None
    return list(revisions), [b for b in boundary if b not in revisions]


def tree_entries(revision, root=ROOT):
    """(mode, kind, blob id, name) for every file of a revision."""
    for entry in git('ls-tree', '-rz', revision, root=root).split(b'\0'):
        if entry:
            meta, raw_name = entry.split(b'\t', 1)
            yield (*meta.split(), raw_name.decode())


def published_commits(refs, root=ROOT):
    """The commit each --published ref names."""
    if not refs:
        return []
    checked = git('cat-file', '--batch-check', root=root, input=''.join(f'{ref}^{{commit}}\n' for ref in refs).encode())
    if any(' commit ' not in line for line in checked.decode().splitlines()):
        raise ValueError('A --published value names no commit')
    return [line.split()[0] for line in checked.decode().splitlines()]


def published_files(boundary, commits=(), root=ROOT):
    """(pairs, unproven). The pairs are the (name, blob id) pairs in the boundary revisions that the published
    commits reach; a range does not report them again. A base no published commit reaches proves nothing, so its
    files are checked; such bases are the unproven ones."""
    if not commits:
        return set(), list(boundary)
    if not boundary:
        return set(), []
    walk = ''.join(f'{revision}\n' for revision in boundary) + ''.join(f'^{commit}\n' for commit in commits)
    unreached = set(git('rev-list', '--stdin', '--', root=root, input=walk.encode()).decode().split())
    pairs = {(name, oid) for revision in boundary if revision not in unreached
             for _, _, oid, name in tree_entries(revision, root)}
    return pairs, [revision for revision in boundary if revision in unreached]


def inherited_files(revisions, unproven, published=(), root=ROOT):
    """(revision, name, blob id) for each file of the revisions whose blob an unproven range base also holds,
    with the first revision holding it. Gitleaks' git scan reads only the diffs of a range, and an unchanged
    or renamed file has none."""
    base = {oid for revision in unproven for _, kind, oid, _ in tree_entries(revision, root) if kind == b'blob'}
    first = {}
    for revision in revisions if base else ():
        for mode, kind, oid, name in tree_entries(revision, root):
            if oid in base and kind == b'blob' and mode != b'120000' and (name, oid) not in published:
                first.setdefault((name, oid), revision)
    return [(revision, name, oid) for (name, oid), revision in first.items()]


def merge_files(revisions, published=(), root=ROOT):
    """(revision, name, blob id) for each file of a merge among the revisions whose blob none of its parents holds
    at that path: a conflict resolution or an evil merge. Gitleaks' git scan shows no diff for a merge."""
    if not revisions:
        return []
    listed = git('rev-list', '--no-walk=unsorted', '--parents', '--stdin', root=root,
                 input=''.join(r + '\n' for r in revisions).encode()).decode().splitlines()
    merges = [line.split()[0] for line in listed if len(line.split()) > 2]
    if not merges:
        return []
    # A combined diff lists each path whose merge version differs from every parent's.
    tokens = git('diff-tree', '--stdin', '-c', '-r', '--raw', '--no-abbrev', '-z', root=root,
                 input=''.join(m + '\n' for m in merges).encode()).split(b'\0')
    result, revision, index = [], None, 0
    while index < len(tokens):
        token, index = tokens[index], index + 1
        if not token.startswith(b':'):
            revision = token.decode() or revision
            continue
        parents = len(token) - len(token.lstrip(b':'))
        fields = token.lstrip(b':').split()
        mode, oid, name = fields[parents], fields[2 * parents + 1], tokens[index].decode()
        index += 1
        if mode not in {b'000000', b'120000', b'160000'} and (name, oid) not in published:
            result.append((revision, name, oid))
    return result


SCHEME_URL = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*://')
SCP_LIKE = re.compile(r'[^/]*:')  # host:path, a colon before any slash, as git reads an address


def listing_target(destination, cwd):
    """What to list for a push destination: a URL or scp-like address as given, and anything else as a local path
    made absolute, so git cannot take it for a remote name. None when that path does not exist."""
    if SCHEME_URL.match(destination) or SCP_LIKE.match(destination):
        return destination
    path = os.path.abspath(os.path.join(cwd, destination))
    return path if destination and os.path.exists(path) else None


def ssh_command(root=ROOT):
    """The ssh command git would run, told to fail rather than ask for a password or wait on a connection."""
    configured = subprocess.run(['git', 'config', '--get', 'core.sshCommand'], cwd=root, capture_output=True, env=git_env())
    command = (os.environ.get('GIT_SSH_COMMAND') or configured.stdout.decode().strip()
               or shlex.quote(os.environ.get('GIT_SSH') or 'ssh'))
    return command + ' -o BatchMode=yes -o ConnectTimeout=10'


def history_rewritten(root=ROOT):
    """Whether this clone is shallow or has a grafts file: its history then lacks or invents parents, and
    git push follows the same view, so no listed commit can prove what the destination holds."""
    shallow, grafts = git('rev-parse', '--is-shallow-repository', '--git-path', 'info/grafts', root=root).decode().splitlines()
    return shallow == 'true' or os.path.exists(os.environ.get('GIT_GRAFT_FILE') or Path(root, grafts))


def list_destination(destination, timeout, root=ROOT):
    """(commits, problem): the commits the destination lists that this clone holds, or no commits and why.
    The listing never prompts and stops after timeout seconds; git's output and the destination stay unprinted."""
    target = listing_target(destination, os.getcwd())
    if target is None:
        return [], 'the destination path does not exist'
    env = git_env(GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='false', SSH_ASKPASS='false', SSH_ASKPASS_REQUIRE='never',
                  GCM_INTERACTIVE='never', GIT_SSH_COMMAND=ssh_command(root))
    resolved = subprocess.run(['git', 'ls-remote', '--get-url', '--', target], cwd=root, capture_output=True, env=env)
    if resolved.returncode or resolved.stdout.decode().rstrip('\n') != target:
        return [], 'git would list another repository for that name'  # a remote of that name, or a URL rewrite
    # A session of its own: no terminal to prompt on, and a timeout stops ssh or a credential helper too.
    process = subprocess.Popen(['git', 'ls-remote', '--', target], cwd=root, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        listed = process.communicate(timeout=timeout)[0]
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # it ended just now
        process.communicate()
        return [], f'no answer within {timeout:g} s'
    if process.returncode:
        return [], 'git ls-remote failed; its message is withheld'
    oids = dict.fromkeys(line.split(b'\t', 1)[0] for line in listed.splitlines())
    found = git('cat-file', '--batch-check=%(objectname)', root=root, input=b''.join(oid + b'^{commit}\n' for oid in oids))
    return list(dict.fromkeys(line for line in found.decode().split('\n') if re.fullmatch('[0-9a-f]{40,64}', line))), None


def tag_target(data):
    return data.split(None, 2)[1]  # every tag object starts with 'object <id>'


def read_tags(oids, root=ROOT):
    """{id: (type, bytes)} for each object among the ids that is not a commit and, for tags, for what
    they name in turn. Commits are left to rev-list."""
    found, pending = {}, list(dict.fromkeys(oids))
    while pending:
        objects, pending = list(read_objects(pending, root)), []
        for oid, kind, data in objects:
            if kind != b'commit':
                found[oid] = (kind, data)
            if kind == b'tag' and tag_target(data) not in found:
                pending.append(tag_target(data))
        pending = list(dict.fromkeys(pending))
    return found


def tagged_objects(root=ROOT, ranges=(), revisions=None):
    """{id: (type, bytes)} for each annotated tag a range names and, when revisions are given (--history),
    each one under refs/tags that leads into them; with the tags those name and any tree or blob they end at."""
    starts = []
    for spec in ranges:
        for line in git('rev-parse', *spec.split(), '--', root=root).decode().splitlines():
            if line == '--':
                break
            if not line.startswith(('^', '-')):
                starts.append(line.encode())
    found = read_tags(starts, root)
    if revisions is not None:
        listed = git('for-each-ref', '--format=%(objecttype) %(objectname)', 'refs/tags', root=root).decode().splitlines()
        tags = [line.split()[1].encode() for line in listed if line.startswith('tag ')]
        behind, reached = read_tags(tags, root), set(revisions)
        for tag in tags:
            chain, oid = [], tag
            while oid in behind and behind[oid][0] == b'tag':
                chain.append(oid)
                oid = tag_target(behind[oid][1])
            if oid.decode() in reached:
                found.update((link, behind[link]) for link in chain)
    return found


def identity_allowed(email, allowed_emails=()):
    return any(fnmatch.fnmatchcase(email.lower(), p.lower()) for p in [*DEFAULT_ALLOWED_EMAILS, *allowed_emails])


def commit_findings(root=ROOT, allowed_emails=(), denylist=(), revisions=None):
    """Check commit identities and messages; report short SHAs, never the values."""
    result = []
    revisions = history_revisions(root)[0] if revisions is None else revisions
    if not revisions:
        return result  # git log with no revisions would read HEAD
    log = git('log', '--no-walk=unsorted', '--stdin', '-z', '--format=%H%x01%ae%x01%ce%x01%an%x01%cn%x01%B',
              root=root, input=''.join(r + '\n' for r in revisions).encode()).decode('utf-8', errors='replace')
    for record in filter(None, log.split('\0')):
        revision, author, committer, author_name, committer_name, message = record.lstrip('\n').split('\x01', 5)
        for role, email in [('author-email', author), ('committer-email', committer)]:
            if not identity_allowed(email, allowed_emails):
                result.append((revision[:12], 'personal-commit-email', role, 0))
        for label, text in [('commit-names', author_name + '\n' + committer_name), ('commit-message', message)]:
            result.extend((revision[:12], rule, label, line) for rule, line in denylist_text_findings(text, denylist))
    return result


def commit_messages(revisions, root=ROOT):
    """(revision, message bytes) for each revision."""
    if not revisions:
        return []  # git log with no revisions would read HEAD
    log = git('log', '--no-walk=unsorted', '--stdin', '-z', '--format=%H%x01%B', root=root,
              input=''.join(r + '\n' for r in revisions).encode())
    return [tuple(record.lstrip(b'\n').split(b'\x01', 1)) for record in filter(None, log.split(b'\0'))]


def local_ref_names(root=ROOT):
    """The names of the local branches and tags, which --history checks too."""
    return git('for-each-ref', '--format=%(refname)', 'refs/heads', 'refs/tags', root=root).decode(errors='replace').split('\n')[:-1]


def tag_findings(tagged, allowed_emails=(), denylist=()):
    """Check annotated tags (name, tagger, message and signature block) and tagged blobs, as
    tagged_objects returns them; report short ids, never the values."""
    result = []
    for oid, (kind, data) in tagged.items():
        ref = oid.decode()[:12]
        if kind == b'blob':
            result.extend((ref, rule, 'tagged-blob', line) for rule, line in content_findings(data, (True, False), denylist))
        if kind != b'tag':
            continue
        header, _, body = data.decode('utf-8', errors='replace').partition('\n\n')
        names, other = [], []
        for line in header.splitlines():
            key, _, value = line.partition(' ')
            if key == 'tag':
                names.append(value)
            elif key == 'tagger':
                name, _, email = value.partition(' <')
                names.append(name)
                if not identity_allowed(email.split('>', 1)[0], allowed_emails):
                    result.append((ref, 'personal-commit-email', 'tagger-email', 0))
            elif key not in {'object', 'type'}:
                other.append(line)  # such as a gpgsig header
        text = body.rstrip('\n') + ''.join('\n' + line for line in other)
        result.extend((ref, rule, 'tag-names', line) for rule, line in denylist_text_findings('\n'.join(names), denylist))
        result.extend((ref, rule, 'tag-message', line) for rule, line in content_findings(text.encode(), (True, False), denylist))
    return result


def read_objects(oids, root=ROOT):
    """Yield (oid, type, bytes) for each object id through one git cat-file process."""
    process = subprocess.Popen(['git', 'cat-file', '--batch'], cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=git_env())

    def feed():  # a separate writer, so neither pipe fills while the other waits
        try:
            process.stdin.write(b''.join(oid + b'\n' for oid in oids))
            process.stdin.close()
        except OSError:
            pass  # the reader stopped early

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    try:
        for oid in oids:
            header = process.stdout.readline().split()
            if len(header) != 3:
                raise ValueError('git cat-file could not read an object')
            data = process.stdout.read(int(header[2]))
            process.stdout.read(1)  # the newline after each object
            yield oid, header[1], data
    finally:
        process.stdout.close()
        if writer.is_alive():
            process.kill()
        writer.join()
        process.wait()


def history_findings(root=ROOT, denylist=(), revisions=None, published=()):
    """Check the files of the revisions, reading and scanning each unique blob once.
    Files in published, (name, blob id) pairs, are only hashed for the inventory."""
    revisions = history_revisions(root)[0] if revisions is None else revisions
    result, trees, first_seen, kinds, links = [], [], {}, {}, set()
    for revision in revisions:
        tree = {}
        for mode, kind, oid, name in tree_entries(revision, root):
            if kind != b'blob' or mode == b'120000':
                if (name, oid) not in links and (name, oid) not in published:
                    result.append((revision[:12], 'unreviewed-link', name, 0))
                    links.add((name, oid))
                continue
            tree[name] = oid
            if (name, oid) not in published:
                first_seen.setdefault((name, oid), revision)
                kinds.setdefault(oid, set()).add(content_kind(name))
        trees.append((revision, tree))
    inventory_ids = {tree['bank/files.json'] for _, tree in trees if 'bank/files.json' in tree}
    digests, contents, inventories = {}, {}, {}
    for oid, _, data in read_objects(list({oid for _, tree in trees for oid in tree.values()}), root):
        digests[oid] = sha256(data)
        for kind in kinds.get(oid, ()):
            contents[oid, kind] = content_findings(data, kind, denylist)
        if oid in inventory_ids:
            inventories[oid] = json.loads(data)
    for (name, oid), revision in first_seen.items():
        result.extend((revision[:12], *item) for item in name_findings(name, denylist))
        result.extend((revision[:12], rule, name, line) for rule, line in contents[oid, content_kind(name)])
    for revision, tree in trees:
        if 'bank/files.json' in tree:
            known = {name: digests[oid] for name, oid in tree.items() if name != 'bank/files.json'}
            result.extend((revision[:12], *item) for item in check_inventory(inventories[tree['bank/files.json']], known))
    return result, len(revisions)


def run_gitleaks(command, root=ROOT, cwd=None, input=None):
    """(commit, rule, file, line) for each leak, from Gitleaks' JSON report. Its own output is withheld:
    it prints file paths, authors and emails."""
    with tempfile.TemporaryDirectory(prefix='fleet-public-gitleaks-') as temp:
        report = Path(temp) / 'report.json'
        process = subprocess.run(['gitleaks', *command, '--redact', '--no-banner', '--config', str(root / '.gitleaks.toml'),
                                  '--report-format', 'json', '--report-path', str(report)],
                                 cwd=cwd or root, input=input, capture_output=True, env=git_env())
        leaks = json.loads(report.read_text()) if report.exists() else []
    if process.returncode and not leaks:
        raise ValueError(f'Gitleaks could not complete a scan (exit status {process.returncode}); its output is withheld')
    return [(leak.get('Commit') or '', leak.get('RuleID'), leak.get('File') or '', leak.get('StartLine') or 0) for leak in leaks]


def snapshot_secrets(files, root=ROOT):
    """(rule, name, line) for each leak Gitleaks finds in files (names to bytes) copied to a temporary directory."""
    with tempfile.TemporaryDirectory(prefix='fleet-public-scan-') as temp:
        snapshot = Path(temp)
        for name, data in files.items():
            dest = snapshot / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        return [(rule, os.path.normpath(name), line) for _, rule, name, line in run_gitleaks(['dir', '.'], root, snapshot)]


def stdin_secrets(items, root=ROOT):
    """(ref, rule, label, line) for each leak Gitleaks finds in items, (ref, label, bytes) triples, through one
    stdin scan. Each item starts on a fresh line, so a leak's line maps back to its item."""
    starts, chunks, offset = [], [], 0
    for _, _, data in items:
        data = data if data.endswith(b'\n') else data + b'\n'
        starts.append(offset)
        chunks.append(data)
        offset += data.count(b'\n')
    if not chunks:
        return []
    result = []
    for _, rule, _, line in run_gitleaks(['stdin'], root, input=b''.join(chunks)):
        index = bisect.bisect_right(starts, line - 1) - 1
        ref, label, _ = items[index]
        result.append((ref, f'gitleaks:{rule}', label, line - starts[index]))
    return result


def blob_secrets(entries, root=ROOT):
    """(ref, rule, name, line) for each leak in entries, (ref, name, blob id) triples, scanned as files at
    their own paths. Two versions of one name go to separate snapshots."""
    data = {oid: content for oid, _, content in read_objects(list({oid for _, _, oid in entries}), root)}
    layers = []
    for ref, name, oid in entries:
        layer = next((layer for layer in layers if name not in layer), None)
        if layer is None:
            layers.append(layer := {})
        layer[name] = (ref, oid)
    result = []
    for layer in layers:
        found = snapshot_secrets({name: data[oid] for name, (_, oid) in layer.items()}, root)
        result.extend((layer[name][0], f'gitleaks:{rule}', name, line) for rule, name, line in found)
    return result


def scan_secrets(files, history, root=ROOT, ranges=(), tagged=None, revisions=(), ref_names=(), inherited=()):
    """Gitleaks findings for the tracked files (unless files is None), the history or the ranges' revisions, the
    revisions' commit messages, the ref names, the tags, tagged blobs and tagged trees from tagged_objects, and the
    inherited and merge files, (revision, name, blob id) triples, that the diffs do not show."""
    if not shutil.which('gitleaks'):
        raise ValueError('Install Gitleaks before --secrets (see docs/public-release.md).')
    result = []
    if files is not None:
        result.extend(('working-tree', f'gitleaks:{rule}', name, line) for rule, name, line in snapshot_secrets(files, root))
    if ranges:  # the revisions by name: what excludes the rest can run to thousands of commits
        log_chunks = ['--no-walk=unsorted ' + ' '.join(revisions[i:i + 1000]) for i in range(0, len(revisions), 1000)]
    else:
        log_chunks = ['HEAD'] if history else []
    for log_opts in log_chunks:
        result.extend((commit[:12], f'gitleaks:{rule}', name, line)
                      for commit, rule, name, line in run_gitleaks(['git', f'--log-opts={log_opts}', '.'], root))
    texts = [(revision.decode()[:12], 'commit-message', message) for revision, message in commit_messages(revisions, root)]
    texts.extend(('ref-name', shown_ref(name), name.encode()) for name in dict.fromkeys(ref_names))
    for oid, (kind, data) in (tagged or {}).items():
        ref = oid.decode()[:12]
        if kind == b'tree':
            entries = {name: blob for mode, entry, blob, name in tree_entries(oid.decode(), root)
                       if entry == b'blob' and mode != b'120000'}
            blobs = {blob: content for blob, _, content in read_objects(list(set(entries.values())), root)}
            tree_files = {name: blobs[blob] for name, blob in entries.items()}
            result.extend((ref, f'gitleaks:{rule}', name, line) for rule, name, line in snapshot_secrets(tree_files, root))
        else:
            texts.append((ref, 'tag-object' if kind == b'tag' else 'tagged-blob', data))
    result.extend(stdin_secrets(texts, root))
    result.extend(blob_secrets([(revision[:12], name, oid) for revision, name, oid in inherited], root))
    return result


def shown_file(filename, root=ROOT, denylist=()):
    """An OSError's file as printed: inside the checkout through shown_path, otherwise not at all."""
    path = Path(os.path.abspath(os.fsdecode(filename)))
    for base in (Path(os.path.abspath(root)), Path(root).resolve()):
        if path.is_relative_to(base):
            return shown_path(path.relative_to(base).as_posix(), denylist)
    return 'a file outside the checkout'


def error_text(error, root=ROOT, denylist=()):
    """An error as printed. Only the checker's own messages appear whole; system errors lose their
    paths, and anything else is named by type, because paths and terms reach exception text."""
    if isinstance(error, OSError):
        text = error.strerror or type(error).__name__
        return text if error.filename is None else f'{text}: {shown_file(error.filename, root, denylist)}'
    if isinstance(error, ValueError):
        return str(error)
    return f'unexpected {type(error).__name__}'


class QuietParser(argparse.ArgumentParser):
    """Argument errors name no value: a stray argument can be a private path or term."""
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f'{self.prog}: error: invalid arguments; values withheld (see --help)\n')


def main(argv=None, root=ROOT):
    parser = QuietParser(prog='check-public.py', description=__doc__)
    parser.add_argument('--history', action='store_true', help='Also check every revision reachable from HEAD and its commits.')
    parser.add_argument('--range', action='append', default=[], metavar='REV_RANGE',
                        help='Check only these revisions and their commits, not the working tree '
                             '(git rev-list arguments such as origin/main..HEAD). Repeatable.')
    parser.add_argument('--published', action='append', default=[], metavar='REF',
                        help='With --range, an object name or ref the destination already has. '
                             'Files a range keeps unchanged from a base these refs reach are not reported again; '
                             'without it every file of the range is checked. Repeatable.')
    parser.add_argument('--secrets', action='store_true', help='Also require a redacted Gitleaks scan.')
    parser.add_argument('--denylist', default=os.environ.get('PUBLIC_CHECK_DENYLIST') or None, metavar='FILE',
                        help='Private file of case-insensitive regexes, one per line (env PUBLIC_CHECK_DENYLIST). Keep it outside Git.')
    parser.add_argument('--allow-email', action='append', default=[], metavar='PATTERN',
                        help='Also allow this commit email or glob with --history. Repeatable.')
    parser.add_argument('--ref-name', action='append', default=[], metavar='NAME',
                        help='Also check this ref name, such as one a push creates, against the deny-list, the email '
                             'rule and, with --secrets, Gitleaks. --history checks local branch and tag names. Repeatable.')
    parser.add_argument('--destination', metavar='URL_OR_PATH',
                        help='With --range, a push destination to list: the listed commits this clone holds count as '
                             'published and leave every range. A name that is not a URL or host:path is a local path. '
                             'If the listing fails, nothing counts as published. Never printed.')
    parser.add_argument('--destination-timeout', type=float, default=30, metavar='SECONDS',
                        help='Give up listing the destination after this long (default 30).')
    args = parser.parse_args(argv)
    if args.destination is not None and not args.range:
        parser.error('--destination needs --range')
    denylist = ()
    try:
        denylist = load_denylist(args.denylist) if args.denylist else ()
        files, results, revisions, tagged, inherited = None, [], [], {}, []
        ref_names = args.ref_name + (local_ref_names(root) if args.history else [])
        if not args.range:
            files = current_files(root, denylist)
            results = [('working-tree', *item) for name, data in files.items() for item in findings(name, data, denylist)]
            results.extend(('working-tree', *item) for item in inventory_findings(files))
        if args.history or args.range:
            commits, listed, note = published_commits(args.published, root), [], None
            if (commits or args.destination is not None) and history_rewritten(root):
                commits, note = [], 'this clone is shallow or has grafts, so its history cannot prove what the destination holds'
            elif args.destination is not None:
                listed, problem = list_destination(args.destination, args.destination_timeout, root)
                commits += listed
                note = problem and f"could not list the destination's refs ({problem})"
            if note:
                print(f'Public check: {note}; nothing counts as published, so everything the pushed tips reach is checked.',
                      file=sys.stderr)
            revisions, boundary = history_revisions(root, args.range, listed)
            tagged = tagged_objects(root, args.range, revisions if args.history else None)
            trees = [oid.decode() for oid, (kind, _) in tagged.items() if kind == b'tree']
            published, unproven = published_files(boundary, commits, root)
            results.extend(history_findings(root, denylist, revisions + trees, published)[0])
            if args.secrets:
                unshown = inherited_files(revisions, unproven, published, root) + merge_files(revisions, published, root)
                inherited = list({(name, oid): (revision, name, oid) for revision, name, oid in unshown}.values())
            results.extend(commit_findings(root, args.allow_email, denylist, revisions))
            results.extend(tag_findings(tagged, args.allow_email, denylist))
        results.extend(ref_name_findings(ref_names, denylist))
        if args.secrets:
            results.extend(scan_secrets(files, args.history, root, args.range, tagged, revisions, ref_names, inherited))
        for ref, rule, name, line in results:
            print(f'{ref}: {rule}: {shown_path(name, denylist)}:{line}', file=sys.stderr)
        if results:
            print(f'Public check failed: {len(results)} location(s). Values withheld.', file=sys.stderr)
            return 1
        scope = 'no working tree' if files is None else f'{len(files)} tracked files'
        tags = sum(kind == b'tag' for kind, _ in tagged.values())
        print(f'Public check passed: {scope}; {len(revisions)} historical revision(s); {tags} tag object(s); '
              f'{len(denylist)} deny-list pattern(s).')
        return 0
    except Exception as error:  # never a traceback or a raw message: both can carry private paths
        print(f'Public check failed: {error_text(error, root, denylist)}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
