import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from send_to_paperless.errors import AppError
from send_to_paperless.storage import (
    Keyring, atomic_json, config_path, load_config, load_receipts,
    save_receipt, server_url, state_dir,
)

SERVER = "https://paperless.example.ts.net/"
TASK = "12345678-1234-4234-8234-123456789abc"


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(Path(self.temp.name) / "config"),
            "XDG_STATE_HOME": str(Path(self.temp.name) / "state"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_https_normalization_including_subpath_and_ipv6(self):
        self.assertEqual(server_url("https://PAPERLESS.example.ts.net:443"), SERVER)
        self.assertEqual(server_url(SERVER + "paperless/"), SERVER + "paperless/")
        self.assertEqual(server_url("https://[::1]:8443/paperless"), "https://[::1]:8443/paperless/")

    def test_unsafe_server_urls_are_rejected(self):
        for value in ("http://example.org", "https://user:secret@example.org", "https://example.org/?token=secret", "https://example.org/#secret", "https://example.org/../bad", "https://example.org/\n", "https://example.org\\bad", 42, None):
            with self.subTest(value=value), self.assertRaises(AppError):
                server_url(value)

    def test_config_is_atomic_private_and_does_not_contain_a_token(self):
        data = {"version": 1, "server": SERVER, "tags": [12]}
        atomic_json(config_path(), data)
        self.assertEqual(load_config(), data)
        self.assertEqual(stat.S_IMODE(config_path().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(config_path().parent.stat().st_mode), 0o700)
        self.assertEqual(list(config_path().parent.glob(".write-*")), [])

    def test_bad_config_never_produces_a_traceback(self):
        config_path().parent.mkdir(parents=True)
        for data in ([], {"version": 1, "server": None}, {"version": 1, "server": SERVER, "tags": [True]}, {"version": 1, "server": SERVER, "tags": [0]}):
            config_path().write_text(json.dumps(data))
            with self.assertRaises(AppError):
                load_config()

    def test_receipts_are_private_and_scoped_to_server(self):
        receipt = {"task_id": TASK, "server": SERVER, "status": "pending"}
        save_receipt(receipt)
        self.assertEqual(load_receipts(SERVER), [receipt])
        self.assertEqual(load_receipts("https://other.example/"), [])
        self.assertEqual(stat.S_IMODE((state_dir() / f"{TASK}.json").stat().st_mode), 0o600)

    def test_corrupt_receipts_are_ignored(self):
        state_dir().mkdir(parents=True)
        (state_dir() / "bad.json").write_text("not-json")
        (state_dir() / "list.json").write_text("[]")
        self.assertEqual(load_receipts(SERVER), [])

    @patch("send_to_paperless.storage.shutil.which", return_value="/usr/bin/secret-tool")
    @patch("send_to_paperless.storage.subprocess.run")
    def test_secret_is_transferred_only_over_stdin_and_captured_stdout(self, run, which):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        Keyring().set(SERVER, "not-a-real-secret")
        args, kwargs = run.call_args
        self.assertNotIn("not-a-real-secret", args[0])
        self.assertEqual(kwargs["input"], "not-a-real-secret")
        self.assertTrue(kwargs["capture_output"])
        run.return_value = subprocess.CompletedProcess([], 0, stdout="not-a-real-secret\n", stderr="")
        self.assertEqual(Keyring().get(SERVER), "not-a-real-secret")

    @patch("send_to_paperless.storage.shutil.which", return_value="/usr/bin/secret-tool")
    @patch("send_to_paperless.storage.subprocess.run")
    def test_keyring_errors_do_not_echo_diagnostics_or_fall_back_to_files(self, run, which):
        run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="private-error-secret")
        with self.assertRaises(AppError) as caught:
            Keyring().set(SERVER, "not-a-real-secret")
        self.assertNotIn("private-error-secret", str(caught.exception))
        self.assertFalse(config_path().exists())
        run.side_effect = subprocess.TimeoutExpired("secret-tool", 120)
        with self.assertRaisesRegex(AppError, "keyring"):
            Keyring().get(SERVER)
