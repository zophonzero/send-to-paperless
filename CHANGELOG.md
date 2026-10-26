# Changelog

## 0.1.2 — First public release

- Upload PDFs and images to Paperless-ngx from the CLI or Flea's script menu.
- Store API tokens in the system keyring using Secret Service.
- Wait for import results and save local task receipts for later status checks.
- Support both legacy and newer Paperless task response formats.
- Show specific duplicate errors, including duplicates in the trash.
- Show upload, processing, and result notifications with filenames and start times.
- Keep GUI feedback separate from Flea's persistent script errors, with a fallback
  when desktop notification delivery is unavailable.
- Include a per-user installer, uninstaller, and automated tests.
