import ast
from unittest.mock import patch

from fixtures import Offline
from hotaru.modules import HmodLoader


class Modules(Offline):
    async def testbundledmanifests(self):
        runtime = self.build()
        files = sorted(runtime.constellations_dir.glob("*.hmod"))
        self.assertEqual(len(files), 5)
        for path in files:
            with self.subTest(module=path.name):
                loaded = HmodLoader().load(path)
                tree = ast.parse(loaded.source)
                functions = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
                self.assertTrue(all("command_" + name in functions for name in loaded.manifest.commands))
                self.assertTrue(runtime._is_kernel_path(path))

    async def testinstalledpath(self):
        runtime = self.build()
        root = self.root / "package" / "hotaru"
        bundled = root / "constellations"
        bundled.mkdir(parents=True)
        with patch("hotaru.runtime.__file__", str(root / "runtime.py")):
            self.assertEqual(runtime.constellations_dir, bundled)
            self.assertTrue(runtime._is_kernel_path(bundled / "kernel.hmod"))
