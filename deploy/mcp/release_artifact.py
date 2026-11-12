"""Build from a clean pushed Git commit; verify/extract only the MCP allowlist."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile


REQUIRED = {'knowledge-management-demo/mcp_backend/server.py',
            'knowledge-management-demo/mcp_backend/contracts.py',
            'knowledge-management-demo/mcp_backend/neo4j_repository.py',
            'knowledge-management-demo/mcp_backend/__main__.py',
            'knowledge-management-demo/requirements-mcp.lock.txt'}


def allowed(name):
    p = PurePosixPath(name)
    if str(p) != name or p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name:
        return False
    if name in {'knowledge-management-demo/requirements-mcp.lock.txt', 'knowledge-management-demo/mcp-config.env.example'}:
        return True
    if name.startswith(('knowledge-management-demo/mcp_backend/', 'knowledge-management-demo/tests_mcp/')):
        return p.suffix in {'.py', '.md'}
    if name.startswith('knowledge-management-demo/scripts/mcp_'):
        return p.suffix == '.py'
    if name.startswith('deploy/mcp/') and len(p.parts) == 3:
        return (p.suffix in {'.py', '.sh', '.service', '.conf', '.html'}
                or p.name.endswith('.env.example') or p.name == 'README.md')
    return False


def sha(data): return hashlib.sha256(data).hexdigest()


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.PIPE)


def build(root, remote_ref, output):
    commit = git(root, 'rev-parse', 'HEAD').decode().strip()
    if git(root, 'status', '--porcelain').strip():
        raise ValueError('The workspace must be clean before locking an artifact.')
    if not remote_ref.startswith('origin/') or git(root, 'rev-parse', remote_ref).decode().strip() != commit:
        raise ValueError('The candidate must match its already-pushed origin ref.')
    names = git(root, 'ls-tree', '-r', '--name-only', commit).decode('utf-8').splitlines()
    files = {name: git(root, 'show', f'{commit}:{name}') for name in sorted(names) if allowed(name)}
    if not REQUIRED <= files.keys(): raise ValueError('The commit is missing required MCP files.')
    manifest = {'format': 1, 'commit': commit, 'runtime': 'python3.12',
        'files': {name: {'sha256': sha(value), 'size': len(value)} for name, value in files.items()}}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists(): raise ValueError('The artifact already exists; reuse or explicitly choose another path.')
    with tarfile.open(output, 'w') as archive:
        for name, value in {'MCP-MANIFEST.json': json.dumps(manifest, sort_keys=True).encode(), **files}.items():
            member = tarfile.TarInfo(name); member.size = len(value); member.mode = 0o644
            archive.addfile(member, io.BytesIO(value))
    return {'commit': commit, 'sha256': sha(output.read_bytes()), 'files': len(files)}


def verify(archive_path, expected_sha, expected_commit):
    if not re.fullmatch('[0-9a-f]{40}', expected_commit): raise ValueError('An exact Git commit is required.')
    if Path(archive_path).stat().st_size > 32*1024*1024: raise ValueError('Artifact size mismatch.')
    data = Path(archive_path).read_bytes()
    if sha(data) != expected_sha: raise ValueError('Artifact checksum mismatch.')
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        members = archive.getmembers()
        names = [m.name for m in members]
        if len(names) > 1000 or len(names) != len(set(names)) or any(not m.isfile() or m.size > 2*1024*1024 for m in members):
            raise ValueError('Duplicate, linked or oversized archive members are forbidden.')
        if 'MCP-MANIFEST.json' not in names: raise ValueError('Manifest missing.')
        manifest = json.load(archive.extractfile('MCP-MANIFEST.json'))
        if manifest.get('format') != 1 or manifest.get('commit') != expected_commit or manifest.get('runtime') != 'python3.12':
            raise ValueError('Manifest version or commit mismatch.')
        files = manifest.get('files', {})
        if not REQUIRED <= files.keys() or set(names) != set(files) | {'MCP-MANIFEST.json'}:
            raise ValueError('Manifest file set mismatch.')
        values = {}
        for name, entry in files.items():
            if not allowed(name): raise ValueError('An archive member is outside the MCP allowlist.')
            value = archive.extractfile(name).read()
            if len(value) != entry['size'] or sha(value) != entry['sha256']: raise ValueError('A member checksum mismatched.')
            values[name] = value
    return manifest, values


def extract(archive_path, expected_sha, expected_commit, target):
    manifest, values = verify(archive_path, expected_sha, expected_commit)
    target = Path(target).resolve()
    if target.exists() and any(target.iterdir()): raise ValueError('Extraction requires an empty immutable release directory.')
    target.mkdir(parents=True, exist_ok=True)
    for name, value in values.items():
        path = target.joinpath(*PurePosixPath(name).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    (target/'MCP-MANIFEST.json').write_text(json.dumps(manifest, sort_keys=True), encoding='utf-8')
    return {'status': 'verified_extracted', 'commit': expected_commit, 'files': len(values)}


def verify_installed(target, expected_commit, archive_path=None, expected_sha=None):
    target = Path(target).resolve()
    manifest = json.loads((target/'MCP-MANIFEST.json').read_text(encoding='utf-8'))
    if archive_path is None:
        anchor = json.loads((target/'.mcp-artifact.json').read_text(encoding='utf-8'))
        archive_path, expected_sha = anchor['path'], anchor['sha256']
    trusted, _ = verify(archive_path, expected_sha, expected_commit)
    if manifest != trusted: raise ValueError('Installed manifest differs from the trusted artifact.')
    if manifest.get('commit') != expected_commit or not REQUIRED <= manifest.get('files', {}).keys():
        raise ValueError('Installed release identity mismatch.')
    for name, entry in manifest['files'].items():
        path = target.joinpath(*PurePosixPath(name).parts)
        if not allowed(name) or path.is_symlink() or not path.resolve().is_relative_to(target):
            raise ValueError('Installed member path mismatch.')
        if path.stat().st_size != entry['size'] or sha(path.read_bytes()) != entry['sha256']:
            raise ValueError('Installed release drifted from its manifest.')
    return {'status': 'installed_verified', 'commit': expected_commit, 'files': len(manifest['files'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='operation', required=True)
    pack = subs.add_parser('build'); pack.add_argument('--root', default='.'); pack.add_argument('--remote-ref', required=True); pack.add_argument('--output', required=True)
    for name in ('verify', 'extract'):
        command = subs.add_parser(name); command.add_argument('archive'); command.add_argument('--sha256', required=True); command.add_argument('--commit', required=True)
        if name == 'extract': command.add_argument('--target', required=True)
    installed = subs.add_parser('installed'); installed.add_argument('--target', required=True); installed.add_argument('--commit', required=True)
    installed.add_argument('--archive'); installed.add_argument('--sha256')
    args = parser.parse_args()
    try:
        if args.operation == 'build': result = build(args.root, args.remote_ref, args.output)
        elif args.operation == 'installed': result = verify_installed(args.target, args.commit, args.archive, args.sha256)
        elif args.operation == 'extract': result = extract(args.archive, args.sha256, args.commit, args.target)
        else:
            manifest, _ = verify(args.archive, args.sha256, args.commit)
            result = {'status': 'verified', 'commit': manifest['commit'], 'files': len(manifest['files'])}
        print(json.dumps(result)); return 0
    except (ValueError, KeyError, OSError, tarfile.TarError, subprocess.CalledProcessError):
        print(json.dumps({'error': 'Artifact verification/build failed; no release was applied.'})); return 2


if __name__ == '__main__': raise SystemExit(main())
