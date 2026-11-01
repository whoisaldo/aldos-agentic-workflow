#!/usr/bin/env python3
"""Deploy published skill releases locally and to the fleet.json sync machines that are reachable."""
import argparse
import datetime
import hashlib
import json
import re
import subprocess
import sys
import zipfile

from fleetlib import ROOT, atomic_write, config_path, encode, load_config, state_dir, this_machine
from portable import export_release
from skills_sync import release_path, sync, sync_lock

RUNTIME_FILES = ('fleet.json', 'fleet.schema.json', 'scripts/fleetlib.py', 'scripts/fleetconfig.py',
                 'scripts/skills_sync.py', 'scripts/portable.py')


def command(argv, *, input=None, timeout=45):
    result = subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ValueError(f'{argv[0]} failed: {(result.stderr or result.stdout)[-1800:].strip()}')
    return result.stdout.strip()


def remote_python(machine):
    return machine.get('python') or ('python' if machine.get('os') == 'windows' else 'python3')


def ssh_python(machine, script):
    return command(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', machine['ssh'], remote_python(machine), '-'], input=script)


def runtime_archive(state):
    # Only executable deployment code and public configuration travel; no private state.
    sources = {name: config_path() if name == 'fleet.json' else ROOT / name for name in RUNTIME_FILES}
    payloads = {name: path.read_bytes() for name, path in sources.items()}
    revision = hashlib.sha256(b''.join(name.encode() + b'\0' + payloads[name] for name in sorted(payloads))).hexdigest()[:20]
    destination = state / 'transfers' / ('skill-sync-runtime-' + revision + '.zip')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        with zipfile.ZipFile(destination, 'x', zipfile.ZIP_DEFLATED) as archive:
            for name, content in payloads.items():
                archive.writestr(name, content)
    return destination, revision, hashlib.sha256(destination.read_bytes()).hexdigest()


def remote_launcher(machine):
    """The fleet launcher kept current on a peer, or None when fleet.json names none for a Windows peer without a work root."""
    if machine.get('launcher'):
        return machine['launcher']
    if machine.get('os') == 'windows':
        return machine['work_root'].rstrip('\\/') + '\\bin\\fleet.cmd' if machine.get('work_root') else None
    return '~/.local/bin/fleet'


def sync_remote(config, name, release, runtime):
    machine = config['machines'][name]
    archive, runtime_revision, runtime_sha = runtime
    # The remote home and state are resolved by the peer's own Python, never by this machine's account paths.
    probe = '''import json,os
from pathlib import Path
def expand(p):
 home=Path(os.environ.get('FLEET_USER_HOME',str(Path.home())))
 return home/p[2:] if p.startswith('~/') else Path(p)
state=Path(os.environ['FLEET_STATE_DIR']) if os.environ.get('FLEET_STATE_DIR') else expand(MACHINE_STATE) if MACHINE_STATE else Path('D:/AgentWork/fleet-state') if os.name=='nt' else expand(CONFIG_STATE)
(state/'transfers').mkdir(parents=True,exist_ok=True)
print(json.dumps({'state':str(state),'release':(state/'releases'/REVISION/'manifest.json').is_file(),'runtime':(state/'sync-runtime'/RUNTIME/'scripts/skills_sync.py').is_file()}))
'''
    probe = substitute(probe, {'MACHINE_STATE': machine.get('state_dir'), 'CONFIG_STATE': config['state_dir'],
                               'REVISION': release.name, 'RUNTIME': runtime_revision})
    remote = json.loads(ssh_python(machine, probe))
    remote_state = remote['state'].replace('\\', '/')
    release_zip = state_dir(config) / 'transfers' / ('skills-' + release.name + '.zip')
    if not release_zip.exists():
        export_release(release, release_zip)
    release_sha = hashlib.sha256(release_zip.read_bytes()).hexdigest()
    for local, exists in ((archive, remote['runtime']), (release_zip, remote['release'])):
        if not exists:
            command(['scp', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', str(local), machine['ssh'] + ':' + remote_state + '/transfers/' + local.name])
    bootstrap = r'''import hashlib,json,os,sys,zipfile
from pathlib import Path,PurePosixPath
state=Path(STATE)
# The peer's fleet code must use the state directory this worker just probed.
os.environ['FLEET_STATE_DIR']=str(state)
runtime=state/'sync-runtime'/RUNTIME
if not runtime.is_dir():
 archive=state/'transfers'/RUNTIME_ZIP
 if hashlib.sha256(archive.read_bytes()).hexdigest()!=RUNTIME_SHA: raise ValueError('Runtime checksum mismatch')
 with zipfile.ZipFile(archive) as z:
  if set(z.namelist())!=set(FILES): raise ValueError('Unexpected runtime files')
  for n in z.namelist():
   p=PurePosixPath(n)
   if p.is_absolute() or '..' in p.parts: raise ValueError('Unsafe runtime archive')
  runtime.mkdir(parents=True)
  z.extractall(runtime)
sys.path.insert(0,str(runtime/'scripts'))
from fleetlib import read_json,atomic_write,encode,expand
from portable import install_release
from skills_sync import sync,status
release=state/'releases'/REVISION
if not release.is_dir(): install_release(state/'transfers'/RELEASE_ZIP,RELEASE_SHA,state)
# A reconnect may find an older latest pointer even though this release was already installed.
atomic_write(state/'latest-release.json',encode({'revision':REVISION}))
config=read_json(runtime/'fleet.json')
report=sync(config,apply=True)
check=status(config)
if check['problems']: raise ValueError('; '.join(check['problems']))
# Update only the fleet CLI launcher, so doctor and sync use the same checked code.
if LAUNCHER:
 launcher=expand(LAUNCHER)
 repo=Path(REPO_ROOT)
 if os.name=='nt':
  text='@echo off\r\nset "FLEET_REPO_ROOT='+str(repo)+'"\r\nset "FLEET_STATE_DIR='+str(state)+'"\r\n"'+sys.executable+'" "'+str(runtime/'scripts/fleetlib.py')+'" %*\r\n'
 else:
  import shlex
  text='#!/bin/sh\nexport FLEET_REPO_ROOT='+shlex.quote(str(repo))+'\nexport FLEET_STATE_DIR='+shlex.quote(str(state))+'\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(runtime/'scripts/fleetlib.py'))+' "$@"\n'
 if not launcher.exists() or launcher.read_text(encoding='utf-8').replace('\r\n','\n')!=text.replace('\r\n','\n'):
  if launcher.exists():
   prior=launcher.read_text(encoding='utf-8')
   if 'fleetlib.py' not in prior or (str(repo) not in prior and str(state/'sync-runtime') not in prior): raise ValueError('Unrecognized fleet launcher; preserved '+str(launcher))
   import time,shutil
   backup=state/'backups'/'fleet-launcher'/str(time.time_ns())
   backup.parent.mkdir(parents=True,exist_ok=True)
   shutil.copy2(launcher,backup)
  atomic_write(launcher,text,mode=0o755)
print(json.dumps({'revision':report['revision'],'profiles':report['profiles'],'changed':len(report['changes']),'problems':check['problems']}))
'''
    values = {'STATE': remote['state'], 'RUNTIME': runtime_revision, 'RUNTIME_ZIP': archive.name,
              'RUNTIME_SHA': runtime_sha, 'FILES': list(RUNTIME_FILES), 'REVISION': release.name,
              'RELEASE_ZIP': release_zip.name, 'RELEASE_SHA': release_sha,
              'REPO_ROOT': machine.get('repo', str(ROOT)), 'LAUNCHER': remote_launcher(machine)}
    return json.loads(ssh_python(machine, substitute(bootstrap, values)))


def substitute(script, values):
    # Replace complete tokens, longest first, so RUNTIME never changes RUNTIME_SHA.
    return re.sub(r'\b(' + '|'.join(sorted(values, key=len, reverse=True)) + r')\b', lambda m: repr(values[m.group()]), script)


def run(config, local_only=False):
    state = state_dir(config)
    settings = config.get('sync', {})
    release, _ = release_path(config)
    results = {}
    with sync_lock(config, 'fleet-sync-worker'):
        local = sync(config, apply=True)
        here = this_machine(config) or settings.get('host', 'local')
        results[here] = {'revision': local['revision'], 'profiles': local['profiles'], 'changed': len(local['changes']), 'problems': []}
        if not local_only:
            runtime = runtime_archive(state)
            for name in settings.get('machines', []):
                try:
                    results[name] = sync_remote(config, name, release, runtime)
                except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                    results[name] = {'error': str(error)}
                    print(f'{name}: {error}', file=sys.stderr)
        atomic_write(state / 'fleet-sync-status.json', encode({'checked_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'machines': results}))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local', action='store_true', help='Update only this machine')
    args = parser.parse_args()
    results = run(load_config(), args.local)
    print(encode(results), end='')
    return int(any('error' in value for value in results.values()))


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
