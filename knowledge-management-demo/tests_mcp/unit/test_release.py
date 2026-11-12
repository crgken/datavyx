from contextlib import contextmanager
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace


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

    def archive(self, folder, extra=None, bad_hash=False, duplicate=False, link=False,data_contract=2,permanent_erasure=False):
        files = {name: b'pass\n' for name in release.REQUIRED}
        if extra: files[extra] = b'forbidden'
        manifest = {'format': 1, 'data_contract':data_contract,'commit': 'a'*40, 'runtime': 'python3.12',
            'files': {n: {'sha256': release.sha(v), 'size': len(v)} for n, v in files.items()}}
        if permanent_erasure is not None:manifest['permanent_erasure']=permanent_erasure
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

    def test_append_only_candidate_cannot_be_a_lifecycle_rollback_target(self):
        with self.directory() as folder:
            path,checksum=self.archive(folder,data_contract=1)
            with self.assertRaises(ValueError):release.verify(path,checksum,'a'*40)

    def test_rollback_cannot_reopen_permanent_erasure_or_assume_an_old_manifest_disables_it(self):
        for flag in (None,True,'false',0):
            with self.subTest(flag=flag),self.directory() as folder:
                path,checksum=self.archive(folder,permanent_erasure=flag)
                with self.assertRaises(ValueError):release.verify(path,checksum,'a'*40)

    def test_recovery_loader_passes_literal_arguments_without_model_environment(self):
        with self.directory() as folder:
            path=folder/'mcp-config.env';path.write_text('MCP_MODE=test\nMCP_CURSOR_SECRET=literal-safe-value\n',encoding='utf-8')
            argv=['run_env','--env',str(path),'--python','python','--backend',str(folder),
                '--action','erasure-audit','--dataset','literal-$(no-command)-`no-command`']
            with patch('sys.argv',argv),patch.dict('os.environ',{'LLM_API_KEY':'must-not-be-inherited'}),patch.object(run_env.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run:
                self.assertEqual(run_env.main(),0)
                command=run.call_args.args[0];env=run.call_args.kwargs['env']
                self.assertIsInstance(command,list);self.assertIn(argv[-1],command)
                self.assertNotIn('LLM_API_KEY',env);self.assertFalse(run.call_args.kwargs.get('shell',False))


if __name__ == '__main__': unittest.main()
