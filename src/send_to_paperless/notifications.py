"""Per-invocation desktop feedback. No persistent process or shared notification ID."""

from datetime import datetime
from html import escape
from pathlib import Path
import shutil
import subprocess


def text(value) -> str:
    """Keep filenames/server metadata from injecting terminal control sequences."""
    return "".join(c if c.isprintable() else "?" for c in str(value))


def notify(message: str, *, problem: bool = False, summary: str = "Send to Paperless",
           replace_id: int | None = None, transient: bool = False) -> int | None:
    command = shutil.which("notify-send")
    if not command:
        return None
    args = [command, "--app-name=Send to Paperless", "--icon=document-send",
            "--urgency=normal", f"--expire-time={15000 if problem else 8000}", "--print-id"]
    if replace_id is not None:
        args.append(f"--replace-id={replace_id}")
    if transient:
        args.append("--transient")
    args += ["--", text(summary), escape(text(message), quote=False)]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=5, check=False)
        value = result.stdout.strip()
        if result.returncode == 0 and value.isascii() and value.isdigit() and len(value) <= 10:
            number = int(value)
            if 0 < number < 2**32:
                return number
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


class UploadNotice:
    """Replace only this invocation's message; concurrent uploads stay independent."""
    def __init__(self, sender=notify):
        self.sender = sender
        self.notification_id = None
        self.delivered = False
        self.started = datetime.now().astimezone().strftime("%H:%M:%S")
        self.files = []
        self.last_summary = ""
        self.last_message = ""

    def show(self, phase: str, message: str, *, problem: bool = False, transient: bool = False) -> None:
        self.last_summary = f"Send to Paperless — {phase} · {self.started}"
        self.last_message = message
        number = self.sender(message, problem=problem,
                             summary=self.last_summary,
                             replace_id=self.notification_id, transient=transient)
        self.delivered = type(number) is int and 0 < number < 2**32
        if self.delivered:
            self.notification_id = number

    def file_context(self) -> str:
        names = [text(name)[:160] for name in self.files[:3]]
        if len(self.files) > 3:
            names.append(f"+ {len(self.files) - 3} more files")
        return " · ".join(names)

    def start(self, files: list[Path]) -> None:
        self.files = [path.name for path in files]
        self.show("Uploading", self.file_context(), transient=True)

    def processing(self, count: int) -> None:
        self.show("Processing", f"{self.file_context()} — {count} upload(s) accepted. Waiting for Paperless to finish importing.", transient=True)

    def finish(self, results: list[dict]) -> None:
        failed = [item for item in results if item["status"] in {"failed", "uncertain"}]
        pending = [item for item in results if item["status"] == "pending"]
        accepted = [item for item in results if item["status"] == "accepted"]
        if len(results) == 1:
            item = results[0]
            if item.get("error_code") == "duplicate":
                phase = "Duplicate"
            else:
                phase = {"success": "Imported", "failed": "Upload failed", "uncertain": "Upload uncertain",
                         "pending": "Import not yet confirmed", "accepted": "Accepted for processing"}[item["status"]]
        elif failed:
            phase = "Import incomplete"
        elif pending:
            phase = "Import not yet confirmed"
        elif accepted:
            phase = "Accepted for processing"
        else:
            phase = "Imported" if results else "Status checked"
        lines = []
        ordered = sorted(results, key=lambda item: {"failed": 0, "uncertain": 0, "pending": 1, "accepted": 2, "success": 3}[item["status"]])
        if len(results) > 1:
            successes = sum(item["status"] == "success" for item in results)
            lines.append(f"{successes} imported, {len(failed)} failed/uncertain, {len(pending) + len(accepted)} not yet confirmed.")
        for item in ordered[:4]:
            name = text(item.get("filename", item.get("task_id", "Upload")))[:160]
            if item.get("error"):
                detail = text(item["error"])[:240]
            else:
                detail = {"success": "Imported successfully.", "pending": "Still processing; do not upload again.",
                          "accepted": "Upload accepted; import not yet confirmed."}.get(item["status"], "Import failed.")
                if item.get("document_id") is not None:
                    detail += f" Document {item['document_id']}."
            lines.append(f"{name}: {detail}")
        if len(results) > 4:
            lines.append(f"+ {len(results) - 4} more results; check send-to-paperless status.")
        if pending or accepted:
            lines.append("Check later: send-to-paperless status")
        # The notify helper keeps the body on one line for control-character safety.
        self.show(phase, " · ".join(lines) or "No local uploads to check.", problem=bool(failed))

    def fail(self, message: str, *, cancelled: bool = False) -> None:
        context = self.file_context()
        self.show("Cancelled" if cancelled else "Upload failed",
                  f"{context}: {message}" if context else message, problem=True)
