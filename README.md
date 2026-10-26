# Send to Paperless

Send PDFs and images to **Paperless-ngx** from the command line or the **Flea**
file manager. Built for Omarchy and Linux.

- Select files in Flea: **Run script → Send to Paperless**.
- Upload one or several files with the same CLI.
- Store your API token in the existing system keyring using Secret Service.
- Use the regular Paperless upload pipeline and check the final import result.
- Get file-specific upload, processing, and result notifications from Flea.
- Run only when invoked: no status bar widget or additional background service.
- Keep your original files.

## Requirements

- Linux and Python **3.11+**.
- `libsecret` (`secret-tool`) and `libnotify` (`notify-send`).
- A running Secret Service, such as GNOME Keyring or a compatible KWallet.
- For the context menu: Flea with support for `~/.config/flea/scripts/`.
  The integration was checked against the installed Flea **0.3.12** interface.
- Paperless-ngx reachable over **HTTPS**, including private Tailscale addresses.
- A Paperless user allowed to add documents and view file tasks. Any tags
  passed during upload must also be available to that user.

## Install

Clone the repository and install for your user, without `sudo`:

```sh
git clone https://github.com/zophonzero/send-to-paperless.git
cd send-to-paperless
python3 install.py
send-to-paperless setup
```

The installer adds `~/.local/bin/send-to-paperless`, copies the app into
`~/.local/share/send-to-paperless`, and installs the Flea action. It respects
`XDG_DATA_HOME` and `XDG_CONFIG_HOME`. Use `--prefix` and `--config-home` for a
staged installation. Make sure `~/.local/bin` is in your `PATH`.
Flea refreshes the script list when you open its menu.

### Upload feedback in Flea

Each invocation reports **Uploading → Processing → Imported**, or a specific
outcome such as **Duplicate**, **Upload failed**, or **Import not yet confirmed**.
Messages include the filename and start time. Each phase replaces that upload's
previous notification while it is available; concurrent uploads remain separate.
Result popups request an 8-second lifetime, or 15 seconds for failures, rather
than remaining pinned indefinitely. Your desktop's notification settings apply.

Flea intentionally keeps script errors until they are dismissed, and successful
scripts cannot clear them. The installed action therefore uses its own desktop
feedback rather than adding a permanent Flea error when the result was delivered.
If desktop feedback is unavailable, the specific outcome is sent to Flea as a
fallback. An old error left by an earlier version can be dismissed once with
**Escape** in Flea.

This project is a **CLI with a Flea integration**, not an `omarchy-shell`
plugin. Install it with the provided installer, rather than `omarchy plugin add`.

### Set up your connection

Create an API token in Paperless under **User menu → My Profile**, then enter
your server URL and token locally:

```sh
send-to-paperless setup --server https://paperless.example.ts.net/
```

The token is entered with echo disabled, verified against the task API, and
saved in your system keyring. It is never stored in a configuration file or
passed through process arguments. A locked keyring may prompt you to unlock it.
There is no automatic plaintext fallback.

`~/.config/send-to-paperless/config.json` contains only your server URL and
optional default tag IDs. Run `setup` again to replace your credentials.
When changing the server URL, the old server's keyring entry is kept.

## Usage

```sh
send-to-paperless Invoice.pdf Scan.jpg
send-to-paperless send --tag 12 -- Invoice.pdf
send-to-paperless send --json -- Invoice.pdf
send-to-paperless send --wait-timeout 600 -- Scan.pdf
send-to-paperless send --no-wait -- Invoice.pdf
send-to-paperless status
send-to-paperless status 12345678-1234-4234-8234-123456789abc
send-to-paperless doctor
```

The `send` command is optional. Use `send -- FILE` for filenames starting with
`-` or matching a command name such as `setup`. Selecting the same file through
multiple paths in one invocation uploads it once. Folders are not traversed.

Accepted extensions: PDF, PNG, JPEG, TIFF, WebP, GIF, BMP, HEIC/HEIF. Paperless
validates the actual content and supported formats; additional image formats
depend on your server installation.

By default, the app waits up to **300 seconds** after submitting all files.
Upload acceptance does not mean import success. If processing takes longer or
the status query fails, the result stays pending. Run `status` later to check
without sending the file again. Without an ID, `status` checks the latest 20
local receipts for the current server. Previously confirmed results use the
local receipt; they do not indicate whether the document has since been deleted.

Task IDs, basenames, server URLs, timestamps, and results are saved as private
files in `~/.local/state/send-to-paperless/tasks/` (or under `XDG_STATE_HOME`).
Receipts contain neither document contents nor API tokens, and are kept until
you remove them.

Exit codes: **0** confirmed success, or acceptance only with `--no-wait`;
**1** failure or uncertain upload; **2** invalid arguments; **3** import not yet
confirmed; **130** cancelled. `--json` returns an object with `version: 1` and
`results`, or `error` for a general error.

## Inbox and workflows

Documents are submitted as multipart uploads to
`POST /api/documents/post_document/`. OCR, matching, and applicable workflows
run on your server. The client does not override titles, document types, or
correspondents.

Paperless treats API uploads and the web UI as the same source. Workflows
restricted to the consumption folder or email do not automatically apply.
Make sure your existing inbox rules cover API uploads. To add an existing inbox
tag explicitly, save its ID during setup:

```sh
send-to-paperless setup --tag 12
```

Repeat `--tag` for multiple tags. Explicit setup tags replace the saved list;
tags passed to `send` add to it. Without tag options, existing defaults are kept.
`setup --clear-tags` clears the defaults. Find tag IDs in the Paperless tag API
or the URL when editing a tag.

Uploads are **never automatically retried**: a disconnected request might
already have started an import. Check Paperless file tasks and documents before
resending an uncertain upload. Duplicate handling depends on your server's
version and configuration.

When Paperless rejects a duplicate and supplies a structured `duplicate_of`
result, the CLI and desktop notification show the existing document ID (also
included in Flea's fallback message). A duplicate in the trash is identified
separately. Duplicates keep CLI exit code **1** because the new file was not
imported. Unknown import errors use a generic message; arbitrary server
diagnostics are not displayed or stored.

The Flea adapter uses a successful process exit only to confirm that its desktop
result was delivered. The actual import status is retained in receipts and in
`send-to-paperless status`. Terminal commands, including `send --notify`, keep
the exit codes documented above.

## Remove credentials or uninstall

```sh
send-to-paperless logout
python3 install.py --uninstall
```

`logout` removes the local token for the configured server. To revoke a token,
replace it in Paperless as well. Uninstalling removes the app and Flea action,
but keeps settings, keyring entries, and task receipts. Unrelated files at the
installation destinations are not overwritten.

## Development

Run directly from a checkout:

```sh
python3 bin/send-to-paperless --help
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Tests use a local HTTP server and a simulated keyring; they do not upload to a
real Paperless instance. A complete live check requires a deliberately chosen
test document to verify import, inbox assignment, workflows, and status reporting.

References: [Paperless API](https://docs.paperless-ngx.com/api/#file-uploads),
[workflows](https://docs.paperless-ngx.com/usage/#workflows),
[libsecret](https://gnome.pages.gitlab.gnome.org/libsecret/).
