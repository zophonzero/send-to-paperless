#!/usr/bin/env python3
"""Per-user installation of the CLI and Flea action; no services or shell plugin."""

import argparse
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile

MARKER = "# Managed by Send to Paperless."
APP_MARKER = ".send-to-paperless-managed"
SOURCE = Path(__file__).resolve().parent


def generated(path: Path) -> bool:
    return not path.is_symlink() and path.is_file() and MARKER in path.read_text(encoding="utf-8").splitlines()[:3]


def executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp = tempfile.mkstemp(prefix=".send-to-paperless-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), 0o755)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install or remove Send to Paperless for the current user.")
    parser.add_argument("--prefix", type=Path, help="Installation prefix (default: ~/.local; data respects XDG_DATA_HOME)")
    parser.add_argument("--config-home", type=Path, help="Configuration base (default: XDG_CONFIG_HOME or ~/.config)")
    parser.add_argument("--uninstall", action="store_true", help="Remove the app and Flea action; keep credentials and settings")
    args = parser.parse_args(argv)
    prefix = (args.prefix or Path.home() / ".local").expanduser().absolute()
    config_home = (args.config_home or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))).expanduser().absolute()
    data_home = prefix / "share" if args.prefix else Path(os.environ.get("XDG_DATA_HOME", prefix / "share"))
    data = data_home.expanduser().absolute() / "send-to-paperless"
    launcher = prefix / "bin/send-to-paperless"
    action = config_home / "flea/scripts/Send to Paperless"
    try:
        previous = {}
        # Check every destination before writing any. Unrelated files stay untouched.
        for target in (launcher, action):
            if (target.exists() or target.is_symlink()) and not generated(target):
                raise ValueError(f"Refusing to replace an unrelated file: {target}")
            previous[target] = target.read_text(encoding="utf-8") if target.exists() else None
        if (data.exists() or data.is_symlink()) and (data.is_symlink() or not (data / APP_MARKER).is_file()):
            raise ValueError(f"Refusing to replace an unrelated data directory: {data}")
        if args.uninstall:
            launcher.unlink(missing_ok=True)
            action.unlink(missing_ok=True)
            if data.exists():
                shutil.rmtree(data)
            print("CLI and Flea action removed. Settings, keyring entries, and task receipts were kept.")
            return 0
        if sys.version_info < (3, 11):
            raise ValueError("Python 3.11 or newer is required.")
        interpreter = Path(sys.executable).absolute()
        for dependency in ("secret-tool", "notify-send"):
            if not shutil.which(dependency):
                raise ValueError(f"{dependency} is missing. Install libsecret and libnotify.")
        data.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".send-to-paperless-install-", dir=data.parent) as tmp:
            staged = Path(tmp) / "app"
            staged.mkdir()
            shutil.copytree(SOURCE / "src/send_to_paperless", staged / "send_to_paperless", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            (staged / APP_MARKER).write_text("Send to Paperless\n", encoding="utf-8")
            (staged / "__main__.py").write_text("from send_to_paperless.cli import main\nraise SystemExit(main())\n", encoding="utf-8")
            backup = Path(tmp) / "previous"
            if data.exists():
                os.replace(data, backup)
            try:
                os.replace(staged, data)
                executable(launcher, f'#!/bin/sh\n{MARKER}\nexec {shlex.quote(str(interpreter))} {shlex.quote(str(data))} "$@"\n')
                executable(action, f'#!/bin/sh\n{MARKER}\nexec {shlex.quote(str(launcher))} send --flea -- "$@"\n')
            except BaseException:
                if data.exists():
                    shutil.rmtree(data)
                if backup.exists():
                    os.replace(backup, data)
                for target, content in previous.items():
                    if content is None:
                        if generated(target):
                            target.unlink()
                    elif not target.exists() or target.read_text(encoding="utf-8") != content:
                        executable(target, content)
                raise
        print(f"CLI: {launcher}")
        print(f"Flea: {action}")
        print("Set up your connection: send-to-paperless setup")
        return 0
    except (OSError, ValueError) as exc:
        print(f"Installation: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
