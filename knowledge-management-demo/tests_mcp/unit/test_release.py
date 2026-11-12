from contextlib import contextmanager
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('mcp_release_artifact', ROOT/'deploy/mcp/release_artifact.py')
release = importlib.util.module_from_spec(spec); spec.loader.exec_module(release)
env_spec = importlib.util.spec_from_file_location('mcp_run_env', ROOT/'deploy/mcp/run_env.py')
run_env = importlib.util.module_from_spec(env_spec); env_spec.loader.exec_module(run_env)


class ReleaseTests(unittest.TestCase):
    @contextmanager
    def directory(self):
        parent = (ROOT/'knowledge-management-demo/.mcp-artifacts').resolve()
        parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as name:
            path = Path(name).resolve()
            self.assertTrue(path.is_relative_to(parent))  # Verified before automatic recursive cleanup.
            yield path

    def archive(self, folder, extra=None, bad_hash=False, duplicate=False, link=False):
        files = {name: b'pass\n' for name in release.REQUIRED}
        if extra: files[extra] = b'forbidden'
        manifest = {'format': 1, 'commit': 'a'*40, 'runtime': 'python3.12',
            'files': {n: {'sha256': release.sha(v), 'size': len(v)} for n, v in files.items()}}
        if bad_hash: manifest['files'][next(iter(files))]['sha256'] = '0'*64
        path = folder/'release.tar'
        with tarfile.open(path, 'w') as archive:
            for name, value in {'MCP-MANIFEST.json': json.dumps(manifest).encode(), **files}.items():
                member = tarfile.TarInfo(name); member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
                if duplicate:
                    archive.addfile(member, io.BytesIO(value)); duplicate = False
            if link:
                member = tarfile.TarInfo('link'); member.type = tarfile.SYMTYPE; member.linkname = '/etc/passwd'
                archive.addfile(member)
        return path, release.sha(path.read_bytes())

    def test_valid_extract_requires_matching_commit_hash_and_empty_target(self):
        with self.directory() as folder:
            path, checksum = self.archive(folder)
            release.extract(path, checksum, 'a'*40, folder/'release')
            self.assertEqual(release.verify_installed(folder/'release', 'a'*40, path, checksum)['status'], 'installed_verified')
            with self.assertRaises(ValueError): release.extract(path, checksum, 'a'*40, folder/'release')
            with self.assertRaises(ValueError): release.verify(path, checksum, 'b'*40)
            with self.assertRaises(ValueError): release.verify(path, '0'*64, 'a'*40)
            (folder/'release'/next(iter(release.REQUIRED))).write_text('changed', encoding='utf-8')
            with self.assertRaises(ValueError): release.verify_installed(folder/'release', 'a'*40, path, checksum)
            manifest_path = folder/'release/MCP-MANIFEST.json'
            forged = json.loads(manifest_path.read_text())
            forged['files'][next(iter(release.REQUIRED))] = {'size': 7, 'sha256': release.sha(b'changed')}
            manifest_path.write_text(json.dumps(forged))
            with self.assertRaises(ValueError): release.verify_installed(folder/'release', 'a'*40, path, checksum)

    def test_path_traversal_private_files_and_unlisted_runtime_are_rejected(self):
        for name in ['../outside.py', '/absolute.py', 'knowledge-management-demo/mcp_backend//server.py',
                     'knowledge-management-demo/demo/server.py', 'deploy/mcp/mcp-candidate.env',
                     'knowledge-web-pwa/build/web/index.html']:
            with self.subTest(name=name), self.directory() as folder:
                path, checksum = self.archive(folder, extra=name)
                with self.assertRaises(ValueError): release.verify(path, checksum, 'a'*40)

    def test_member_tampering_duplicates_and_links_are_rejected(self):
        for flags in [{'bad_hash': True}, {'duplicate': True}, {'link': True}]:
            with self.subTest(flags=flags), self.directory() as folder:
                path, checksum = self.archive(folder, **flags)
                with self.assertRaises(ValueError): release.verify(path, checksum, 'a'*40)

    def test_env_file_is_data_without_expansion_or_legacy_keys(self):
        with self.directory() as folder:
            path = folder/'mcp-config.env'
            path.write_text('MCP_CURSOR_SECRET="literal-$HOME-$(no-command)-`no-command`"\nMCP_NEO4J_PASSWORD=literal#hash\n', encoding='utf-8')
            values = run_env.read_env(path)
            self.assertEqual(values['MCP_CURSOR_SECRET'], 'literal-$HOME-$(no-command)-`no-command`')
            self.assertEqual(values['MCP_NEO4J_PASSWORD'], 'literal#hash')
            path.write_text('LLM_API_KEY=forbidden', encoding='utf-8')
            with self.assertRaises(ValueError): run_env.read_env(path)
            path.write_text('MCP_MODE=test\nMCP_MODE=production', encoding='utf-8')
            with self.assertRaises(ValueError): run_env.read_env(path)


if __name__ == '__main__': unittest.main()
