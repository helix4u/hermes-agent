# Changelog

All notable user-visible changes to this project are documented here. Agents and developers should skim this file when picking up work after a gap or when debugging “what changed recently.”

## Unreleased

### Authentication

- **Codex re-auth ownership**: Expired OpenAI Codex sessions no longer silently import fresh credentials from `~/.codex` during runtime refresh or status/model checks. Hermes now treats Hermes-owned re-auth as the authoritative recovery path and keeps shared Codex auth import limited to explicit import flows.
- **Codex device auth retry**: Hermes now retries the OpenAI Codex device-code login once with a fresh code when the one-time authorization-code exchange is rejected as consumed/invalid, which avoids the common "already used" dead-end that previously forced a logout/login cycle.
- **Codex status visibility**: `hermes status` now shows whether the current Codex session is Hermes-owned, a migrated shared session, or an explicitly imported shared session.

### Browser sidecar (Chrome extension)

- **Control room**: Upgraded the built-in full-page `control-room.html` into an operator console with live session selection, follow-up chat, image attachments, voice-note transcription, reply TTS playback, audit-event browsing, delegation branch summaries, tool benchmarks, system prompt view, tool inventory, path metadata, and untruncated inspect JSON in one page.
- **Control room compatibility**: When the gateway/browser bridge does not support the newer `/session inspect` action yet, the control room now falls back to transcript-only inspection instead of flashing repeated `Unsupported browser bridge action: inspect` errors.
- **Control room cleanup**: Removed the broken **Raw session trace** panel and fully retired the old log-setup page and saved log-URL flow. The control room is now the built-in audit surface rather than a launcher for an external dashboard.
- **Control room launcher**: Added a top-level **Control room** button beside **Options** in the sidepanel so the full operator console is one click away instead of buried in the settings page.
- **Voice input permission bootstrap**: Sidepanel and control-room voice input are back to a single-button offscreen recording flow. When microphone permission is still missing, the visible extension page now requests access first and then hands recording back to the hidden offscreen recorder, avoiding the janky extra recorder window and the double-stop race it introduced.
- **Audit events panel**: Replaced the old trace-style audit-events renderer with structured data cards so the control room no longer falls back to the old “just lines” look when you inspect session events.
- **Recall panel removed**: Removed the control-room recall panel because it was not returning useful results and repeatedly confused users about whether it was doing a live search.
- **Activity log**: Side panel includes a collapsible **Activity log** showing gateway tool/thinking progress (parity with Discord-style `gateway-progress` lines). Buffer on the server is up to **128** lines per session (`activity_log` / `recent_events` in bridge API).
- **Options**: Added runtime configuration sections for provider/model selection, provider credential helpers, TTS/STT defaults, terminal backend and timeout controls, Windows shell preference, web backend/archive fallback settings, and delegation overrides. The old optional log-dashboard field is gone; **Open control room** is the primary local observability entry point. Existing extension settings for activity log, microphone device, themes, and sidecar prompt controls remain.
- **Reply section actions**: Browser-side replies now expose separate actions for tagged `ʞᴎiʜƚ` blocks versus the main answer content, instead of forcing everything through one copy/read control.

### Gateway

- **Browser bridge progress**: While the agent runs a **browser sidecar** turn, `_run_agent` mirrors each progress line into `_browser_bridge_progress` so the extension poll sees the same stream as messaging platforms (without requiring a `Platform.LOCAL` messaging adapter).
- **Concurrent tool progress**: Concurrent tool batches now emit each tool's completion update as soon as that tool finishes instead of waiting for the whole batch, which keeps sidecar/gateway sessions from looking dead during mixed fast/slow searches.
- **Long-tool heartbeats**: Gateway progress now emits periodic `still running` updates for long-lived tools, so browser sidecar and console users can see that Hermes is alive even when a search or shell task is taking a while.
- **Quiet-mode tool spinners**: Gateway sessions with live tool/thinking progress enabled no longer start the raw quiet-mode tool spinner underneath. This avoids stale animated lines like long-running `read`/`grep`/`patch` spinners lingering after the real progress feed has already moved on.
- **Session inspection**: Browser bridge `/session` `inspect` now returns persisted audit events, derived audit metrics, delegation/branch summaries, and per-tool benchmark rollups in addition to transcript, saved session-log JSON, tool counts, role counts, source details, and Hermes paths.
- **Runtime config bridge**: Browser bridge `/session` now supports `runtime_config_get`, `runtime_config_save`, `runtime_provider_models`, and `recall_search`, allowing extension surfaces to read/write Hermes runtime settings and call the session-recall backend when needed.
- **Optional tool deps**: Gateway/browser-bridge startup no longer hard-fails in environments missing `fal_client`; the image-generation tool now degrades cleanly behind its own requirement checks.
- **Discord listen controls**: Discord replies now expose separate listen buttons for the full message, tagged `ʞᴎiʜƚ` content, and the final answer content, which also improves cron-delivery listen behavior for normal text responses.
- **Discord cron listen parity**: Detached Discord sends now include the same persistent `Listen / ʞᴎiʜƚ / Answer` buttons as normal gateway replies, and the `Answer` button now prefers the `Action / Response:` section when that marker exists.
- **Cron override editing**: The cron job API now allows existing jobs to update or clear per-job `model`, `provider`, and `base_url` overrides, which makes it possible to unpin stale job-specific model routing and fall back to the current default runtime config without recreating the job.
- **Discord cron run/remove autocomplete**: The grouped Discord `/cron run` and `/cron remove` slash commands now register job-id autocomplete on the live subcommands again, so job selection comes back as an actual picker instead of forcing manual ID pastes.
- **Discord model picker cleanup**: Discord `/model` now exposes `provider` as a real dropdown choice list plus a separate autocompleted `name` field, which makes provider-targeted switches like `nous` + `openai/gpt-5.4-mini` much less error-prone than the old single freeform field.
- **Stable explicit provider switches**: `/model` no longer auto-reroutes an explicitly requested provider/model target after parsing it, and provider-native bare tails like `gpt-5.4-mini` now expand to `openai/gpt-5.4-mini` on Nous Portal instead of drifting to another provider.

### Recall and web

- **Date-aware recall**: `session_search` now supports a `date_awareness` mode so callers can explicitly switch relative-date grounding on or off when using the session-recall backend.
- **Archive fallback**: Web config now has a first-class `web.archive_fallback` block, and `web_extract_tool` can try archive services like `archive.today`, `archive.is`, `archive.ph`, or `web.archive.org` first for configured paywalled domains before optionally falling back to the original URL.

### Terminal and docs

- **Installer targeting**: The documented install commands and bundled install scripts target `NousResearch/hermes-agent`, while keeping the Windows-native bootstrap flow and shell-selection improvements.
- **Live tool budget metadata**: The agent now injects a transient tool-use runtime status note on API calls with tools enabled, so the model can see whether tool calls are still usable on the current response, how many follow-up iterations remain, and whether any configured soft tool-call budget is exhausted.
- **Discord progress cleanup**: Discord gateway progress now defaults to action-oriented updates when `display.tool_progress` is unset and suppresses low-signal `thinking` chatter unless you explicitly enable verbose progress, which makes long tool turns much easier to follow.
- **Repeated tool-call guardrail**: The agent now injects a transient same-turn redundancy note when it has already repeated the exact same tool call, nudging the model to reuse the newest result instead of re-running identical `read_file`, `web_search`, `terminal`, or similar calls without changed inputs.
- **Filename-aware search defaults**: `search_files` now auto-infers `target='files'` for filename/glob/full-path patterns like `*.py` or `D:\...\clip.mp4` when the model omits `target`, which avoids the common “content search on a filename” stall during sidecar/browser tasks.
- **Search visibility and deadlines**: `search_files` previews now include mode/path context instead of only the raw pattern, and Windows local-backend searches now return partial results with a timeout warning after 15 seconds instead of sitting silently inside a native filesystem walk.
- **Windows bootstrap scripts**: Added repo-local Windows bootstrap scripts at `scripts/bootstrap-windows.ps1` and `scripts/bootstrap-windows.bat` so an existing checkout can be set up for native Windows development without WSL, plus README coverage for both the fresh installer and local bootstrap path.
- **Windows terminal order**: Windows local shell auto-selection now prefers `cmd.exe`, then PowerShell, then WSL, which is a better default for mainstream Windows installs.
- **Per-command shell override**: The terminal tool now supports a Windows-only `shell_mode` override so advanced flows can deliberately mix `cmd`, PowerShell, and WSL work in one broader task.
- **Windows/WSL localhost warning**: Gateway startup now warns when browser bridge, API server, or live CDP browser settings are still on collision-prone localhost defaults for mixed native-Windows + WSL setups, and the README now documents the split-port recommendation.
- **README sync**: Updated the README to document native Windows usage without WSL and call out the browser sidecar/control-room workflow.
- **Cross-session scaffolding**: Added `docs/repo-map.md`, `docs/todo.md`, `docs/tool-registry.md`, and the new `terminal-shell-orchestration` skill so future sessions have a lighter-weight map of the repo, goals, and shell/tool nuances.

### Skills

- Added bundled skill **`wiki-upkeep`** (`skills/productivity/wiki-upkeep/SKILL.md`) for maintaining local wikis (structure, links, safe edits).
- Added bundled skill **`terminal-shell-orchestration`** (`skills/productivity/terminal-shell-orchestration/SKILL.md`) for shell-aware Windows/POSIX execution planning and tool discovery.
- **Kokoro skill routing**: Added a narrow per-turn routing hint so requests that clearly mention Kokoro voices, the local Kokoro FastAPI service, or Kokoro endpoints are pushed toward the existing `kokoro-tts` skill and built-in `text_to_speech` Kokoro provider before broad web research.
