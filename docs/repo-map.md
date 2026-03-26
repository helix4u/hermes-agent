# Repo map

Fast orientation map for Hermes Agent contributors and future sessions. Keep this short, high-signal, and updated when major ownership or entry points move.

## Core runtime

- `run_agent.py`: Main synchronous conversation loop and tool-call orchestration entry.
- `model_tools.py`: Tool discovery, schema assembly, and function-call dispatch.
- `toolsets.py`: Platform/toolset grouping.
- `hermes_state.py`: Session DB, recall/search, and persistence helpers.

## CLI and config

- `cli.py`: Interactive CLI shell.
- `hermes_cli/main.py`: Top-level `hermes` entrypoint.
- `hermes_cli/config.py`: Default config, env metadata, migrations, Hermes-home helpers.
- `hermes_cli/setup.py`: Guided setup wizard.
- `scripts/bootstrap-windows.ps1`: Repo-local native Windows bootstrap for a cloned checkout.
- `scripts/bootstrap-windows.bat`: `cmd.exe` wrapper for the Windows bootstrap flow.

## Gateway and browser bridge

- `gateway/run.py`: Main gateway loop, browser bridge actions, slash command handlers.
- `gateway/platforms/discord.py`: Discord delivery, slash commands, voice/listen buttons.
- `gateway/browser_bridge.py`: HTTP bridge used by the browser extension.
- `gateway/session.py`: Gateway-side session persistence.

## Browser extension

- `browser-extension/sidepanel.html`: Main in-browser chat sidepanel.
- `browser-extension/sidepanel.js`: Sidepanel session UI, attachments, voice input, reply actions.
- `browser-extension/control-room.html`: Full-page operator console.
- `browser-extension/control-room.js`: Inspect, audit, branch, benchmark, and follow-up UI.
- `browser-extension/options.html`: Extension + runtime config UI.
- `browser-extension/background.js`: Extension state, bridge calls, TTS/STT helpers, capture helpers.

## Terminal stack

- `tools/terminal_tool.py`: Public terminal tool schema and execution path.
- `tools/process_registry.py`: Background-process tracking and PTY spawning.
- `tools/environments/local.py`: Host-shell local execution.
- `tools/environments/shell_utils.py`: Windows shell selection, path conversion, subprocess wrapping.

## Observability and memory

- `tools/session_search_tool.py`: Cross-session recall and date-aware search.
- `agent/trajectory.py`: Trajectory saving helpers.
- `tests/gateway/test_browser_bridge_sidebar_sessions.py`: Focused browser-bridge/control-room regression coverage.

## Cross-session docs

- `docs/todo.md`: Active goals and current status.
- `docs/tool-registry.md`: Discovered tool availability and shell-specific notes.
- `CHANGELOG.md`: User-visible behavior changes.
