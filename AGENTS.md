# Repository maintenance

This is a Windows desktop POP3 mail forwarder with a CustomTkinter UI,
independent WeCom and SMTP delivery, and per-user local configuration.

## Privacy and compatibility

- Keep real mailbox accounts, passwords, webhook keys and private server names
  out of source, fixtures, documentation and distributed packages. Use
  `example.test` or `example.com` for test/sample addresses.
- Preserve blank private configuration defaults. Never commit `config.json`,
  local data directories, logs, databases, credentials or build environments.
- Keep Windows DPAPI credentials scoped to the current user. Public builds
  must start with empty account/password fields on a fresh Windows profile.
- Preserve existing configuration compatibility, account identifiers, UIDL
  baselines and per-channel delivery checkpoints. Never reset them silently.
- Sender exclusions override keywords and cancel pending delivery routes.
  Removing an exclusion must not resend previously ignored messages.
- Never delete original POP3 messages.

## Runtime and validation

- Keep network work off the Tk thread. Workers must not call Tk directly.
- Preserve the bounded close deadline and the Windows titlebar startup guard.
- Cancellation must work even when OS socket shutdown does not wake I/O.
  Short polling must preserve fragmented protocol data and configured timeouts.
- Confirm a real blocked send before testing cancellation; fixed payload size
  alone does not prove backpressure on Windows.
- Run `python -X utf8 -m unittest discover -s tests -v` for functional changes.
- Run `python -X utf8 check_startup.py --data-dir .build-smoke` for startup/build
  changes. Verify a packaged EXE with the same checker and `--exe`.
- Native Windows DPAPI and desktop tests must run in Windows CI. Do not skip
  failing Windows tests or relax assertions to obtain a successful build.
- Publish EXE artifacts only after functional tests and packaged startup checks
  succeed. Keep startup failure diagnostics available for investigation.

Use a focused branch and pull request, describe resulting behavior and actual
validation, and avoid unrelated changes. Do not rewrite existing Git history
as part of routine maintenance.
