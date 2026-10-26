"""Interactive setup and the same upload path for terminal and Flea."""

import argparse
from datetime import datetime, timezone
import getpass
import json
import math
import os
from pathlib import Path
import sys
import time

from . import __version__
from .api import Client, open_document
from .errors import APIError, AppError
from .notifications import UploadNotice, notify, text
from .storage import (
    Keyring, atomic_json, config_path, load_config, load_receipts,
    save_receipt, server_url, valid_task_id,
)


def positive_id(value: str) -> int:
    try:
        number = int(value)
        if number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("Expected a positive tag ID.")


def duration(value: str) -> float:
    try:
        number = float(value)
        if number > 0 and math.isfinite(number):
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("Expected a positive number of seconds.")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="send-to-paperless", description="Send to Paperless — upload PDFs and images to Paperless-ngx.",
        epilog="Shortcut: send-to-paperless FILE [FILE ...]",
    )
    root.add_argument("--version", action="version", version=f"Send to Paperless {__version__}")
    sub = root.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", help="Set up your server and API token")
    setup.add_argument("--server", help="HTTPS URL of your Paperless instance")
    setup_tags = setup.add_mutually_exclusive_group()
    setup_tags.add_argument("--tag", type=positive_id, action="append", default=None, help="Default tag ID, e.g. your inbox tag; repeatable")
    setup_tags.add_argument("--clear-tags", action="store_true", help="Clear saved default tags")
    send = sub.add_parser("send", help="Upload PDFs and images (keep original files)")
    send.add_argument("--tag", type=positive_id, action="append", default=[], help="Additional tag ID; repeatable")
    send.add_argument("--no-wait", action="store_true", help="Exit after acceptance, before import is confirmed")
    send.add_argument("--wait-timeout", type=duration, default=300, metavar="SECONDS", help="Maximum wait for import results (default: 300)")
    send.add_argument("--json", action="store_true", help="Machine-readable results")
    send.add_argument("--notify", action="store_true", help="Desktop notification (used by Flea)")
    send.add_argument("--flea", action="store_true", help=argparse.SUPPRESS)
    send.add_argument("files", nargs="+", type=Path)
    status = sub.add_parser("status", help="Check an import, or the latest 20 local uploads without an ID")
    status.add_argument("task_id", nargs="?")
    status.add_argument("--json", action="store_true")
    status.add_argument("--notify", action="store_true")
    sub.add_parser("doctor", help="Check settings, keyring access, and the API connection")
    sub.add_parser("logout", help="Remove the local API token from the keyring")
    return root


def setup(args) -> int:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise AppError("Setup requires a terminal for hidden token input.")
    current = None
    if config_path().exists():
        current = load_config()
    default = current["server"] if current else ""
    value = args.server or input(f"Paperless HTTPS URL{f' [{default}]' if default else ''}: ") or default
    server = server_url(value)
    tags = [] if args.clear_tags else args.tag if args.tag is not None else (current["tags"] if current else [])
    keyring = Keyring()
    # Refuse getpass's echoing fallback on a terminal without echo control.
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            token = getpass.getpass("Paperless API token (input hidden): ")
        except getpass.GetPassWarning:
            raise AppError("Hidden input is unavailable. Use a regular terminal.") from None
    Client(server, token).probe()
    keyring.set(server, token)
    atomic_json(config_path(), {"version": 1, "server": server, "tags": sorted(set(tags))})
    print("Connection verified. API token saved in the system keyring.")
    print("Ready: send-to-paperless FILE.pdf")
    if current and current["server"] != server:
        print("Note: the token for your previous server URL remains in the keyring.")
    return 0


def remember(receipt: dict) -> None:
    try:
        save_receipt(receipt)
    except OSError:
        print(f"Could not save task {receipt['task_id']} locally; keep this ID to check its status.", file=sys.stderr)


def wait_for_tasks(client: Client, receipts: list[dict], timeout: float, *, interval: float = 2) -> None:
    deadline = time.monotonic() + timeout
    pending = list(receipts)
    while pending and time.monotonic() < deadline:
        for item in list(pending):
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                state = client.task(item["task_id"], timeout=min(30, left))
            except APIError as exc:
                item["error"] = str(exc)
                remember(item)
                # A permission/network error does not mean the server import failed.
                pending.remove(item)
                continue
            item.pop("error", None)
            item.update(state)
            if state["status"] != "pending":
                remember(item)
                pending.remove(item)
        if pending:
            time.sleep(min(interval, max(0, deadline - time.monotonic())))


def report(results: list[dict], args) -> int:
    failures = sum(item["status"] in {"failed", "uncertain"} for item in results)
    pending = sum(item["status"] == "pending" for item in results)
    accepted = sum(item["status"] == "accepted" for item in results)
    if args.json:
        print(json.dumps({"version": 1, "results": results}, ensure_ascii=False))
    else:
        labels = {"success": "Imported", "failed": "Failed", "uncertain": "Upload uncertain", "pending": "Not yet confirmed", "accepted": "Accepted, processing pending"}
        for item in results:
            line = f"{labels[item['status']]}: {text(item.get('filename', item.get('task_id', '')))}"
            if item.get("task_id"):
                line += f" · Task {item['task_id']}"
            documents = item.get("document_ids") or ([item["document_id"]] if item.get("document_id") is not None else [])
            if documents:
                label = "Document" if len(documents) == 1 else "Documents"
                line += f" · {label} {', '.join(text(value) for value in documents)}"
            print(line)
            if item.get("error"):
                print("  " + text(item["error"]), file=sys.stderr)
        if pending or accepted:
            print("Check later: send-to-paperless status — do not send submitted files again.")
    if args.notify:
        args.notice.finish(results)
    return 1 if failures else 3 if pending else 0


def send(args) -> int:
    config = load_config()
    client = Client(config["server"], Keyring().get(config["server"]))
    tags = sorted(set(config["tags"] + args.tag))
    results, queued = [], []
    seen = set()
    for original in args.files:
        path = original.expanduser().absolute()
        item = {"filename": path.name, "status": "failed"}
        try:
            # Validate before sending and avoid duplicates within one invocation.
            stream, _ = open_document(path)
            with stream:
                info = os.fstat(stream.fileno())
                identity = (info.st_dev, info.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            task_id = client.upload(path, tags)
            item.update({
                "server": config["server"], "task_id": task_id, "status": "pending",
                "accepted_at": datetime.now(timezone.utc).isoformat(),
            })
            remember(item)
            queued.append(item)
            if not args.json:
                print(f"Submitted: {text(path.name)} · Task {task_id}", flush=True)
        except APIError as exc:
            item.update(status="uncertain" if exc.uncertain else "failed", error=str(exc))
        except AppError as exc:
            item["error"] = str(exc)
        except OSError:
            item["error"] = "Could not read the file. Check the path and read permissions."
        results.append(item)
    if args.no_wait:
        # Keep saved receipts pending so 'status' knows to check the server later.
        for item in queued:
            item["status"] = "accepted"
    else:
        if queued and args.notice:
            args.notice.processing(len(queued))
        wait_for_tasks(client, queued, args.wait_timeout)
    return report(results, args)


def status(args) -> int:
    config = load_config()
    client = Client(config["server"], Keyring().get(config["server"]))
    receipts = load_receipts(config["server"])
    if args.task_id:
        task_id = valid_task_id(args.task_id)
        receipts = [next((item for item in receipts if item["task_id"] == task_id), {
            "server": config["server"], "task_id": task_id, "status": "pending",
        })]
    else:
        receipts = receipts[:20]
    for item in receipts:
        # A completed receipt remains useful even after the server task was dismissed.
        confirmed = item.get("status") == "success"
        failed = item.get("status") == "failed"
        # Refresh older failures once so existing receipts gain the specific reason.
        if (failed and item.get("error_code")) or (confirmed and item.get("document_id") is not None):
            continue
        try:
            state = client.task(item["task_id"])
            if confirmed and state["status"] != "success":
                continue  # An older confirmed receipt must not regress to pending.
            if failed and state["status"] == "pending":
                continue
            item.pop("error", None)
            item.update(state)
        except APIError as exc:
            if confirmed or failed:
                continue
            item.update(status="pending", error=str(exc))
        remember(item)
    return report(receipts, args)


def finish(code: int, args) -> int:
    if not getattr(args, "flea", False):
        return code
    if args.notice.delivered:
        # Flea keeps script errors indefinitely. The GUI adapter reports the
        # result through its own notification; receipts retain the real result.
        return 0
    # Keep Flea's error channel as a fallback if desktop delivery is unavailable.
    print(text(f"{args.notice.last_summary}: {args.notice.last_message} (Desktop notification unavailable.)"), file=sys.stderr)
    return code or 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    known = {"setup", "send", "status", "doctor", "logout", "-h", "--help", "--version"}
    if argv and argv[0] not in known:
        argv.insert(0, "send")
    args = parser().parse_args(argv)
    if getattr(args, "flea", False):
        args.notify = True
    args.notice = UploadNotice(sender=notify) if getattr(args, "notify", False) else None
    try:
        if args.command == "setup":
            return setup(args)
        if args.command == "send":
            if args.notice:
                args.notice.start(args.files)
            return finish(send(args), args)
        if args.command == "status":
            return finish(status(args), args)
        config = load_config()
        keyring = Keyring()
        if args.command == "logout":
            keyring.delete(config["server"])
            print("Local API token removed. The token on the server has not been revoked.")
        else:
            Client(config["server"], keyring.get(config["server"])).probe()
            print(f"Server: {config['server']}")
            print("Keyring and authenticated task API are accessible. No document was uploaded.")
        return 0
    except (AppError, OSError) as exc:
        message = str(exc) if isinstance(exc, AppError) else "Local access failed. Check paths and file permissions."
        print("Send to Paperless: " + text(message), file=sys.stderr)
        if getattr(args, "json", False):
            print(json.dumps({"version": 1, "error": message}, ensure_ascii=False))
        if args.notice:
            args.notice.fail(message)
        return finish(1, args)
    except (KeyboardInterrupt, EOFError):
        message = "Cancelled. Submitted files may still be imported. Check: send-to-paperless status"
        print("\n" + message, file=sys.stderr)
        if args.notice:
            args.notice.fail(message, cancelled=True)
        return finish(130, args)
