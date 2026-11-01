#!/usr/bin/env python3
"""Ship verified skill releases without their Mac-specific source directories."""
import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import zipfile

from fleetlib import allow_windows_user, atomic_write, encode, load_config, read_json, state_dir, tree_entries, verify_release


def export_release(source, destination):
    manifest = verify_release(source)
    if source.name != manifest["revision"] or not re.fullmatch(r"[0-9a-f]{20}", source.name):
        raise ValueError("Invalid release directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents accidentally replacing a published package.
    with zipfile.ZipFile(destination, "x", zipfile.ZIP_DEFLATED) as archive:
        for relative, path in tree_entries(source):
            archive.write(path, source.name + "/" + relative)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    atomic_write(destination.with_suffix(destination.suffix + ".sha256"), digest + "\n")
    return digest


def install_release(archive_path, checksum, state):
    if hashlib.sha256(archive_path.read_bytes()).hexdigest() != checksum:
        raise ValueError("Release archive checksum mismatch")
    releases = state / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".incoming-", dir=releases))
    try:
        with zipfile.ZipFile(archive_path) as archive:
            names = set()
            revisions = set()
            for item in archive.infolist():
                name = PurePosixPath(item.filename)
                if name.is_absolute() or ".." in name.parts or "\\" in item.filename or ":" in item.filename or len(name.parts) < 2:
                    raise ValueError("Unsafe archive path")
                if not re.fullmatch(r"[0-9a-f]{20}", name.parts[0]):
                    raise ValueError("Invalid archive release id")
                if stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError("Release archives cannot contain symlinks")
                if item.filename in names:
                    raise ValueError("Duplicate archive entry")
                names.add(item.filename)
                revisions.add(name.parts[0])
            if len(revisions) != 1:
                raise ValueError("An archive must contain one release")
            archive.extractall(staging)
            revision = revisions.pop()
        source = staging / revision
        manifest = verify_release(source)
        if manifest["revision"] != revision:
            raise ValueError("Archive and manifest release ids differ")
        target = releases / revision
        if target.exists():
            if verify_release(target) != manifest:
                raise ValueError("Existing release differs; preserved it")
        else:
            os.rename(source, target)
            for path in target.rglob("*"):
                if path.is_file():
                    path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
            if os.name != "nt":
                for path in sorted(target.rglob("*"), reverse=True):
                    if path.is_dir():
                        path.chmod(0o555)
                target.chmod(0o555)
        allow_windows_user(target)
        atomic_write(state / "latest-release.json", encode({"revision": revision}))
        return target
    finally:
        shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    export = sub.add_parser("export")
    export.add_argument("--release")
    export.add_argument("--output", type=Path, required=True)
    install = sub.add_parser("install")
    install.add_argument("--archive", type=Path, required=True)
    install.add_argument("--sha256", required=True)
    args = parser.parse_args()
    state = state_dir(load_config())
    if args.action == "export":
        revision = args.release or read_json(state / "latest-release.json")["revision"]
        if not re.fullmatch(r"[0-9a-f]{20}", revision):
            raise ValueError("Invalid release id")
        print(export_release(state / "releases" / revision, args.output))
    else:
        print(install_release(args.archive, args.sha256, state))


if __name__ == "__main__":
    main()
