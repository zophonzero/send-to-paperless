import subprocess
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from send_to_paperless.notifications import UploadNotice, notify


class NotificationTests(unittest.TestCase):
    @patch("send_to_paperless.notifications.shutil.which", return_value="/usr/bin/notify-send")
    @patch("send_to_paperless.notifications.subprocess.run")
    def test_notifications_expire_escape_markup_and_return_the_replaceable_id(self, run, which):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="123\n", stderr="")
        self.assertEqual(notify("<invoice> & file.pdf", problem=True, replace_id=122), 123)
        args = run.call_args.args[0]
        self.assertIn("--urgency=normal", args)
        self.assertIn("--expire-time=15000", args)
        self.assertIn("--replace-id=122", args)
        self.assertIn("--print-id", args)
        self.assertEqual(args[-1], "&lt;invoice&gt; &amp; file.pdf")

    @patch("send_to_paperless.notifications.shutil.which", return_value="/usr/bin/notify-send")
    @patch("send_to_paperless.notifications.subprocess.run")
    def test_invalid_ids_or_failed_delivery_are_detected(self, run, which):
        for code, value in ((1, "123"), (0, ""), (0, "not-an-id"), (0, "0"), (0, str(2**32))):
            run.return_value = subprocess.CompletedProcess([], code, stdout=value, stderr="ignored")
            self.assertIsNone(notify("Test"))

    def test_phase_updates_share_an_id_file_and_start_time(self):
        sender = Mock(return_value=123)
        notice = UploadNotice(sender)
        notice.started = "12:34:56"
        notice.start([Path("/tmp/Invoice.pdf")])
        notice.processing(1)
        notice.finish([{"filename": "Invoice.pdf", "status": "success", "document_id": 42}])
        self.assertEqual([c.kwargs["replace_id"] for c in sender.call_args_list], [None, 123, 123])
        for call in sender.call_args_list:
            self.assertIn("12:34:56", call.kwargs["summary"])
            self.assertIn("Invoice.pdf", call.args[0])
        self.assertTrue(sender.call_args_list[0].kwargs["transient"])
        self.assertFalse(sender.call_args_list[-1].kwargs["transient"])
        self.assertIn("Imported", sender.call_args.kwargs["summary"])
        self.assertTrue(notice.delivered)

    def test_a_later_success_has_no_previous_duplicate_text_or_notification_id(self):
        sender = Mock(side_effect=[101, 101, 202, 202])
        failed = UploadNotice(sender)
        failed.start([Path("Old.pdf")])
        failed.finish([{"filename": "Old.pdf", "status": "failed", "error_code": "duplicate", "error": "Duplicate: document 42 exists."}])
        success = UploadNotice(sender)
        success.start([Path("New.pdf")])
        success.finish([{"filename": "New.pdf", "status": "success", "document_id": 43}])
        self.assertIsNone(sender.call_args_list[2].kwargs["replace_id"])
        self.assertEqual(sender.call_args.kwargs["replace_id"], 202)
        self.assertNotIn("Duplicate", sender.call_args.args[0])
        self.assertNotIn("Old.pdf", sender.call_args.args[0])
        self.assertIn("New.pdf", sender.call_args.args[0])

    def test_batch_notification_prioritizes_failure_reasons_over_successes(self):
        sender = Mock(return_value=123)
        notice = UploadNotice(sender)
        results = [{"filename": f"ok{i}.pdf", "status": "success"} for i in range(5)]
        results.append({"filename": "duplicate.pdf", "status": "failed", "error": "Duplicate: document 42 exists."})
        notice.finish(results)
        self.assertIn("Import incomplete", sender.call_args.kwargs["summary"])
        self.assertIn("duplicate.pdf: Duplicate", sender.call_args.args[0])
        self.assertIn("5 imported, 1 failed", sender.call_args.args[0])

    def test_failed_final_delivery_does_not_reuse_success_of_the_starting_notice(self):
        notice = UploadNotice(Mock(side_effect=[123, None]))
        notice.start([Path("Invoice.pdf")])
        self.assertTrue(notice.delivered)
        notice.fail("Could not connect.")
        self.assertFalse(notice.delivered)
