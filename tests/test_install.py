import json
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from send_to_paperless import __version__

ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="send to paperless ")
        self.addCleanup(self.temp.cleanup)
        self.prefix = Path(self.temp.name) / "local path's"
        self.config = Path(self.temp.name) / "config path"
        self.launcher = self.prefix / "bin/send-to-paperless"
        self.action = self.config / "flea/scripts/Send to Paperless"
        self.runtime = self.prefix / "share/send-to-paperless"
        # A source install only checks these; no actual desktop process is invoked.
        self.helpers = Path(self.temp.name) / "helpers"
        self.helpers.mkdir()
        for name in ("secret-tool", "notify-send"):
            path = self.helpers / name
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)

    def install(self, *extra):
        return subprocess.run([
            sys.executable, str(ROOT / "install.py"), "--prefix", str(self.prefix),
            "--config-home", str(self.config), *extra,
        ], capture_output=True, text=True, env={**os.environ, "PATH": f"{self.helpers}:{os.environ.get('PATH', '')}"})

    def test_staged_install_update_and_uninstall_preserve_settings(self):
        self.assertEqual(self.install().returncode, 0)
        version = subprocess.run([str(self.launcher), "--version"], capture_output=True, text=True)
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertIn(f"Send to Paperless {__version__}", version.stdout)
        self.assertEqual(self.install().returncode, 0)
        settings = self.config / "send-to-paperless/config.json"
        settings.parent.mkdir()
        settings.write_text('{"keep": true}')
        self.assertEqual(self.install("--uninstall").returncode, 0)
        self.assertFalse(self.launcher.exists())
        self.assertFalse(self.action.exists())
        self.assertFalse(self.runtime.exists())
        self.assertEqual(settings.read_text(), '{"keep": true}')

    def test_unknown_file_collision_aborts_before_installing_anything(self):
        self.action.parent.mkdir(parents=True)
        self.action.write_text("my own action\n")
        result = self.install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("unrelated file", result.stderr)
        self.assertEqual(self.action.read_text(), "my own action\n")
        self.assertFalse(self.runtime.exists())
        self.assertFalse(self.launcher.exists())

    def test_flea_action_preserves_argument_boundaries_and_never_evaluates_paths(self):
        self.assertEqual(self.install().returncode, 0)
        capture = Path(self.temp.name) / "capture.py"
        output = Path(self.temp.name) / "arguments.json"
        capture.write_text("import json, sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n")
        import shlex
        self.launcher.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(capture))} {shlex.quote(str(output))} \"$@\"\n")
        names = ["/tmp/Invoice with spaces.pdf", "/tmp/Ä 'quote'.jpg", "$(touch forbidden).pdf", "--help.pdf"]
        result = subprocess.run([str(self.action), *names], capture_output=True, cwd=self.temp.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.read_text()), ["send", "--flea", "--", *names])
        self.assertFalse((Path(self.temp.name) / "forbidden").exists())

    def test_failed_action_install_rolls_back_the_runtime_and_launcher(self):
        spec = importlib.util.spec_from_file_location("installer", ROOT / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        original = installer.executable

        def fail_action(path, content):
            if path == self.action:
                raise OSError("Simulated action write failure")
            original(path, content)

        with patch.object(installer, "executable", side_effect=fail_action), patch.object(installer.shutil, "which", return_value="/test/helper"), redirect_stderr(io.StringIO()):
            result = installer.main(["--prefix", str(self.prefix), "--config-home", str(self.config)])
        self.assertEqual(result, 1)
        self.assertFalse(self.runtime.exists())
        self.assertFalse(self.launcher.exists())
        self.assertFalse(self.action.exists())
