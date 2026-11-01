"""Synchronize fleet-owned skill links. Never writes settings, credentials, or histories."""
import contextlib
import datetime as dt
import os
from pathlib import Path
import re
import uuid

from fleetlib import (atomic_write, encode, expand, install_skill_link, profile,
                      read_json, skill_allowed, state_dir, tree_hash, verify_release)


@contextlib.contextmanager
def sync_lock(config, name):
    """OS-owned locks release on exit or reboot; automatic retries never inherit stale leases."""
    path = state_dir(config) / 'locks' / (name + '.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if not stream.tell():
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            lock = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            unlock = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            lock = lambda: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            unlock = lambda: fcntl.flock(stream, fcntl.LOCK_UN)
        try:
            lock()
        except OSError as error:
            raise ValueError('Another synchronization is running: ' + name) from error
        try:
            yield
        finally:
            unlock()


def installed_profiles(config, names=None):
    """Managed profiles by default. Native homes and shared skill roots join only when fleet.json sync opts in."""
    settings = config.get('sync', {})
    selected = names or [n for n, p in config['profiles'].items() if p.get('managed') or settings.get('include_native')]
    homes = [(name, profile(config, name)) for name in selected]
    if names is None:
        for i, root in enumerate(settings.get('shared_homes', [])):
            # A shared root such as ~/.agents is read by several tools, so it gets only portable skills.
            if (expand(root) / 'skills').is_dir():
                homes.append(('shared' if i == 0 else f'shared-{i}', {'home': expand(root), 'driver': 'codex', 'identity': 'shared'}))
    return [(name, p) for name, p in homes if p['home'].is_dir()]


def release_path(config, revision=None):
    state = state_dir(config)
    revision = revision or read_json(state / 'latest-release.json')['revision']
    if not re.fullmatch(r'[0-9a-f]{20}', revision):
        raise ValueError('Invalid release id')
    path = state / 'releases' / revision
    manifest = verify_release(path)
    if manifest['revision'] != revision:
        raise ValueError('Release directory and manifest differ')
    return path, manifest


def owned_source(config, path):
    """Only sealed fleet release links can be changed during live synchronization."""
    if not path.is_symlink() and not (hasattr(path, 'is_junction') and path.is_junction()):
        return None
    source = path.resolve()
    try:
        parts = source.relative_to((state_dir(config) / 'releases').resolve()).parts
    except ValueError:
        return None
    if len(parts) != 3 or parts[1] != 'skills' or parts[2] != path.name:
        return None
    if not re.fullmatch(r'[0-9a-f]{20}', parts[0]):
        return None
    manifest = read_json(source.parent.parent / 'manifest.json')
    if tree_hash(source) != manifest.get('skills', {}).get(path.name):
        raise ValueError(f'Installed release was modified; preserved {path}')
    return source


def fingerprint(path):
    if path.is_symlink():
        return ('link', os.readlink(path))
    if hasattr(path, 'is_junction') and path.is_junction():
        return ('junction', str(path.resolve()))
    return ('occupied',) if path.exists() else None


def plan(config, names=None, revision=None):
    release, manifest = release_path(config, revision)
    changes, problems = [], []
    profiles = installed_profiles(config, names)
    for name, p in profiles:
        for skill, expected in manifest['skills'].items():
            source = release / 'skills' / skill
            path = p['home'] / 'skills' / skill
            before = fingerprint(path)
            allowed = skill_allowed(config, p, source)
            if before is None:
                if allowed:
                    changes.append({'profile': name, 'path': str(path), 'source': str(source), 'before': before})
                continue
            owned = owned_source(config, path)
            if owned is None:
                # Provider-owned and local skills have a different owner, even on a name collision.
                if allowed:
                    problems.append(f'{name}: unmanaged skill occupies {path}; preserved it')
                continue
            if not allowed or tree_hash(owned) != expected:
                changes.append({'profile': name, 'path': str(path), 'source': str(source) if allowed else None, 'before': before})
    return release, profiles, changes, problems


def status(config, names=None):
    release, profiles, changes, problems = plan(config, names)
    for change in changes:
        action = 'missing' if change['before'] is None else 'outdated' if change['source'] else 'excluded'
        problems.append(f"{change['profile']}: {action} skill {Path(change['path']).name}")
    return {'revision': release.name, 'profiles': len(profiles), 'problems': problems}


def sync(config, names=None, revision=None, apply=False):
    release, profiles, changes, problems = plan(config, names, revision)
    report = {'revision': release.name, 'profiles': len(profiles), 'changes': changes, 'problems': problems, 'applied': False}
    if problems or not apply:
        if problems and apply:
            raise ValueError('; '.join(problems))
        return report
    state = state_dir(config)
    with sync_lock(config, 'skills-sync'):
        # Recheck every link before the first write. Existing release files are never edited.
        for entry in changes:
            if fingerprint(Path(entry['path'])) != (tuple(entry['before']) if entry['before'] else None):
                raise ValueError('Skill changed after planning: ' + entry['path'])
        backup = None
        if changes:
            stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = state / 'backups' / ('skill-sync-' + stamp)
            backup.mkdir(parents=True, mode=0o700)
            receipt = {'release': release.name, 'changes': changes, 'complete': False}
            atomic_write(backup / 'manifest.json', encode(receipt))
            for i, entry in enumerate(changes):
                path = Path(entry['path'])
                path.parent.mkdir(parents=True, exist_ok=True)
                old = backup / str(i)
                temporary = path.parent / ('.fleet-skill-' + uuid.uuid4().hex)
                try:
                    if fingerprint(path) != (tuple(entry['before']) if entry['before'] else None):
                        raise ValueError('Skill changed after planning: ' + str(path))
                    if entry['source']:
                        install_skill_link(temporary, Path(entry['source']))
                    if path.is_symlink():
                        # Back up the link, without moving its immutable target or exposing a gap.
                        old.symlink_to(os.readlink(path), target_is_directory=True)
                        if entry['source']:
                            os.replace(temporary, path)
                        else:
                            path.unlink()
                    elif entry['before']:
                        # Windows directory junctions cannot replace another directory atomically.
                        # Keep the old junction for rollback and retain both release directories.
                        os.rename(path, old)
                        try:
                            if entry['source']:
                                os.rename(temporary, path)
                        except BaseException:
                            os.rename(old, path)
                            raise
                    elif entry['source']:
                        os.rename(temporary, path)
                    entry['applied'] = True
                    atomic_write(backup / 'manifest.json', encode(receipt))
                finally:
                    if temporary.is_symlink():
                        temporary.unlink()
                    elif hasattr(temporary, 'is_junction') and temporary.is_junction():
                        os.rmdir(temporary)
            receipt['complete'] = True
            atomic_write(backup / 'manifest.json', encode(receipt))
        previous = state / 'skills-sync.json'
        old_record = read_json(previous) if previous.exists() else {}
        records = old_record.get('profiles', {})
        records.update({str(p['home']): {'name': name, 'revision': release.name} for name, p in profiles})
        atomic_write(previous, encode({'revision': release.name, 'profiles': records,
                                     'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                                     'backup': str(backup) if backup else old_record.get('backup')}))
        report.update(applied=True, backup=str(backup) if backup else None)
    return report
