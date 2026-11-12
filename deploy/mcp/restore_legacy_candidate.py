"""Restore a verified stopped-volume backup to a NEW isolated candidate Neo4j only."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import socket
import subprocess
import tarfile
import time


NAME = 'atlasai-mcp-candidate-neo4j'
VOLUME = 'atlasai-mcp-candidate-canonical-20261112'
IMAGE = 'neo4j:5.26-community'
BACKUP_ROOT = Path('/home/ubuntu/atlasai-deploy/backups')


def run(*command):
    return subprocess.check_output(list(command), stderr=subprocess.PIPE).decode()


def validate_backup(folder):
    folder = Path(folder).resolve(strict=True)
    if not folder.is_relative_to(BACKUP_ROOT): raise ValueError('Unexpected backup root.')
    manifest = json.loads((folder/'manifest.json').read_text())
    name = 'atlas-knowledge-demo-neo4j.tar.gz'
    entry = next(e for e in manifest['files'] if e['file'] == name)
    archive = folder/name
    hasher = hashlib.sha256()
    with archive.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''): hasher.update(chunk)
    if hasher.hexdigest() != entry['sha256'] or archive.stat().st_size != entry['bytes']:
        raise ValueError('Backup checksum mismatch.')
    with tarfile.open(archive, 'r:gz') as tar:
        for member in tar.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError('Unsafe backup archive member.')
    return archive, {'created_utc': manifest['created_utc'], 'source_release': manifest['previous_release'],
                     'backup_sha256': entry['sha256'], 'backup_bytes': entry['bytes']}


def restore(folder):
    if os.geteuid() != 0: raise ValueError('Root is required for an isolated volume restore.')
    archive, metadata = validate_backup(folder)
    if subprocess.run(['docker', 'inspect', NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        raise ValueError('Candidate container already exists; verify/reuse it rather than overwrite.')
    if subprocess.run(['docker', 'volume', 'inspect', VOLUME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        raise ValueError('Candidate volume already exists; never overwrite a prior copy.')
    with socket.socket() as sock: sock.bind(('127.0.0.1', 17694))
    run('docker', 'image', 'inspect', IMAGE)  # No implicit image build/pull or production restart.
    run('docker', 'volume', 'create', VOLUME)
    info = json.loads(run('docker', 'volume', 'inspect', VOLUME))[0]
    target = Path(info['Mountpoint']).resolve(strict=True)
    expected = Path('/var/lib/docker/volumes')/VOLUME/'_data'
    if target != expected or any(target.iterdir()): raise ValueError('Restore target is unexpected or nonempty.')
    with tarfile.open(archive, 'r:gz') as tar: tar.extractall(target, filter='data')
    run('docker', 'run', '-d', '--name', NAME, '--restart', 'unless-stopped',
        '--memory', '768m', '--cpus', '1', '-p', '127.0.0.1:17694:7687',
        '--mount', f'type=volume,src={VOLUME},dst=/data',
        '-e', 'NEO4J_AUTH=none', '-e', 'NEO4J_server_memory_pagecache_size=128m',
        '-e', 'NEO4J_server_memory_heap_initial__size=128m', '-e', 'NEO4J_server_memory_heap_max__size=256m', IMAGE)
    for attempt in range(45):
        result = subprocess.run(['docker', 'exec', NAME, 'timeout', '7', 'cypher-shell', 'RETURN 1'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=9)
        if result.returncode == 0:
            return {'status': 'isolated_backup_restored', 'container': NAME, 'bolt_port': 17694,
                    'production_changed': False, **metadata}
        time.sleep(1)
    raise ValueError('Candidate database did not become ready; source and production remain unchanged.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', required=True); parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    try:
        result = restore(args.backup) if args.restore else {'status': 'backup_verified', **validate_backup(args.backup)[1]}
        print(json.dumps(result)); return 0
    except (OSError, ValueError, KeyError, StopIteration, tarfile.TarError, subprocess.SubprocessError):
        print(json.dumps({'error': 'Candidate backup verification/restore failed; production was not changed.'})); return 2


if __name__ == '__main__': raise SystemExit(main())
