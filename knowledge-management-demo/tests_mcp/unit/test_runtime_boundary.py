import ast
from pathlib import Path
import unittest


class RuntimeBoundaryTests(unittest.TestCase):
    def test_backend_imports_and_lock_do_not_include_old_runtime_or_model_packages(self):
        root = Path(__file__).resolve().parents[2]
        forbidden = {'demo', 'graphiti_core', 'graphiti', 'openai', 'pydantic_ai', 'litellm',
                     'anthropic', 'sentence_transformers', 'torch', 'transformers', 'ollama'}
        for path in (root/'mcp_backend').rglob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8-sig'))
            for node in ast.walk(tree):
                modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ''] if isinstance(node, ast.ImportFrom) else []
                for module in modules:
                    self.assertNotIn(module.split('.')[0], forbidden, str(path))
        for line in (root/'requirements-mcp.lock.txt').read_text(encoding='utf-8-sig').splitlines():
            package = line.split('==')[0].split('[')[0].strip().replace('-', '_').lower()
            self.assertNotIn(package, forbidden)


if __name__ == '__main__': unittest.main()
