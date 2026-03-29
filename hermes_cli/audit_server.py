"""Local audit viewer for Hermes sessions and tool/runtime events."""

from __future__ import annotations

import asyncio
import json
import logging
import webbrowser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from hermes_state import SessionDB

try:
    from aiohttp import web
except ImportError:  # pragma: no cover - handled by CLI guard
    web = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

DEFAULT_AUDIT_HOST = "127.0.0.1"
DEFAULT_AUDIT_PORT = 8646

_SCOPE_TO_KINDS = {
    "thinking": {"thinking"},
    "tools": {"tool_start", "tool_finish"},
    "results": {"message"},
    "system": {"system"},
    "cron": {"cron"},
    "delivery": {"delivery"},
    "errors": set(),
}


def _require_aiohttp() -> None:
    if web is None:
        raise RuntimeError("aiohttp is required for `hermes audit serve`. Install messaging/gateway dependencies first.")


def _parse_float(value: Optional[str]) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_int(value: Optional[str], default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _event_matches_scopes(event: Dict[str, Any], scopes: set[str]) -> bool:
    if not scopes:
        return True
    if "errors" in scopes and event.get("is_error"):
        return True
    return event.get("scope") in scopes


def _merge_timeline(transcript: List[Dict[str, Any]], events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged = []
    for row in transcript:
        merged.append(
            {
                "entry_type": "transcript",
                "sort_ts": row.get("ts") or 0,
                **row,
            }
        )
    for event in events:
        merged.append(
            {
                "entry_type": "event",
                "sort_ts": event.get("ts") or 0,
                **event,
            }
        )
    merged.sort(key=lambda item: (item.get("sort_ts") or 0, item.get("id") or 0, item.get("seq") or 0))
    return merged


class HermesAuditServer:
    def __init__(self, db_path: Optional[Path] = None):
        _require_aiohttp()
        self.db = SessionDB(db_path=db_path)

    async def close(self) -> None:
        self.db.close()

    def build_app(self) -> "web.Application":
        app = web.Application()
        app.add_routes(
            [
                web.get("/", self.handle_index),
                web.get("/api/sessions", self.handle_sessions),
                web.get(r"/api/sessions/{session_id}", self.handle_session),
                web.get(r"/api/sessions/{session_id}/transcript", self.handle_transcript),
                web.get(r"/api/sessions/{session_id}/events", self.handle_events),
                web.get(r"/api/sessions/{session_id}/metrics", self.handle_metrics),
                web.get("/api/stream", self.handle_stream),
            ]
        )
        return app

    async def handle_index(self, request: "web.Request") -> "web.Response":
        return web.Response(text=_INDEX_HTML, content_type="text/html")

    async def handle_sessions(self, request: "web.Request") -> "web.Response":
        source = request.query.get("source") or None
        text = request.query.get("text") or None
        status = request.query.get("status") or None
        limit = _parse_int(request.query.get("limit"), 50)
        offset = _parse_int(request.query.get("offset"), 0)
        sessions = self.db.get_audit_session_summary(source=source, text=text, limit=limit, offset=offset)
        if status:
            sessions = [row for row in sessions if row.get("metrics", {}).get("last_status") == status]
        return web.json_response({"sessions": sessions})

    async def handle_session(self, request: "web.Request") -> "web.Response":
        session_id = request.match_info["session_id"]
        session = self.db.get_session(session_id)
        if not session:
            return web.json_response({"error": "session_not_found"}, status=404)
        return web.json_response({"session": session, "metrics": self.db.get_session_metrics(session_id)})

    async def handle_transcript(self, request: "web.Request") -> "web.Response":
        session_id = request.match_info["session_id"]
        if not self.db.get_session(session_id):
            return web.json_response({"error": "session_not_found"}, status=404)
        transcript = self.db.get_transcript_rows(session_id)
        return web.json_response({"transcript": transcript})

    async def handle_events(self, request: "web.Request") -> "web.Response":
        session_id = request.match_info["session_id"]
        if not self.db.get_session(session_id):
            return web.json_response({"error": "session_not_found"}, status=404)

        scopes = {
            part.strip()
            for part in (request.query.get("scopes") or "").split(",")
            if part.strip()
        }
        kinds: set[str] = set()
        for scope in scopes:
            kinds.update(_SCOPE_TO_KINDS.get(scope, set()))
        kinds_list = sorted(kinds) if kinds else None
        events = self.db.get_audit_events(
            session_id,
            kinds=kinds_list,
            tool_name=request.query.get("tool_name") or None,
            status=request.query.get("status") or None,
            text=request.query.get("text") or None,
            min_duration_ms=_parse_int(request.query.get("min_duration_ms"), 0) or None,
            since_ts=_parse_float(request.query.get("from")),
            until_ts=_parse_float(request.query.get("to")),
            limit=_parse_int(request.query.get("limit"), 500),
        )
        if scopes:
            events = [event for event in events if _event_matches_scopes(event, scopes)]
        cursor = events[-1]["id"] if events and events[-1].get("id") is not None else None
        return web.json_response({"events": events, "next_cursor": cursor})

    async def handle_metrics(self, request: "web.Request") -> "web.Response":
        session_id = request.match_info["session_id"]
        if not self.db.get_session(session_id):
            return web.json_response({"error": "session_not_found"}, status=404)
        transcript = self.db.get_transcript_rows(session_id)
        events = self.db.get_audit_events(session_id, limit=5000)
        return web.json_response(
            {
                "metrics": self.db.get_session_metrics(session_id),
                "timeline": _merge_timeline(transcript, events),
            }
        )

    async def handle_stream(self, request: "web.Request") -> "web.StreamResponse":
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await response.prepare(request)
        after_id = _parse_int(request.query.get("after"), 0)
        session_id = request.query.get("session_id") or None

        try:
            while True:
                events = self.db.get_events_since(after_id=after_id, session_id=session_id, limit=100)
                for event in events:
                    after_id = max(after_id, int(event.get("id") or 0))
                    payload = json.dumps(event, ensure_ascii=False)
                    await response.write(f"id: {after_id}\nevent: session_event\ndata: {payload}\n\n".encode("utf-8"))
                await response.write(b": ping\n\n")
                await asyncio.sleep(1.0)
        except (asyncio.CancelledError, ConnectionResetError):
            return response
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Audit SSE stream ended: %s", exc)
            return response


async def _run_server(host: str, port: int, open_browser: bool) -> None:
    server = HermesAuditServer()
    runner = web.AppRunner(server.build_app())
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    url = f"http://{host}:{port}/"
    print(f"Hermes audit viewer running at {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
        await server.close()


def serve_audit_viewer(host: str = DEFAULT_AUDIT_HOST, port: int = DEFAULT_AUDIT_PORT, open_browser: bool = False) -> None:
    _require_aiohttp()
    asyncio.run(_run_server(host=host, port=port, open_browser=open_browser))


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hermes Audit Viewer</title>
  <style>
    :root {
      --bg: #0d1218;
      --panel: #151d24;
      --panel-2: #1d2831;
      --text: #edf3f8;
      --muted: #98a7b5;
      --accent: #72d4c8;
      --warn: #ffb454;
      --error: #ff7a7a;
      --border: #293642;
      --shadow: rgba(0, 0, 0, 0.25);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", "IBM Plex Sans", sans-serif;
      background: radial-gradient(circle at top, #16212b 0, var(--bg) 38%);
      color: var(--text);
    }
    .layout {
      display: grid;
      grid-template-columns: 360px 1fr;
      min-height: 100vh;
    }
    aside, main { padding: 16px; }
    aside {
      border-right: 1px solid var(--border);
      background: linear-gradient(180deg, rgba(26, 37, 47, 0.95), rgba(13, 18, 24, 0.98));
    }
    h1, h2, h3, p { margin: 0; }
    .hero { margin-bottom: 14px; }
    .hero p { color: var(--muted); margin-top: 6px; line-height: 1.45; }
    .controls, .card, .filter-row, .session-row, .timeline-entry, .summary-grid > div {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      box-shadow: 0 8px 30px var(--shadow);
    }
    .controls { padding: 12px; display: grid; gap: 10px; margin-bottom: 14px; }
    .controls input, .controls select, .filters input {
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--panel-2);
      color: var(--text);
      padding: 10px 12px;
    }
    .session-list { display: grid; gap: 10px; max-height: calc(100vh - 210px); overflow: auto; padding-right: 4px; }
    .session-row { padding: 12px; cursor: pointer; transition: transform .12s ease, border-color .12s ease; }
    .session-row:hover, .session-row.active { transform: translateY(-1px); border-color: var(--accent); }
    .session-row small, .muted { color: var(--muted); }
    .session-meta, .session-stats, .filter-row { display: flex; gap: 10px; flex-wrap: wrap; }
    .session-stats span, .badge {
      background: rgba(114, 212, 200, 0.12);
      border: 1px solid rgba(114, 212, 200, 0.24);
      padding: 4px 8px;
      border-radius: 999px;
      font-size: 12px;
    }
    .topbar { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; margin-bottom: 14px; }
    .live-indicator { padding: 8px 12px; border-radius: 999px; border: 1px solid var(--border); background: rgba(114, 212, 200, 0.08); color: var(--accent); }
    .summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }
    .summary-grid > div { padding: 14px; }
    .summary-grid strong { display: block; font-size: 26px; margin-top: 8px; }
    .filters { display: grid; gap: 12px; margin-bottom: 16px; }
    .filter-row { padding: 12px; }
    .toggle-list { display: flex; gap: 8px; flex-wrap: wrap; }
    .toggle-list label {
      display: inline-flex; gap: 6px; align-items: center; background: var(--panel-2);
      padding: 8px 10px; border-radius: 999px; border: 1px solid var(--border);
      font-size: 13px;
    }
    .timeline { display: grid; gap: 12px; }
    .timeline-entry { padding: 14px; }
    .timeline-entry h3 { font-size: 15px; display: flex; gap: 8px; align-items: center; }
    .timeline-entry pre {
      white-space: pre-wrap; word-break: break-word; background: rgba(5, 9, 14, 0.55);
      padding: 12px; border-radius: 12px; color: #dce8f3; overflow: auto;
    }
    .entry-meta { display: flex; gap: 8px; flex-wrap: wrap; margin: 8px 0 10px; color: var(--muted); font-size: 13px; }
    .entry-error { border-color: rgba(255, 122, 122, 0.5); }
    .empty {
      min-height: 300px; display: grid; place-items: center; color: var(--muted);
      border: 1px dashed var(--border); border-radius: 18px; background: rgba(255,255,255,0.02);
    }
    @media (max-width: 1100px) {
      .layout { grid-template-columns: 1fr; }
      aside { border-right: none; border-bottom: 1px solid var(--border); }
      .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 700px) {
      .summary-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="layout">
    <aside>
      <div class="hero">
        <h1>Hermes Audit Viewer</h1>
        <p>Browse sessions, inspect tool lifecycles, and watch live runs update in real time.</p>
      </div>
      <div class="controls">
        <input id="sessionSearch" placeholder="Search sessions, titles, IDs" />
        <select id="sourceFilter">
          <option value="">All sources</option>
          <option value="cli">CLI</option>
          <option value="discord">Discord</option>
          <option value="telegram">Telegram</option>
          <option value="cron">Cron</option>
          <option value="api_server">API Server</option>
        </select>
        <select id="statusFilter">
          <option value="">Any status</option>
          <option value="ok">OK</option>
          <option value="error">Error</option>
          <option value="running">Running</option>
        </select>
      </div>
      <div id="sessionList" class="session-list"></div>
    </aside>
    <main>
      <div class="topbar">
        <div>
          <h2 id="sessionTitle">Select a session</h2>
          <p id="sessionSubtitle" class="muted">Choose a session on the left to inspect transcript, tools, and live runtime events.</p>
        </div>
        <div id="liveIndicator" class="live-indicator">Live stream idle</div>
      </div>
      <div class="summary-grid">
        <div><span class="muted">Runtime</span><strong id="runtimeValue">—</strong></div>
        <div><span class="muted">Tool Calls</span><strong id="toolValue">—</strong></div>
        <div><span class="muted">Errors</span><strong id="errorValue">—</strong></div>
        <div><span class="muted">Last Status</span><strong id="statusValue">—</strong></div>
      </div>
      <div class="filters">
        <div class="filter-row toggle-list" id="scopeToggles"></div>
        <div class="filter-row">
          <input id="detailSearch" placeholder="Filter timeline text / tool names / payload content" />
        </div>
        <div class="filter-row">
          <input id="toolNameFilter" placeholder="Exact tool name filter (optional)" />
        </div>
      </div>
      <div id="timeline" class="timeline">
        <div class="empty">No session selected yet.</div>
      </div>
    </main>
  </div>
  <script>
    const scopeOptions = ["thinking", "tools", "results", "system", "cron", "delivery", "errors"];
    const state = {
      sessions: [],
      selectedSessionId: null,
      selectedSession: null,
      timeline: [],
      liveSource: null,
      scopeSet: new Set(scopeOptions),
    };

    const els = {
      sessionList: document.getElementById("sessionList"),
      sessionSearch: document.getElementById("sessionSearch"),
      sourceFilter: document.getElementById("sourceFilter"),
      statusFilter: document.getElementById("statusFilter"),
      detailSearch: document.getElementById("detailSearch"),
      toolNameFilter: document.getElementById("toolNameFilter"),
      sessionTitle: document.getElementById("sessionTitle"),
      sessionSubtitle: document.getElementById("sessionSubtitle"),
      liveIndicator: document.getElementById("liveIndicator"),
      runtimeValue: document.getElementById("runtimeValue"),
      toolValue: document.getElementById("toolValue"),
      errorValue: document.getElementById("errorValue"),
      statusValue: document.getElementById("statusValue"),
      timeline: document.getElementById("timeline"),
      scopeToggles: document.getElementById("scopeToggles"),
    };

    function fmtMs(ms) {
      if (ms == null) return "—";
      if (ms < 1000) return `${ms}ms`;
      return `${(ms / 1000).toFixed(ms >= 10000 ? 0 : 1)}s`;
    }

    function fmtTime(ts) {
      if (!ts) return "—";
      return new Date(ts * 1000).toLocaleString();
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }

    function buildScopeToggles() {
      els.scopeToggles.innerHTML = "";
      for (const scope of scopeOptions) {
        const label = document.createElement("label");
        const input = document.createElement("input");
        input.type = "checkbox";
        input.checked = true;
        input.addEventListener("change", () => {
          if (input.checked) state.scopeSet.add(scope);
          else state.scopeSet.delete(scope);
          loadTimeline();
        });
        label.append(input, document.createTextNode(scope));
        els.scopeToggles.appendChild(label);
      }
    }

    async function fetchJson(path) {
      const response = await fetch(path);
      if (!response.ok) throw new Error(`Request failed: ${response.status}`);
      return response.json();
    }

    async function loadSessions() {
      const params = new URLSearchParams();
      if (els.sessionSearch.value.trim()) params.set("text", els.sessionSearch.value.trim());
      if (els.sourceFilter.value) params.set("source", els.sourceFilter.value);
      if (els.statusFilter.value) params.set("status", els.statusFilter.value);
      const data = await fetchJson(`/api/sessions?${params.toString()}`);
      state.sessions = data.sessions || [];
      renderSessions();
      if (!state.selectedSessionId && state.sessions[0]) {
        await selectSession(state.sessions[0].id);
      }
    }

    function renderSessions() {
      els.sessionList.innerHTML = "";
      if (!state.sessions.length) {
        els.sessionList.innerHTML = '<div class="card" style="padding:14px;color:var(--muted)">No sessions matched the current filters.</div>';
        return;
      }
      for (const row of state.sessions) {
        const sessionEl = document.createElement("div");
        sessionEl.className = `session-row${row.id === state.selectedSessionId ? " active" : ""}`;
        const metrics = row.metrics || {};
        sessionEl.innerHTML = `
          <div style="display:flex;justify-content:space-between;gap:12px">
            <strong>${escapeHtml(row.title || row.preview || row.id)}</strong>
            <small>${escapeHtml(row.source || "—")}</small>
          </div>
          <div class="session-meta" style="margin-top:8px">
            <small>${escapeHtml(row.id)}</small>
            <small>${escapeHtml(fmtTime(row.last_active))}</small>
          </div>
          <div class="session-stats" style="margin-top:10px">
            <span>runtime ${escapeHtml(fmtMs(metrics.runtime_ms))}</span>
            <span>tools ${escapeHtml(metrics.tool_count ?? 0)}</span>
            <span>errors ${escapeHtml(metrics.error_count ?? 0)}</span>
            <span>${escapeHtml(metrics.last_status || "unknown")}</span>
          </div>
        `;
        sessionEl.addEventListener("click", () => selectSession(row.id));
        els.sessionList.appendChild(sessionEl);
      }
    }

    async function selectSession(sessionId) {
      state.selectedSessionId = sessionId;
      state.selectedSession = null;
      renderSessions();
      const [{ session, metrics }] = await Promise.all([
        fetchJson(`/api/sessions/${sessionId}`),
      ]);
      state.selectedSession = session;
      els.sessionTitle.textContent = session.title || session.id;
      els.sessionSubtitle.textContent = `${session.source} • ${session.model || "unknown model"} • started ${fmtTime(session.started_at)}`;
      els.runtimeValue.textContent = fmtMs(metrics.runtime_ms);
      els.toolValue.textContent = metrics.tool_count ?? 0;
      els.errorValue.textContent = metrics.error_count ?? 0;
      els.statusValue.textContent = metrics.last_status || "unknown";
      await loadTimeline();
      attachLiveStream();
    }

    async function loadTimeline() {
      if (!state.selectedSessionId) return;
      const params = new URLSearchParams();
      params.set("scopes", Array.from(state.scopeSet).join(","));
      if (els.detailSearch.value.trim()) params.set("text", els.detailSearch.value.trim());
      if (els.toolNameFilter.value.trim()) params.set("tool_name", els.toolNameFilter.value.trim());
      params.set("limit", "1000");
      const [eventsData, transcriptData] = await Promise.all([
        fetchJson(`/api/sessions/${state.selectedSessionId}/events?${params.toString()}`),
        fetchJson(`/api/sessions/${state.selectedSessionId}/transcript`),
      ]);
      const merged = [];
      for (const row of transcriptData.transcript || []) merged.push({ entry_type: "transcript", ...row });
      for (const row of eventsData.events || []) merged.push({ entry_type: "event", ...row });
      merged.sort((a, b) => (a.ts || 0) - (b.ts || 0));
      state.timeline = merged;
      renderTimeline();
    }

    function renderTimeline() {
      if (!state.timeline.length) {
        els.timeline.innerHTML = '<div class="empty">No transcript or audit events matched these filters.</div>';
        return;
      }
      els.timeline.innerHTML = state.timeline.map(entry => {
        if (entry.entry_type === "transcript") {
          return `
            <div class="timeline-entry">
              <h3>💬 ${escapeHtml(entry.role || "message")}</h3>
              <div class="entry-meta">
                <span class="badge">${escapeHtml(fmtTime(entry.ts))}</span>
                ${entry.tool_name ? `<span class="badge">${escapeHtml(entry.tool_name)}</span>` : ""}
                ${entry.finish_reason ? `<span class="badge">${escapeHtml(entry.finish_reason)}</span>` : ""}
              </div>
              <pre>${escapeHtml(entry.content || "(empty)")}</pre>
            </div>
          `;
        }
        const payload = entry.payload ? `<details><summary>Raw payload</summary><pre>${escapeHtml(JSON.stringify(entry.payload, null, 2))}</pre></details>` : "";
        return `
          <div class="timeline-entry ${entry.is_error ? "entry-error" : ""}">
            <h3>${entry.is_error ? "❌" : "🧭"} ${escapeHtml(entry.title || entry.kind)}</h3>
            <div class="entry-meta">
              <span class="badge">${escapeHtml(entry.kind)}</span>
              <span class="badge">${escapeHtml(entry.phase || "—")}</span>
              <span class="badge">${escapeHtml(entry.status || "—")}</span>
              <span class="badge">${escapeHtml(fmtTime(entry.ts))}</span>
              ${entry.tool_name ? `<span class="badge">${escapeHtml(entry.tool_name)}</span>` : ""}
              ${entry.duration_ms != null ? `<span class="badge">${escapeHtml(fmtMs(entry.duration_ms))}</span>` : ""}
              ${entry.synthetic ? `<span class="badge">synthetic</span>` : ""}
            </div>
            <pre>${escapeHtml(entry.preview || "(no preview)")}</pre>
            ${payload}
          </div>
        `;
      }).join("");
    }

    function attachLiveStream() {
      if (state.liveSource) state.liveSource.close();
      if (!state.selectedSessionId) return;
      const source = new EventSource(`/api/stream?session_id=${encodeURIComponent(state.selectedSessionId)}`);
      state.liveSource = source;
      els.liveIndicator.textContent = "Live stream connected";
      source.addEventListener("session_event", (event) => {
        const payload = JSON.parse(event.data);
        els.liveIndicator.textContent = `Live: ${payload.title || payload.kind}`;
        loadTimeline().catch(console.error);
      });
      source.onerror = () => {
        els.liveIndicator.textContent = "Live stream reconnecting...";
      };
    }

    for (const input of [els.sessionSearch, els.sourceFilter, els.statusFilter]) {
      input.addEventListener("input", () => loadSessions().catch(console.error));
      input.addEventListener("change", () => loadSessions().catch(console.error));
    }
    for (const input of [els.detailSearch, els.toolNameFilter]) {
      input.addEventListener("input", () => loadTimeline().catch(console.error));
    }

    buildScopeToggles();
    loadSessions().catch(console.error);
  </script>
</body>
</html>
"""
