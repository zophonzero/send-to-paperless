"""Local settings/receipts and Secret Service access. No secret file fallback."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from .errors import AppError


def config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "send-to-paperless/config.json"


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "send-to-paperless/tasks"


def server_url(value: str) -> str:
    """Canonical HTTPS base URL, including optional reverse-proxy subpath."""
    try:
        if not isinstance(value, str):
            raise ValueError
        url = urlsplit(value.strip())
        port = url.port
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or url.password is not None or url.query or url.fragment
                or any(c.isspace() or ord(c) < 32 for c in value)
                or "\\" in value or any(p in (".", "..") for p in url.path.split("/"))):
            raise ValueError
        host = url.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{host}]"
        netloc = host if port in (None, 443) else f"{host}:{port}"
        return urlunsplit(("https", netloc, url.path.rstrip("/") + "/", "", ""))
    except (ValueError, UnicodeError):
        raise AppError("Use an HTTPS server URL without credentials, a query, or a fragment.") from None


def valid_task_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise AppError("Invalid task ID; expected a UUID.") from None


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def load_config() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError
        data["server"] = server_url(data["server"])
        tags = data.get("tags", [])
        if not isinstance(tags, list) or any(type(tag) is not int or tag <= 0 for tag in tags):
            raise ValueError
        return {"version": 1, "server": data["server"], "tags": tags}
    except FileNotFoundError:
        raise AppError("First set up your connection in a terminal: send-to-paperless setup") from None
    except (ValueError, KeyError, TypeError):
        raise AppError("Invalid configuration. Run send-to-paperless setup.") from None


def token_valid(token: str) -> str:
    if not token or not token.isascii() or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in token):
        raise AppError("The API token is empty or contains invalid characters.")
    return token


class Keyring:
    def __init__(self):
        self.command = shutil.which("secret-tool")
        if not self.command:
            raise AppError("secret-tool is missing. Install the libsecret package.")

    def _run(self, operation: str, server: str, token: str | None = None) -> subprocess.CompletedProcess:
        args = [self.command, operation]
        if operation == "store":
            args += ["--label=Send to Paperless"]
        args += ["application", "send-to-paperless", "server", server]
        try:
            # Tokens travel through stdin/stdout, never process arguments or logs.
            return subprocess.run(args, input=token, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            raise AppError("The keyring did not respond. Unlock it and try again.") from None
        except OSError:
            raise AppError("Could not access the system keyring.") from None

    def get(self, server: str) -> str:
        result = self._run("lookup", server)
        if result.returncode:
            raise AppError("API token unavailable. Unlock the keyring or run send-to-paperless setup.")
        return token_valid(result.stdout.rstrip("\r\n"))

    def set(self, server: str, token: str) -> None:
        if self._run("store", server, token_valid(token)).returncode:
            raise AppError("Could not save the token in the system keyring.")

    def delete(self, server: str) -> None:
        if self._run("clear", server).returncode:
            raise AppError("Could not remove the token; check the system keyring.")


def save_receipt(receipt: dict) -> None:
    atomic_json(state_dir() / f"{valid_task_id(receipt['task_id'])}.json", receipt)


def load_receipts(server: str) -> list[dict]:
    receipts = []
    for path in state_dir().glob("*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            if item.get("server") == server:
                item["task_id"] = valid_task_id(item["task_id"])
                receipts.append(item)
        except (ValueError, TypeError, KeyError, AttributeError, AppError):
            continue
    return sorted(receipts, key=lambda item: item.get("accepted_at", ""), reverse=True)
