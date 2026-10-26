from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from send_to_paperless.cli import main, notify
from send_to_paperless.errors import APIError
from send_to_paperless.storage import atomic_json, config_path, load_receipts

SERVER = "https://paperless.example.ts.net/"
TASK = "12345678-1234-4234-8234-123456789abc"
SECOND_TASK = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TOKEN = "not-a-real-secret"


class TerminalOutput(io.StringIO):
    def isatty(self):
        return True


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config"), "XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.path = self.root / "Invoice ä $(never-run).pdf"
        self.path.write_bytes(b"%PDF-1.4\ntest")
        atomic_json(config_path(), {"version": 1, "server": SERVER, "tags": [12]})
        keyring_patch = patch("send_to_paperless.cli.Keyring")
        self.keyring = keyring_patch.start().return_value
        self.addCleanup(keyring_patch.stop)
        self.keyring.get.return_value = TOKEN
        client_patch = patch("send_to_paperless.cli.Client")
        self.client_class = client_patch.start()
        self.addCleanup(client_patch.stop)
        self.client = self.client_class.return_value
        self.client.upload.return_value = TASK
        self.client.task.return_value = {"status": "success", "document_id": 42}

    def invoke(self, args):
        output, errors = io.StringIO(), TerminalOutput()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(args)
        self.assertNotIn(TOKEN, output.getvalue() + errors.getvalue())
        return code, output.getvalue(), errors.getvalue()

    def test_default_wait_requires_success_and_preserves_original(self):
        code, output, _ = self.invoke(["send", "--json", str(self.path)])
        self.assertEqual(code, 0)
        result = json.loads(output)["results"][0]
        self.assertEqual((result["status"], result["document_id"]), ("success", 42))
        self.assertEqual(self.path.read_bytes(), b"%PDF-1.4\ntest")
        self.assertEqual(load_receipts(SERVER)[0]["status"], "success")

    def test_nowait_is_acceptance_and_saves_pending_receipt(self):
        code, output, _ = self.invoke(["--json", "--no-wait", str(self.path)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["results"][0]["status"], "accepted")
        self.client.task.assert_not_called()
        self.assertEqual(load_receipts(SERVER)[0]["status"], "pending")

    def test_timeout_is_pending_not_failed_and_can_resume_without_upload(self):
        self.client.task.return_value = {"status": "pending"}
        code, output, _ = self.invoke(["send", "--json", "--wait-timeout", "0.001", str(self.path)])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output)["results"][0]["status"], "pending")
        self.client.task.return_value = {"status": "success", "document_id": 42}
        code, output, _ = self.invoke(["status", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["results"][0]["status"], "success")
        self.assertEqual(self.client.upload.call_count, 1)

    def test_status_query_connection_error_keeps_import_pending(self):
        self.client.task.side_effect = APIError("Could not connect to Paperless.")
        code, output, _ = self.invoke(["send", "--json", str(self.path)])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output)["results"][0]["status"], "pending")

    def test_status_enriches_an_older_success_receipt_without_reuploading(self):
        self.client.task.return_value = {"status": "success", "document_id": None}
        self.invoke(["send", "--json", str(self.path)])
        self.client.task.return_value = {"status": "success", "document_id": 42, "document_ids": [42]}
        code, output, _ = self.invoke(["status", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["results"][0]["document_ids"], [42])
        self.client.upload.assert_called_once()

    def test_dismissed_task_does_not_erase_confirmed_success(self):
        self.client.task.return_value = {"status": "success", "document_id": None}
        self.invoke(["send", "--json", str(self.path)])
        self.client.task.return_value = {"status": "pending"}
        code, output, _ = self.invoke(["status", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["results"][0]["status"], "success")

    def test_server_processing_failure_returns_failure(self):
        self.client.task.return_value = {"status": "failed", "error": "Import failed."}
        code, output, _ = self.invoke(["send", "--json", str(self.path)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["results"][0]["status"], "failed")

    def test_duplicate_reason_reaches_cli_stderr_notification_and_receipt(self):
        reason = "Duplicate: already exists as document 42 in Paperless."
        self.client.task.return_value = {"status": "failed", "error_code": "duplicate", "error": reason,
            "duplicate_of": 42, "duplicate_in_trash": False}
        with patch("send_to_paperless.cli.notify") as notification:
            code, _, errors = self.invoke(["send", "--notify", str(self.path)])
        self.assertEqual(code, 1)
        # This is also Flea's fallback if notification delivery is unavailable.
        self.assertEqual(errors.strip().splitlines()[-1].strip(), reason)
        self.assertIn(reason, notification.call_args.args[0])
        saved = load_receipts(SERVER)[0]
        self.assertEqual((saved["error_code"], saved["duplicate_of"]), ("duplicate", 42))

    def test_status_refreshes_an_older_generic_failure_without_reuploading(self):
        self.client.task.return_value = {"status": "failed", "error": "Import failed."}
        self.invoke(["send", "--json", str(self.path)])
        self.client.task.return_value = {"status": "failed", "error_code": "duplicate",
            "error": "Duplicate: already exists as document 42 in Paperless.", "duplicate_of": 42}
        code, output, _ = self.invoke(["status", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["results"][0]["duplicate_of"], 42)
        self.client.upload.assert_called_once()
        self.assertEqual(load_receipts(SERVER)[0]["error_code"], "duplicate")

    def test_dismissed_task_does_not_erase_a_previous_failure(self):
        self.client.task.return_value = {"status": "failed", "error": "Import failed."}
        self.invoke(["send", "--json", str(self.path)])
        self.client.task.return_value = {"status": "pending"}
        code, output, _ = self.invoke(["status", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["results"][0]["status"], "failed")

    def test_uncertain_post_is_not_retried_and_is_distinct_from_pending_task(self):
        self.client.upload.side_effect = APIError("Check Paperless before retrying.", uncertain=True)
        code, output, _ = self.invoke(["send", "--json", str(self.path)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["results"][0]["status"], "uncertain")
        self.assertEqual(self.client.upload.call_count, 1)
        self.assertEqual(load_receipts(SERVER), [])

    def test_default_and_explicit_tags_merge_without_duplicates(self):
        self.invoke(["send", "--no-wait", "--tag", "12", "--tag", "13", str(self.path)])
        self.client.upload.assert_called_once_with(self.path, [12, 13])

    def test_duplicate_paths_and_hardlinks_are_uploaded_once(self):
        alias = self.root / "alias.pdf"
        os.link(self.path, alias)
        code, output, _ = self.invoke(["send", "--json", str(self.path), str(alias), str(self.path)])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(output)["results"]), 1)
        self.client.upload.assert_called_once()

    def test_batch_continues_after_a_bad_selection(self):
        code, output, _ = self.invoke(["send", "--json", str(self.root / "missing.pdf"), str(self.path)])
        self.assertEqual(code, 1)
        self.assertEqual([item["status"] for item in json.loads(output)["results"]], ["failed", "success"])

    def test_missing_config_shows_setup_command_and_notifies_once(self):
        config_path().unlink()
        with patch("send_to_paperless.cli.notify") as notification:
            code, _, errors = self.invoke(["send", "--notify", str(self.path)])
        self.assertEqual(code, 1)
        self.assertIn("send-to-paperless setup", errors)
        self.assertEqual(notification.call_count, 2)
        self.assertIn("send-to-paperless setup", notification.call_args.args[0])
        self.client.upload.assert_not_called()

    def test_setup_probes_before_storing_hidden_token(self):
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stderr.isatty", return_value=True), patch("send_to_paperless.cli.getpass.getpass", return_value=TOKEN):
            code, _, _ = self.invoke(["setup", "--server", SERVER, "--tag", "13"])
        self.assertEqual(code, 0)
        self.keyring.set.assert_called_once_with(SERVER, TOKEN)
        self.client.probe.assert_called_once()
        self.assertNotIn(TOKEN, config_path().read_text())
        self.assertEqual(json.loads(config_path().read_text())["tags"], [13])

    def test_failed_setup_leaves_credentials_and_config_unchanged(self):
        before = config_path().read_bytes()
        self.client.probe.side_effect = APIError("Invalid API token.")
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stderr.isatty", return_value=True), patch("send_to_paperless.cli.getpass.getpass", return_value=TOKEN):
            code, _, _ = self.invoke(["setup", "--server", SERVER])
        self.assertEqual(code, 1)
        self.keyring.set.assert_not_called()
        self.assertEqual(config_path().read_bytes(), before)

    def test_doctor_never_uploads_a_document(self):
        code, output, _ = self.invoke(["doctor"])
        self.assertEqual(code, 0)
        self.client.probe.assert_called_once()
        self.client.upload.assert_not_called()
        self.assertIn("No document was uploaded", output)

    def test_terminal_filename_controls_are_not_printed(self):
        path = self.root / "evil\x1b[31m.pdf"
        path.write_bytes(b"%PDF")
        code, output, errors = self.invoke([str(path)])
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", output + errors)

    @patch("send_to_paperless.notifications.shutil.which", return_value="/usr/bin/notify-send")
    @patch("send_to_paperless.notifications.subprocess.run", side_effect=OSError)
    def test_notification_failure_is_nonfatal(self, run, which):
        notify("Finished.")

    def test_flea_duplicate_uses_delivered_notification_without_a_sticky_status_error(self):
        reason = "Duplicate: already exists as document 42 in Paperless."
        self.client.task.return_value = {"status": "failed", "error_code": "duplicate", "error": reason}
        with patch("send_to_paperless.cli.notify", return_value=101) as notification:
            code, _, _ = self.invoke(["send", "--flea", str(self.path)])
        self.assertEqual(code, 0)
        self.assertEqual(load_receipts(SERVER)[0]["status"], "failed")
        self.assertIn("Duplicate", notification.call_args.kwargs["summary"])
        self.assertIn(self.path.name, notification.call_args.args[0])
        self.assertIn(reason, notification.call_args.args[0])
        self.assertEqual(notification.call_args.kwargs["replace_id"], 101)

    def test_flea_success_reports_the_current_file_and_replaces_its_progress_message(self):
        with patch("send_to_paperless.cli.notify", return_value=101) as notification:
            code, _, _ = self.invoke(["send", "--flea", str(self.path)])
        self.assertEqual(code, 0)
        self.assertEqual(notification.call_count, 3)
        self.assertIn("Uploading", notification.call_args_list[0].kwargs["summary"])
        self.assertIn("Processing", notification.call_args_list[1].kwargs["summary"])
        self.assertIn("Imported", notification.call_args_list[2].kwargs["summary"])
        self.assertIn(self.path.name, notification.call_args.args[0])
        self.assertIsNone(notification.call_args_list[0].kwargs["replace_id"])
        self.assertEqual(notification.call_args_list[1].kwargs["replace_id"], 101)
        self.assertEqual(notification.call_args_list[2].kwargs["replace_id"], 101)

    def test_flea_notification_failure_falls_back_to_a_specific_stderr_error(self):
        reason = "Duplicate: already exists as document 42 in Paperless."
        self.client.task.return_value = {"status": "failed", "error_code": "duplicate", "error": reason}
        with patch("send_to_paperless.cli.notify", return_value=None):
            code, _, errors = self.invoke(["send", "--flea", str(self.path)])
        self.assertEqual(code, 1)
        self.assertIn(reason, errors.splitlines()[-1])
        self.assertIn("Desktop notification unavailable", errors.splitlines()[-1])

    def test_flea_pending_result_is_explicit_and_saved_as_pending(self):
        self.client.task.return_value = {"status": "pending"}
        with patch("send_to_paperless.cli.notify", return_value=101) as notification:
            code, _, _ = self.invoke(["send", "--flea", "--wait-timeout", "0.001", str(self.path)])
        self.assertEqual(code, 0)
        self.assertEqual(load_receipts(SERVER)[0]["status"], "pending")
        self.assertIn("Import not yet confirmed", notification.call_args.kwargs["summary"])
        self.assertIn("do not upload again", notification.call_args.args[0])
