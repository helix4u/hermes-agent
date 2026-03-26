---
name: terminal-shell-orchestration
description: Plan and execute shell-aware work across Windows cmd, PowerShell, WSL, and POSIX terminals while tracking available tools and syntax differences.
version: 1.0.0
author: Hermes Agent
tags: [terminal, windows, powershell, cmd, wsl, shell, tooling]
---

# Terminal shell orchestration

Use when the user wants Hermes to be deliberate about which shell it uses, especially on Windows where `cmd.exe`, PowerShell, and WSL each have different syntax, quoting rules, path rules, and tool availability.

## What this skill is for

- Choosing the right shell for a task instead of assuming one shell fits all.
- Switching per-command shell mode when the terminal tool supports it.
- Discovering what executables are actually available on the current machine.
- Recording non-standard tools and shell quirks in the local registry docs for future sessions.

## Core rules

- Prefer the shell that best matches the task:
  - `cmd` for broad Windows compatibility and classic batch-style commands.
  - `powershell` for object-aware admin / Windows-native inspection work.
  - `wsl` for Linux-first tooling, POSIX shell scripts, `man`, package managers, or `/mnt/...` paths.
- If the user wants mixed-shell work in one request, use per-command terminal shell overrides instead of rewriting the whole session around one shell.
- On Windows, keep syntax and path style aligned with the active shell.
- Before assuming a tool exists, probe for it.

## Discovery checklist

1. Identify the active OS and requested shell preference.
2. Probe tool availability:
   - `cmd`: `where.exe TOOL`
   - PowerShell: `Get-Command TOOL`
   - WSL / POSIX: `command -v TOOL`
3. If the tool is unfamiliar, inspect its built-in help:
   - `TOOL --help`
   - `TOOL -h`
   - `man TOOL` inside WSL / POSIX when available
4. Record durable findings in:
   - `docs/tool-registry.md`
   - `docs/todo.md` if follow-up work is needed

## Windows shell notes

- `cmd`
  - Use Windows paths like `C:\Users\...`
  - Prefer `dir`, `where`, `type`, `set`, `for`, `findstr`
  - Beware `%VAR%` expansion and `^` escaping
- `powershell`
  - Use `Get-ChildItem`, `Get-Command`, `Get-Content`, `Select-String`
  - Prefer `-LiteralPath` for user-controlled paths
  - Beware quoting differences and object-vs-text behavior
- `wsl`
  - Use POSIX tools and `/mnt/c/...` style paths
  - Prefer `bash -lc` semantics when probing manually
  - Good for Linux docs, `man`, package managers, and shell scripts

## Repo helpers

- Repo map: `docs/repo-map.md`
- Cross-session goals: `docs/todo.md`
- Tool registry: `docs/tool-registry.md`
- User-visible history: `CHANGELOG.md`

## Update discipline

- When this skill discovers a durable shell/tool nuance, add a short note to `docs/tool-registry.md`.
- When work starts or finishes, reflect it in `docs/todo.md`.
- When user-visible behavior changes, add an item to `CHANGELOG.md`.
