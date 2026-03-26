# Cross-session todo

Working list for durable goals, in-flight implementation, and recently completed work. Keep statuses current so future sessions can resume cleanly.

## In progress

- None right now.

## Next up

- Expand the remaining docs beyond the README so setup, extension behavior, and control-room workflows are documented in dedicated docs instead of mostly living in code and changelog notes.
- Consider exposing per-command shell selection in more first-class UI affordances beyond the tool schema and runtime config.
- Consider adding a browser-side cron management panel if cron workflows become a frequent extension use case.

## Recently done

- Added the full-page control room with audit events, branch summaries, benchmarks, attachments, voice input, and reply TTS.
- Added repo-local Windows bootstrap scripts (`scripts/bootstrap-windows.ps1` and `scripts/bootstrap-windows.bat`) and documented both the fresh installer path and local-checkout bootstrap path for native Windows use without WSL.
- Removed the control-room recall panel after repeated zero-value searches and confusing UX.
- Synced the README and repo map to the current fork, including native Windows support and the browser sidecar/control-room workflow.
- Replaced the control-room audit-events panel with structured data cards instead of the old trace/list renderer.
- Restored sidepanel/control-room voice input to the single-button offscreen flow, with the visible extension page only handling microphone permission bootstrap when needed.
- Removed the broken raw session trace panel and retired the legacy log-dashboard/setup flow.
- Added provider/model, TTS/STT, terminal, delegation, archive, and date-awareness controls to the options page.
- Swapped Windows local-shell auto fallback to `cmd.exe`, then PowerShell, then WSL.
- Added per-command Windows `shell_mode` support to the terminal tool for shell-aware power-user flows.
- Added a terminal shell orchestration skill plus repo map and tool registry docs.
- Added section-aware reply actions for tagged `ʞᴎiʜƚ` blocks in browser surfaces and Discord listen controls.
