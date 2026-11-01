#!/usr/bin/env python3
"""Install the macOS LaunchAgent that distributes published skill releases. Previews unless --apply is given."""
import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time

from fleetlib import ROOT, atomic_write, load_config, state_dir, this_machine

DEFAULT_LABEL = 'org.sharedfleet.skill-sync'


def launch_agent(config, home=None):
    """The LaunchAgent path and job; label and interval come from fleet.json sync."""
    settings = config.get('sync', {})
    label = settings.get('service_label', DEFAULT_LABEL)
    home = Path(home or Path.home())
    state = state_dir(config)
    job = {'Label': label, 'ProgramArguments': [sys.executable, str(ROOT / 'scripts/sync-fleet.py')],
           'WorkingDirectory': str(ROOT), 'StartInterval': settings.get('interval_seconds', 120), 'RunAtLoad': True,
           'StandardOutPath': str(state / 'logs/skill-sync.log'),
           'StandardErrorPath': str(state / 'logs/skill-sync.error.log'),
           'EnvironmentVariables': {'PATH': str(home / '.local/bin') + ':/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'}}
    if os.environ.get('FLEET_CONFIG'):
        job['EnvironmentVariables']['FLEET_CONFIG'] = os.environ['FLEET_CONFIG']
    return home / 'Library/LaunchAgents' / (label + '.plist'), job


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    config = load_config()
    host = config.get('sync', {}).get('host')
    if sys.platform != 'darwin':
        raise ValueError('The synchronization worker is a macOS LaunchAgent; install it on the sync host')
    if host and this_machine(config) != host:
        raise ValueError(f'fleet.json sync.host is {host}; install the worker there (or set FLEET_MACHINE if this is {host})')
    path, job = launch_agent(config)
    label, interval = job['Label'], job['StartInterval']
    state = state_dir(config)
    domain = 'gui/' + str(os.getuid())
    desired = plistlib.dumps(job)
    print(str(path))
    if not args.apply:
        print(desired.decode())
        return
    (state / 'logs').mkdir(parents=True, exist_ok=True)
    loaded = subprocess.run(['launchctl', 'print', domain + '/' + label], capture_output=True).returncode == 0
    same = path.is_file() and path.read_bytes() == desired
    if not same:
        if path.exists():
            backup = state / 'backups' / 'skill-sync-launchagent'
            backup.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup / (str(time.time_ns()) + '.plist'))
        if loaded:
            subprocess.run(['launchctl', 'bootout', domain + '/' + label], check=True)
            loaded = False
        atomic_write(path, desired.decode(), mode=0o644)
    if not loaded:
        subprocess.run(['launchctl', 'bootstrap', domain, str(path)], check=True)
    print(f'Installed automatic skill synchronization every {interval} seconds.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
