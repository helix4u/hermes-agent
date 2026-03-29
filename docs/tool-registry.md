# Tool registry

Living registry for shell/tool availability and shell-specific nuances discovered while working in this repo. Keep entries terse and practical.

## Windows host

- `cmd.exe`: Preferred default auto-fallback shell for Windows local execution.
- `PowerShell`: Secondary Windows shell for object-aware inspection and admin workflows.
- `WSL`: Third fallback when available; best for POSIX tools and Linux docs.

## Discovery workflow

- `cmd`: `where.exe TOOL`
- `PowerShell`: `Get-Command TOOL`
- `WSL/POSIX`: `command -v TOOL`
- Help probes:
  - `TOOL --help`
  - `TOOL -h`
  - `man TOOL` inside WSL/POSIX when available

## Notes

- Add new entries here when you discover a non-standard executable, shell quirk, or path rule that future sessions should remember.
- If the finding affects user-visible behavior, also add it to `CHANGELOG.md`.
- If it creates follow-up work, add it to `docs/todo.md`.
