"""Exercise the real HTTP/multipart path against a local mock server."""

from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from send_to_paperless.api import Client, Multipart, open_document
from send_to_paperless.errors import APIError, AppError

TASK = "12345678-1234-4234-8234-123456789abc"
OTHER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TOKEN = "test-secret-never-log"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.calls.append((self.command, self.path, dict(self.headers), body))
        code, response = self.server.reply
        if code == -1:
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if code in (301, 302, 307, 308):
            self.send_header("Location", self.server.redirect)
        self.end_headers()
        self.wfile.write(response if isinstance(response, bytes) else json.dumps(response).encode())


class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'Invoice with spaces ä "test".pdf'
        self.content = b"%PDF-1.4\n" + b"document bytes" * 20000
        self.path.write_bytes(self.content)
        self.server.calls = []
        self.server.reply = (200, TASK)
        self.server.redirect = "/do-not-forward-token/"
        self.client = Client(f"http://127.0.0.1:{self.server.server_port}/paperless/", TOKEN)

    def test_multipart_roundtrip_original_bytes_filename_tags_and_task_id(self):
        self.assertEqual(self.client.upload(self.path, [7, 8]), TASK)
        method, path, headers, body = self.server.calls[0]
        self.assertEqual((method, path), ("POST", "/paperless/api/documents/post_document/"))
        self.assertEqual(headers["Authorization"], f"Token {TOKEN}")
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertNotIn("Transfer-Encoding", headers)
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {headers['Content-Type']}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body)
        parts = list(message.iter_parts())
        self.assertEqual([part.get_param("name", header="content-disposition") for part in parts], ["tags", "tags", "document"])
        self.assertEqual([part.get_payload(decode=True) for part in parts[:2]], [b"7", b"8"])
        self.assertEqual(parts[-1].get_payload(decode=True), self.content)
        self.assertNotIn(str(self.path.parent), body.decode(errors="replace"))
        self.assertEqual(self.path.read_bytes(), self.content)

    def test_no_redirects_or_auth_forwarding_for_reads_or_uploads(self):
        for method in (lambda: self.client.probe(), lambda: self.client.upload(self.path, [])):
            self.server.calls = []
            self.server.reply = (307, {})
            with self.assertRaisesRegex(APIError, "redirects"):
                method()
            self.assertEqual(len(self.server.calls), 1)

    def test_server_failure_is_uncertain_no_retry_no_secret_or_response_echo(self):
        self.server.reply = (503, {"detail": TOKEN + " private document text"})
        with self.assertRaises(APIError) as caught:
            self.client.upload(self.path, [])
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertNotIn("private document", str(caught.exception))
        self.assertEqual(len(self.server.calls), 1)

    def test_connection_lost_after_receiving_upload_is_uncertain_no_retry(self):
        self.server.reply = (-1, {})
        with self.assertRaises(APIError) as caught:
            self.client.upload(self.path, [])
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(len(self.server.calls), 1)

    def test_rejected_upload_does_not_claim_it_was_accepted(self):
        for code in (400, 401, 403, 413, 429):
            with self.subTest(code=code):
                self.server.reply = (code, {"error": TOKEN})
                with self.assertRaises(APIError) as caught:
                    self.client.upload(self.path, [])
                self.assertFalse(caught.exception.uncertain)
                self.assertNotIn(TOKEN, str(caught.exception))

    def test_invalid_accepted_response_is_uncertain(self):
        for response in ({"task_id": TASK}, "not-a-uuid", b"<html>login</html>"):
            with self.subTest(response=response):
                self.server.reply = (200, response)
                with self.assertRaises(APIError) as caught:
                    self.client.upload(self.path, [])
                self.assertTrue(caught.exception.uncertain)

    def test_task_list_and_paginated_response_and_document_id(self):
        item = {"task_id": TASK, "status": "SUCCESS", "related_document": "42"}
        for response in ([item], {"results": [item]}):
            self.server.reply = (200, response)
            self.assertEqual(self.client.task(TASK), {"status": "success", "document_id": 42, "document_ids": [42]})

    def test_modern_task_api_lowercase_status_related_ids_and_structured_result(self):
        self.server.reply = (200, [{"task_id": TASK, "status": "success", "related_document_ids": [42, 43], "result_data": {"document_id": 42}}])
        self.assertEqual(self.client.task(TASK), {"status": "success", "document_id": 42, "document_ids": [42, 43]})
        self.server.reply = (200, [{"task_id": TASK, "status": "success", "result_data": {"document_id": 42}}])
        self.assertEqual(self.client.task(TASK)["document_ids"], [42])
        self.server.reply = (200, [{"task_id": TASK, "status": "failed", "result_data": {"error": TOKEN}}])
        result = self.client.task(TASK)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn(TOKEN, result["error"])

    def test_unrelated_missing_or_running_tasks_never_claim_success(self):
        for response in ([], [{"task_id": OTHER, "status": "SUCCESS"}], [{"task_id": TASK, "status": "STARTED"}]):
            self.server.reply = (200, response)
            self.assertEqual(self.client.task(TASK), {"status": "pending"})

    def test_failed_task_does_not_echo_server_result(self):
        self.server.reply = (200, [{"task_id": TASK, "status": "FAILURE", "result": TOKEN}])
        result = self.client.task(TASK)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn(TOKEN, result["error"])

    def test_structured_duplicate_identifies_existing_document_without_echoing_diagnostics(self):
        for value in (42, "42"):
            with self.subTest(value=value):
                self.server.reply = (200, [{"task_id": TASK, "status": "failure", "result_data": {
                    "duplicate_of": value, "duplicate_in_trash": False, "error": TOKEN,
                }}])
                result = self.client.task(TASK)
                self.assertEqual(result, {"status": "failed", "error_code": "duplicate",
                    "error": "Duplicate: already exists as document 42 in Paperless.",
                    "duplicate_of": 42, "duplicate_in_trash": False})
                self.assertNotIn(TOKEN, str(result))

    def test_structured_duplicate_in_trash_has_a_distinct_reason(self):
        self.server.reply = (200, [{"task_id": TASK, "status": "failure", "result_data": {
            "duplicate_of": 42, "duplicate_in_trash": True,
        }}])
        result = self.client.task(TASK)
        self.assertEqual(result["error_code"], "duplicate")
        self.assertTrue(result["duplicate_in_trash"])
        self.assertEqual(result["error"], "Duplicate: document 42 already exists in the Paperless trash.")

    def test_invalid_duplicate_ids_and_unstructured_errors_keep_the_safe_fallback(self):
        for data in ({"duplicate_of": True}, {"duplicate_of": 0}, {"duplicate_of": -2},
                     {"duplicate_of": TOKEN}, {"duplicate_of": 2**63},
                     {"error": TOKEN}, TOKEN, None):
            with self.subTest(data=data):
                self.server.reply = (200, [{"task_id": TASK, "status": "FAILURE", "result_data": data, "result": TOKEN}])
                result = self.client.task(TASK)
                self.assertEqual(result["error_code"], "import_failed")
                self.assertNotIn(TOKEN, str(result))
                self.assertNotIn("duplicate_of", result)

    def test_revoked_task_reports_cancellation(self):
        self.server.reply = (200, [{"task_id": TASK, "status": "REVOKED", "result": TOKEN}])
        self.assertEqual(self.client.task(TASK), {"status": "failed", "error_code": "cancelled", "error": "Import was cancelled by Paperless."})

    def test_invalid_task_id_rejected_before_network(self):
        with self.assertRaises(AppError):
            self.client.task("../../anything")
        self.assertEqual(self.server.calls, [])

    def test_file_type_size_and_special_file_validation(self):
        for name, content in (("empty.pdf", b""), ("script.sh", b"hello")):
            path = Path(self.temp.name) / name
            path.write_bytes(content)
            with self.assertRaises(AppError):
                open_document(path)
        directory = Path(self.temp.name) / "directory.pdf"
        directory.mkdir()
        with self.assertRaises((AppError, OSError)):
            open_document(directory)
        pipe = Path(self.temp.name) / "pipe.pdf"
        os.mkfifo(pipe)
        with self.assertRaises(AppError):
            open_document(pipe)

    def test_file_shrinking_during_upload_is_uncertain(self):
        form = Multipart(io.BytesIO(b"short"), "test.pdf", 10, [])
        with self.assertRaises(APIError) as caught:
            b"".join(form)
        self.assertTrue(caught.exception.uncertain)

    def test_filename_cannot_inject_multipart_headers(self):
        form = Multipart(io.BytesIO(b"pdf"), 'file"\r\nContent-Type: evil.pdf', 3, [])
        self.assertNotIn(b'filename="file"\r\n', form.head)
        self.assertIn(b"%22%0D%0A", form.head)

    def test_credentials_with_header_injection_are_rejected(self):
        with self.assertRaises(AppError):
            Client(self.client.server, "token\r\nX-Evil: true")

    def test_environment_proxy_is_disabled(self):
        self.server.reply = (200, [])
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "no_proxy": ""}):
            Client(self.client.server, TOKEN).probe()
        self.assertEqual(len(self.server.calls), 1)
