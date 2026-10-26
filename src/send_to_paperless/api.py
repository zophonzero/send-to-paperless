"""Bounded streaming multipart uploads; authenticated requests never redirect."""

import http.client
import json
import mimetypes
import os
from pathlib import Path
import secrets
import stat
from typing import BinaryIO
from urllib import error, request

from . import __version__
from .errors import APIError, AppError
from .storage import token_valid, valid_task_id

MAX_RESPONSE = 2 * 1024 * 1024
EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".gif", ".bmp", ".heic", ".heif"}


def document_id(value) -> int | None:
    if type(value) is int:
        number = value
    elif isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 20:
        number = int(value)
    else:
        return None
    return number if 0 < number < 2**63 else None


def task_failure(item: dict) -> dict:
    """Translate known structured reasons, never echo arbitrary server text."""
    result = item.get("result_data")
    duplicate = document_id(result.get("duplicate_of")) if isinstance(result, dict) else None
    if duplicate is not None:
        in_trash = result.get("duplicate_in_trash") is True
        message = (f"Duplicate: document {duplicate} already exists in the Paperless trash."
                   if in_trash else f"Duplicate: already exists as document {duplicate} in Paperless.")
        return {"status": "failed", "error_code": "duplicate", "error": message,
                "duplicate_of": duplicate, "duplicate_in_trash": in_trash}
    if str(item.get("status", "")).upper() == "REVOKED":
        return {"status": "failed", "error_code": "cancelled", "error": "Import was cancelled by Paperless."}
    return {"status": "failed", "error_code": "import_failed",
            "error": "Import failed. Check Paperless file tasks for details."}


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Multipart:
    """Read an already-open regular file in chunks, with an exact Content-Length."""
    def __init__(self, stream: BinaryIO, filename: str, size: int, tags: list[int]):
        self.stream = stream
        self.size = size
        self.boundary = "send-to-paperless-" + secrets.token_hex(24)
        # Browsers escape these characters too. Basename only; never a local path.
        filename = filename.replace('"', "%22").replace("\r", "%0D").replace("\n", "%0A").replace("\\", "%5C")
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        fields = "".join(f'--{self.boundary}\r\nContent-Disposition: form-data; name="tags"\r\n\r\n{tag}\r\n' for tag in tags)
        fields += (f'--{self.boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
                   f"Content-Type: {mime}\r\n\r\n")
        self.head = fields.encode("utf-8")
        self.tail = f"\r\n--{self.boundary}--\r\n".encode("ascii")

    @property
    def length(self) -> int:
        return len(self.head) + self.size + len(self.tail)

    def __iter__(self):
        yield self.head
        left = self.size
        while left:
            block = self.stream.read(min(left, 128 * 1024))
            if not block:
                raise APIError("The file changed during upload. Check the import status in Paperless.", uncertain=True)
            left -= len(block)
            yield block
        yield self.tail


def open_document(path: Path) -> tuple[BinaryIO, int]:
    if path.suffix.lower() not in EXTENSIONS:
        raise AppError("Supported files: PDF and images (PNG, JPEG, TIFF, WebP, GIF, BMP, HEIC/HEIF).")
    # O_NONBLOCK prevents a pipe masquerading as a PDF from hanging the process.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
            raise AppError("Select a readable, non-empty regular file.")
        stream = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    return stream, info.st_size


class Client:
    def __init__(self, server: str, token: str):
        self.server = server
        self.token = token_valid(token)
        # Direct connection: private documents and credentials do not use HTTP_PROXY.
        self.opener = request.build_opener(request.ProxyHandler({}), NoRedirect())

    def _json(self, path: str, *, body=None, headers=None, timeout: float = 30):
        uploading = body is not None
        req = request.Request(self.server + path, data=body, headers={
            "Authorization": f"Token {self.token}",
            "Accept": "application/json",
            "User-Agent": f"Send-to-Paperless/{__version__}",
            **(headers or {}),
        })
        try:
            with self.opener.open(req, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise ValueError
                return json.loads(raw)
        except error.HTTPError as exc:
            code = exc.code
            exc.close()
            messages = {
                400: "Paperless rejected the upload. Check the file format, contents, and tag IDs.",
                401: "Invalid API token. Run send-to-paperless setup again.",
                403: "Permission denied. Check Paperless permissions for documents and file tasks.",
                404: "API endpoint not found. Check the server URL and Paperless version.",
                413: "The file exceeds the server's upload limit.",
                429: "Paperless is rate-limiting requests. Check again later.",
            }
            message = messages.get(code, f"Paperless returned HTTP {code}.")
            if 300 <= code < 400:
                message = "The API redirects requests. Configure the final HTTPS server URL."
            uncertain = uploading and code >= 500
            if uncertain:
                message += " Check the import status before uploading again."
            raise APIError(message, uncertain=uncertain) from None
        except (error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, OSError):
            suffix = " The upload may already have arrived; check Paperless before retrying." if uploading else " Check Tailscale, the server URL, and the HTTPS certificate."
            raise APIError("Could not connect to Paperless." + suffix, uncertain=uploading) from None
        except (ValueError, UnicodeError):
            suffix = " Check the import status before uploading again." if uploading else " Check the server URL."
            raise APIError("Paperless returned an invalid API response." + suffix, uncertain=uploading) from None

    def probe(self) -> None:
        data = self._json("api/tasks/?page_size=1")
        if not isinstance(data, list) and not (isinstance(data, dict) and isinstance(data.get("results"), list)):
            raise APIError("The server did not return the expected Paperless task API response.")

    def upload(self, path: Path, tags: list[int]) -> str:
        stream, size = open_document(path)
        with stream:
            form = Multipart(stream, path.name, size, tags)
            result = self._json("api/documents/post_document/", body=form, headers={
                "Content-Type": f"multipart/form-data; boundary={form.boundary}",
                "Content-Length": str(form.length),
            }, timeout=120)
        try:
            return valid_task_id(result)
        except AppError:
            raise APIError("Upload submitted without a valid task ID. Check Paperless before uploading again.", uncertain=True) from None

    def task(self, task_id: str, *, timeout: float = 30) -> dict:
        task_id = valid_task_id(task_id)
        data = self._json(f"api/tasks/?task_id={task_id}", timeout=timeout)
        items = data.get("results") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise APIError("Unexpected Paperless task API response.")
        # Never infer completion from an unrelated task or a successful POST.
        for item in items:
            if isinstance(item, dict) and item.get("task_id") == task_id:
                status = str(item.get("status", "")).upper()
                if status == "SUCCESS":
                    # Older servers expose a scalar; newer task APIs expose a list
                    # and structured result_data (including split-document imports).
                    candidates = item.get("related_document_ids")
                    candidates = candidates if isinstance(candidates, list) else []
                    candidates = [*candidates, item.get("related_document")]
                    result_data = item.get("result_data")
                    if isinstance(result_data, dict):
                        candidates.append(result_data.get("document_id"))
                    documents = []
                    for value in candidates:
                        number = document_id(value)
                        if number is not None and number not in documents:
                            documents.append(number)
                    return {"status": "success", "document_id": documents[0] if documents else None, "document_ids": documents}
                if status in {"FAILURE", "FAILED", "REVOKED"}:
                    return task_failure(item)
                return {"status": "pending"}
        # Tasks can be delayed, dismissed, or hidden by permissions. Absence is not success.
        return {"status": "pending"}
