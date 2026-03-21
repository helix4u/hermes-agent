#!/usr/bin/env python3
"""
SQLite State Store for Hermes Agent.

Provides persistent session storage with FTS5 full-text search, replacing
the per-session JSONL file approach. Stores session metadata, full message
history, and model configuration for CLI and gateway sessions.

Key design decisions:
- WAL mode for concurrent readers + one writer (gateway multi-platform)
- FTS5 virtual table for fast text search across all session messages
- Compression-triggered session splitting via parent_session_id chains
- Batch runner and RL trajectories are NOT stored here (separate systems)
- Session source tagging ('cli', 'telegram', 'discord', etc.) for filtering
"""

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional


DEFAULT_DB_PATH = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes")) / "state.db"

SCHEMA_VERSION = 6

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT
);

CREATE TABLE IF NOT EXISTS session_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    phase TEXT,
    span_id TEXT,
    parent_span_id TEXT,
    tool_name TEXT,
    status TEXT,
    duration_ms INTEGER,
    is_error INTEGER DEFAULT 0,
    title TEXT,
    preview TEXT,
    payload_json TEXT,
    source_platform TEXT,
    source_surface TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_session_events_session_seq ON session_events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_session_events_session_ts ON session_events(session_id, ts, id);
CREATE INDEX IF NOT EXISTS idx_session_events_kind ON session_events(kind, ts);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content=messages,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

_REDACT_EVENT_KEYS = {
    "chat_id",
    "thread_id",
    "user_id",
    "user_name",
    "chat_name",
    "session_key",
}


def _normalize_preview(text: Any, limit: int = 180) -> Optional[str]:
    if text is None:
        return None
    preview = str(text).replace("\r", " ").replace("\n", " ").strip()
    if not preview:
        return None
    if len(preview) > limit:
        return preview[: limit - 3] + "..."
    return preview


def _redact_event_payload(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for key, inner in value.items():
            if key in _REDACT_EVENT_KEYS:
                sanitized[key] = "[redacted]"
            else:
                sanitized[key] = _redact_event_payload(inner)
        return sanitized
    if isinstance(value, list):
        return [_redact_event_payload(item) for item in value]
    return value


def _event_scope(kind: str, is_error: bool) -> str:
    if is_error:
        return "errors"
    if kind == "thinking":
        return "thinking"
    if kind in {"tool_start", "tool_finish"}:
        return "tools"
    if kind == "message":
        return "results"
    if kind == "system":
        return "system"
    if kind == "cron":
        return "cron"
    if kind == "delivery":
        return "delivery"
    return kind or "other"


class SessionDB:
    """
    SQLite-backed session storage with FTS5 search.

    Thread-safe for the common gateway pattern (multiple reader threads,
    single writer via WAL mode). Each method opens its own cursor.
    """

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=10.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

        self._init_schema()

    def _init_schema(self):
        """Create tables and FTS if they don't exist, run migrations."""
        cursor = self._conn.cursor()

        cursor.executescript(SCHEMA_SQL)

        # Check schema version and run migrations
        cursor.execute("SELECT version FROM schema_version LIMIT 1")
        row = cursor.fetchone()
        if row is None:
            cursor.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        else:
            current_version = row["version"] if isinstance(row, sqlite3.Row) else row[0]
            if current_version < 2:
                # v2: add finish_reason column to messages
                try:
                    cursor.execute("ALTER TABLE messages ADD COLUMN finish_reason TEXT")
                except sqlite3.OperationalError:
                    pass  # Column already exists
                cursor.execute("UPDATE schema_version SET version = 2")
            if current_version < 3:
                # v3: add title column to sessions
                try:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
                except sqlite3.OperationalError:
                    pass  # Column already exists
                cursor.execute("UPDATE schema_version SET version = 3")
            if current_version < 4:
                # v4: add unique index on title (NULLs allowed, only non-NULL must be unique)
                try:
                    cursor.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_title_unique "
                        "ON sessions(title) WHERE title IS NOT NULL"
                    )
                except sqlite3.OperationalError:
                    pass  # Index already exists
                cursor.execute("UPDATE schema_version SET version = 4")
            if current_version < 5:
                new_columns = [
                    ("cache_read_tokens", "INTEGER DEFAULT 0"),
                    ("cache_write_tokens", "INTEGER DEFAULT 0"),
                    ("reasoning_tokens", "INTEGER DEFAULT 0"),
                    ("billing_provider", "TEXT"),
                    ("billing_base_url", "TEXT"),
                    ("billing_mode", "TEXT"),
                    ("estimated_cost_usd", "REAL"),
                    ("actual_cost_usd", "REAL"),
                    ("cost_status", "TEXT"),
                    ("cost_source", "TEXT"),
                    ("pricing_version", "TEXT"),
                ]
                for name, column_type in new_columns:
                    try:
                        # name and column_type come from the hardcoded tuple above,
                        # not user input. Double-quote identifier escaping is applied
                        # as defense-in-depth; SQLite DDL cannot be parameterized.
                        safe_name = name.replace('"', '""')
                        cursor.execute(f'ALTER TABLE sessions ADD COLUMN "{safe_name}" {column_type}')
                    except sqlite3.OperationalError:
                        pass
                cursor.execute("UPDATE schema_version SET version = 5")
                current_version = 5
            if current_version < 6:
                cursor.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS session_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL UNIQUE,
                        session_id TEXT NOT NULL REFERENCES sessions(id),
                        seq INTEGER NOT NULL,
                        ts REAL NOT NULL,
                        kind TEXT NOT NULL,
                        phase TEXT,
                        span_id TEXT,
                        parent_span_id TEXT,
                        tool_name TEXT,
                        status TEXT,
                        duration_ms INTEGER,
                        is_error INTEGER DEFAULT 0,
                        title TEXT,
                        preview TEXT,
                        payload_json TEXT,
                        source_platform TEXT,
                        source_surface TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_session_events_session_seq ON session_events(session_id, seq);
                    CREATE INDEX IF NOT EXISTS idx_session_events_session_ts ON session_events(session_id, ts, id);
                    CREATE INDEX IF NOT EXISTS idx_session_events_kind ON session_events(kind, ts);
                    """
                )
                cursor.execute("UPDATE schema_version SET version = 6")

        # Unique title index — always ensure it exists (safe to run after migrations
        # since the title column is guaranteed to exist at this point)
        try:
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_title_unique "
                "ON sessions(title) WHERE title IS NOT NULL"
            )
        except sqlite3.OperationalError:
            pass  # Index already exists

        # FTS5 setup (separate because CREATE VIRTUAL TABLE can't be in executescript with IF NOT EXISTS reliably)
        try:
            cursor.execute("SELECT * FROM messages_fts LIMIT 0")
        except sqlite3.OperationalError:
            cursor.executescript(FTS_SQL)

        self._conn.commit()

    def close(self):
        """Close the database connection."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    # =========================================================================
    # Session lifecycle
    # =========================================================================

    def create_session(
        self,
        session_id: str,
        source: str,
        model: str = None,
        model_config: Dict[str, Any] = None,
        system_prompt: str = None,
        user_id: str = None,
        parent_session_id: str = None,
    ) -> str:
        """Create a new session record. Returns the session_id."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO sessions (id, source, user_id, model, model_config,
                   system_prompt, parent_session_id, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    source,
                    user_id,
                    model,
                    json.dumps(model_config) if model_config else None,
                    system_prompt,
                    parent_session_id,
                    time.time(),
                ),
            )
            self._conn.commit()
        return session_id

    def end_session(self, session_id: str, end_reason: str) -> None:
        """Mark a session as ended."""
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
                (time.time(), end_reason, session_id),
            )
            self._conn.commit()

    def update_system_prompt(self, session_id: str, system_prompt: str) -> None:
        """Store the full assembled system prompt snapshot."""
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET system_prompt = ? WHERE id = ?",
                (system_prompt, session_id),
            )
            self._conn.commit()

    def update_token_counts(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: Optional[float] = None,
        actual_cost_usd: Optional[float] = None,
        cost_status: Optional[str] = None,
        cost_source: Optional[str] = None,
        pricing_version: Optional[str] = None,
        billing_provider: Optional[str] = None,
        billing_base_url: Optional[str] = None,
        billing_mode: Optional[str] = None,
    ) -> None:
        """Increment token counters and backfill model if not already set."""
        with self._lock:
            self._conn.execute(
                """UPDATE sessions SET
                   input_tokens = input_tokens + ?,
                   output_tokens = output_tokens + ?,
                   cache_read_tokens = cache_read_tokens + ?,
                   cache_write_tokens = cache_write_tokens + ?,
                   reasoning_tokens = reasoning_tokens + ?,
                   estimated_cost_usd = COALESCE(estimated_cost_usd, 0) + COALESCE(?, 0),
                   actual_cost_usd = CASE
                       WHEN ? IS NULL THEN actual_cost_usd
                       ELSE COALESCE(actual_cost_usd, 0) + ?
                   END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?)
                   WHERE id = ?""",
                (
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_write_tokens,
                    reasoning_tokens,
                    estimated_cost_usd,
                    actual_cost_usd,
                    actual_cost_usd,
                    cost_status,
                    cost_source,
                    pricing_version,
                    billing_provider,
                    billing_base_url,
                    billing_mode,
                    model,
                    session_id,
                ),
            )
            self._conn.commit()

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get a session by ID."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def resolve_session_id(self, session_id_or_prefix: str) -> Optional[str]:
        """Resolve an exact or uniquely prefixed session ID to the full ID.

        Returns the exact ID when it exists. Otherwise treats the input as a
        prefix and returns the single matching session ID if the prefix is
        unambiguous. Returns None for no matches or ambiguous prefixes.
        """
        exact = self.get_session(session_id_or_prefix)
        if exact:
            return exact["id"]

        escaped = (
            session_id_or_prefix
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id FROM sessions WHERE id LIKE ? ESCAPE '\\' ORDER BY started_at DESC LIMIT 2",
                (f"{escaped}%",),
            )
            matches = [row["id"] for row in cursor.fetchall()]
        if len(matches) == 1:
            return matches[0]
        return None

    # Maximum length for session titles
    MAX_TITLE_LENGTH = 100

    @staticmethod
    def sanitize_title(title: Optional[str]) -> Optional[str]:
        """Validate and sanitize a session title.

        - Strips leading/trailing whitespace
        - Removes ASCII control characters (0x00-0x1F, 0x7F) and problematic
          Unicode control chars (zero-width, RTL/LTR overrides, etc.)
        - Collapses internal whitespace runs to single spaces
        - Normalizes empty/whitespace-only strings to None
        - Enforces MAX_TITLE_LENGTH

        Returns the cleaned title string or None.
        Raises ValueError if the title exceeds MAX_TITLE_LENGTH after cleaning.
        """
        if not title:
            return None

        # Remove ASCII control characters (0x00-0x1F, 0x7F) but keep
        # whitespace chars (\t=0x09, \n=0x0A, \r=0x0D) so they can be
        # normalized to spaces by the whitespace collapsing step below
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', title)

        # Remove problematic Unicode control characters:
        # - Zero-width chars (U+200B-U+200F, U+FEFF)
        # - Directional overrides (U+202A-U+202E, U+2066-U+2069)
        # - Object replacement (U+FFFC), interlinear annotation (U+FFF9-U+FFFB)
        cleaned = re.sub(
            r'[\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ufffc\ufff9-\ufffb]',
            '', cleaned,
        )

        # Collapse internal whitespace runs and strip
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()

        if not cleaned:
            return None

        if len(cleaned) > SessionDB.MAX_TITLE_LENGTH:
            raise ValueError(
                f"Title too long ({len(cleaned)} chars, max {SessionDB.MAX_TITLE_LENGTH})"
            )

        return cleaned

    def set_session_title(self, session_id: str, title: str) -> bool:
        """Set or update a session's title.

        Returns True if session was found and title was set.
        Raises ValueError if title is already in use by another session,
        or if the title fails validation (too long, invalid characters).
        Empty/whitespace-only strings are normalized to None (clearing the title).
        """
        title = self.sanitize_title(title)
        with self._lock:
            if title:
                # Check uniqueness (allow the same session to keep its own title)
                cursor = self._conn.execute(
                    "SELECT id FROM sessions WHERE title = ? AND id != ?",
                    (title, session_id),
                )
                conflict = cursor.fetchone()
                if conflict:
                    raise ValueError(
                        f"Title '{title}' is already in use by session {conflict['id']}"
                    )
            cursor = self._conn.execute(
                "UPDATE sessions SET title = ? WHERE id = ?",
                (title, session_id),
            )
            self._conn.commit()
            rowcount = cursor.rowcount
        return rowcount > 0

    def get_session_title(self, session_id: str) -> Optional[str]:
        """Get the title for a session, or None."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT title FROM sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
        return row["title"] if row else None

    def get_session_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        """Look up a session by exact title. Returns session dict or None."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM sessions WHERE title = ?", (title,)
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def resolve_session_by_title(self, title: str) -> Optional[str]:
        """Resolve a title to a session ID, preferring the latest in a lineage.

        If the exact title exists, returns that session's ID.
        If not, searches for "title #N" variants and returns the latest one.
        If the exact title exists AND numbered variants exist, returns the
        latest numbered variant (the most recent continuation).
        """
        # First try exact match
        exact = self.get_session_by_title(title)

        # Also search for numbered variants: "title #2", "title #3", etc.
        # Escape SQL LIKE wildcards (%, _) in the title to prevent false matches
        escaped = title.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id, title, started_at FROM sessions "
                "WHERE title LIKE ? ESCAPE '\\' ORDER BY started_at DESC",
                (f"{escaped} #%",),
            )
            numbered = cursor.fetchall()

        if numbered:
            # Return the most recent numbered variant
            return numbered[0]["id"]
        elif exact:
            return exact["id"]
        return None

    def get_next_title_in_lineage(self, base_title: str) -> str:
        """Generate the next title in a lineage (e.g., "my session" → "my session #2").

        Strips any existing " #N" suffix to find the base name, then finds
        the highest existing number and increments.
        """
        # Strip existing #N suffix to find the true base
        match = re.match(r'^(.*?) #(\d+)$', base_title)
        if match:
            base = match.group(1)
        else:
            base = base_title

        # Find all existing numbered variants
        # Escape SQL LIKE wildcards (%, _) in the base to prevent false matches
        escaped = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            cursor = self._conn.execute(
                "SELECT title FROM sessions WHERE title = ? OR title LIKE ? ESCAPE '\\'",
                (base, f"{escaped} #%"),
            )
            existing = [row["title"] for row in cursor.fetchall()]

        if not existing:
            return base  # No conflict, use the base name as-is

        # Find the highest number
        max_num = 1  # The unnumbered original counts as #1
        for t in existing:
            m = re.match(r'^.* #(\d+)$', t)
            if m:
                max_num = max(max_num, int(m.group(1)))

        return f"{base} #{max_num + 1}"

    def list_sessions_rich(
        self,
        source: str = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List sessions with preview (first user message) and last active timestamp.

        Returns dicts with keys: id, source, model, title, started_at, ended_at,
        message_count, preview (first 60 chars of first user message),
        last_active (timestamp of last message).

        Uses a single query with correlated subqueries instead of N+2 queries.
        """
        source_clause = "WHERE s.source = ?" if source else ""
        query = f"""
            SELECT s.*,
                COALESCE(
                    (SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                     FROM messages m
                     WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                     ORDER BY m.timestamp, m.id LIMIT 1),
                    ''
                ) AS _preview_raw,
                COALESCE(
                    (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                    s.started_at
                ) AS last_active
            FROM sessions s
            {source_clause}
            ORDER BY s.started_at DESC
            LIMIT ? OFFSET ?
        """
        params = (source, limit, offset) if source else (limit, offset)
        with self._lock:
            cursor = self._conn.execute(query, params)
            rows = cursor.fetchall()
        sessions = []
        for row in rows:
            s = dict(row)
            # Build the preview from the raw substring
            raw = s.pop("_preview_raw", "").strip()
            if raw:
                text = raw[:60]
                s["preview"] = text + ("..." if len(raw) > 60 else "")
            else:
                s["preview"] = ""
            sessions.append(s)

        return sessions

    # =========================================================================
    # Message storage
    # =========================================================================

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str = None,
        tool_name: str = None,
        tool_calls: Any = None,
        tool_call_id: str = None,
        token_count: int = None,
        finish_reason: str = None,
    ) -> int:
        """
        Append a message to a session. Returns the message row ID.

        Also increments the session's message_count (and tool_call_count
        if role is 'tool' or tool_calls is present).
        """
        with self._lock:
            cursor = self._conn.execute(
                """INSERT INTO messages (session_id, role, content, tool_call_id,
                   tool_calls, tool_name, timestamp, token_count, finish_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    role,
                    content,
                    tool_call_id,
                    json.dumps(tool_calls) if tool_calls else None,
                    tool_name,
                    time.time(),
                    token_count,
                    finish_reason,
                ),
            )
            msg_id = cursor.lastrowid

            # Update counters
            # Count actual tool calls from the tool_calls list (not from tool responses).
            # A single assistant message can contain multiple parallel tool calls.
            num_tool_calls = 0
            if tool_calls is not None:
                num_tool_calls = len(tool_calls) if isinstance(tool_calls, list) else 1
            if num_tool_calls > 0:
                self._conn.execute(
                    """UPDATE sessions SET message_count = message_count + 1,
                       tool_call_count = tool_call_count + ? WHERE id = ?""",
                    (num_tool_calls, session_id),
                )
            else:
                self._conn.execute(
                    "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                    (session_id,),
                )

            self._conn.commit()
        return msg_id

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Load all messages for a session, ordered by timestamp."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp, id",
                (session_id,),
            )
            rows = cursor.fetchall()
        result = []
        for row in rows:
            msg = dict(row)
            if msg.get("tool_calls"):
                try:
                    msg["tool_calls"] = json.loads(msg["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(msg)
        return result

    # =========================================================================
    # Audit event storage
    # =========================================================================

    def append_event(
        self,
        session_id: str,
        *,
        kind: str,
        phase: str = None,
        span_id: str = None,
        parent_span_id: str = None,
        tool_name: str = None,
        status: str = None,
        duration_ms: int = None,
        is_error: bool = False,
        title: str = None,
        preview: str = None,
        payload: Any = None,
        source_platform: str = None,
        source_surface: str = None,
        ts: float = None,
        event_id: str = None,
    ) -> Dict[str, Any]:
        """Append an auditable event to the session event stream."""
        when = float(ts if ts is not None else time.time())
        payload_clean = _redact_event_payload(payload) if payload is not None else None
        with self._lock:
            seq_cursor = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM session_events WHERE session_id = ?",
                (session_id,),
            )
            seq = int(seq_cursor.fetchone()[0])
            resolved_event_id = event_id or f"evt_{session_id}_{seq}_{uuid.uuid4().hex[:8]}"
            self._conn.execute(
                """INSERT INTO session_events (
                    event_id, session_id, seq, ts, kind, phase, span_id, parent_span_id,
                    tool_name, status, duration_ms, is_error, title, preview,
                    payload_json, source_platform, source_surface
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    resolved_event_id,
                    session_id,
                    seq,
                    when,
                    kind,
                    phase,
                    span_id,
                    parent_span_id,
                    tool_name,
                    status,
                    int(duration_ms) if duration_ms is not None else None,
                    1 if is_error else 0,
                    _normalize_preview(title, 120),
                    _normalize_preview(preview, 220),
                    json.dumps(payload_clean, ensure_ascii=False) if payload_clean is not None else None,
                    source_platform,
                    source_surface,
                ),
            )
            self._conn.commit()
        return {
            "event_id": resolved_event_id,
            "session_id": session_id,
            "seq": seq,
            "ts": when,
            "kind": kind,
            "phase": phase,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "tool_name": tool_name,
            "status": status,
            "duration_ms": int(duration_ms) if duration_ms is not None else None,
            "is_error": bool(is_error),
            "title": _normalize_preview(title, 120),
            "preview": _normalize_preview(preview, 220),
            "payload": payload_clean,
            "source_platform": source_platform,
            "source_surface": source_surface,
            "scope": _event_scope(kind, bool(is_error)),
            "synthetic": False,
        }

    def get_events(
        self,
        session_id: str,
        *,
        kinds: Optional[List[str]] = None,
        tool_name: Optional[str] = None,
        status: Optional[str] = None,
        text: Optional[str] = None,
        min_duration_ms: Optional[int] = None,
        after_id: Optional[int] = None,
        since_ts: Optional[float] = None,
        until_ts: Optional[float] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Return persisted audit events for a session, newest-first limited by filters."""
        where_clauses = ["session_id = ?"]
        params: List[Any] = [session_id]

        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            where_clauses.append(f"kind IN ({placeholders})")
            params.extend(kinds)
        if tool_name:
            where_clauses.append("tool_name = ?")
            params.append(tool_name)
        if status:
            if status == "error":
                where_clauses.append("(status = ? OR is_error = 1)")
            elif status == "running":
                where_clauses.append("status = ?")
            else:
                where_clauses.append("status = ?")
            params.append(status)
        if text:
            where_clauses.append("(COALESCE(title, '') LIKE ? OR COALESCE(preview, '') LIKE ? OR COALESCE(payload_json, '') LIKE ?)")
            like = f"%{text}%"
            params.extend([like, like, like])
        if min_duration_ms is not None:
            where_clauses.append("COALESCE(duration_ms, 0) >= ?")
            params.append(int(min_duration_ms))
        if after_id is not None:
            where_clauses.append("id > ?")
            params.append(int(after_id))
        if since_ts is not None:
            where_clauses.append("ts >= ?")
            params.append(float(since_ts))
        if until_ts is not None:
            where_clauses.append("ts <= ?")
            params.append(float(until_ts))

        params.append(max(1, int(limit)))
        sql = (
            "SELECT * FROM session_events WHERE "
            + " AND ".join(where_clauses)
            + " ORDER BY ts ASC, id ASC LIMIT ?"
        )
        with self._lock:
            cursor = self._conn.execute(sql, params)
            rows = cursor.fetchall()
        return [self._decode_event_row(dict(row)) for row in rows]

    def get_events_since(self, after_id: int = 0, limit: int = 200, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return new events after the given autoincrement row id."""
        params: List[Any] = [int(after_id)]
        where_sql = "id > ?"
        if session_id:
            where_sql += " AND session_id = ?"
            params.append(session_id)
        params.append(max(1, int(limit)))
        with self._lock:
            cursor = self._conn.execute(
                f"SELECT * FROM session_events WHERE {where_sql} ORDER BY id ASC LIMIT ?",
                params,
            )
            rows = cursor.fetchall()
        return [self._decode_event_row(dict(row)) for row in rows]

    def get_session_metrics(self, session_id: str) -> Dict[str, Any]:
        """Compute audit-oriented metrics for a session."""
        session = self.get_session(session_id) or {}
        events = self.get_audit_events(session_id, limit=5000)
        transcript = self.get_messages(session_id)
        tool_finish = [event for event in events if event["kind"] == "tool_finish"]
        errors = [event for event in events if event.get("is_error")]
        durations = [event.get("duration_ms") or 0 for event in tool_finish if event.get("duration_ms") is not None]
        runtime_ms = None
        if events:
            runtime_ms = max(0, int((events[-1]["ts"] - events[0]["ts"]) * 1000))
        elif session.get("ended_at") and session.get("started_at"):
            runtime_ms = max(0, int((session["ended_at"] - session["started_at"]) * 1000))

        tool_stats: Dict[str, Dict[str, Any]] = {}
        for event in tool_finish:
            name = event.get("tool_name") or "tool"
            stat = tool_stats.setdefault(
                name,
                {"tool_name": name, "count": 0, "error_count": 0, "max_duration_ms": 0, "total_duration_ms": 0},
            )
            stat["count"] += 1
            if event.get("is_error"):
                stat["error_count"] += 1
            dur = int(event.get("duration_ms") or 0)
            stat["total_duration_ms"] += dur
            stat["max_duration_ms"] = max(stat["max_duration_ms"], dur)
        slowest_tools = sorted(
            [
                {
                    **stat,
                    "avg_duration_ms": int(stat["total_duration_ms"] / stat["count"]) if stat["count"] else 0,
                }
                for stat in tool_stats.values()
            ],
            key=lambda item: (item["max_duration_ms"], item["count"]),
            reverse=True,
        )[:5]

        last_status = "running"
        if session.get("ended_at"):
            last_status = "ok" if not errors else "error"
        elif any(event.get("status") == "running" for event in events):
            last_status = "running"
        elif errors:
            last_status = "error"

        return {
            "session_id": session_id,
            "runtime_ms": runtime_ms,
            "message_count": len(transcript),
            "tool_count": len(tool_finish),
            "error_count": len(errors),
            "last_status": last_status,
            "slowest_tools": slowest_tools,
            "first_event_ts": events[0]["ts"] if events else session.get("started_at"),
            "last_event_ts": events[-1]["ts"] if events else session.get("ended_at") or session.get("started_at"),
        }

    def get_transcript_rows(self, session_id: str) -> List[Dict[str, Any]]:
        """Return transcript rows normalized for audit UI consumption."""
        messages = self.get_messages(session_id)
        rows: List[Dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content")
            if msg.get("role") == "assistant" and not content and msg.get("tool_calls"):
                content = json.dumps(msg.get("tool_calls"), ensure_ascii=False)
            rows.append(
                {
                    "id": msg.get("id"),
                    "kind": "transcript",
                    "role": msg.get("role"),
                    "tool_name": msg.get("tool_name"),
                    "tool_call_id": msg.get("tool_call_id"),
                    "content": content,
                    "preview": _normalize_preview(content, 220),
                    "ts": msg.get("timestamp"),
                    "finish_reason": msg.get("finish_reason"),
                    "tool_calls": msg.get("tool_calls"),
                }
            )
        return rows

    def get_audit_events(self, session_id: str, **kwargs) -> List[Dict[str, Any]]:
        """Return persisted audit events, or synthesized ones for older sessions."""
        events = self.get_events(session_id, **kwargs)
        if events:
            return events
        synthetic = self._synthesize_events_from_messages(session_id)
        tool_name = kwargs.get("tool_name")
        status = kwargs.get("status")
        text = kwargs.get("text")
        min_duration_ms = kwargs.get("min_duration_ms")
        since_ts = kwargs.get("since_ts")
        until_ts = kwargs.get("until_ts")
        kinds = set(kwargs.get("kinds") or [])
        filtered = []
        for event in synthetic:
            if kinds and event.get("kind") not in kinds:
                continue
            if tool_name and event.get("tool_name") != tool_name:
                continue
            if status == "error" and not event.get("is_error"):
                continue
            if status and status not in {"error"} and event.get("status") != status:
                continue
            if text:
                haystack = " ".join(
                    str(event.get(key) or "")
                    for key in ("title", "preview", "tool_name", "payload")
                ).lower()
                if text.lower() not in haystack:
                    continue
            if min_duration_ms is not None and (event.get("duration_ms") or 0) < int(min_duration_ms):
                continue
            if since_ts is not None and (event.get("ts") or 0) < float(since_ts):
                continue
            if until_ts is not None and (event.get("ts") or 0) > float(until_ts):
                continue
            filtered.append(event)
        return filtered[: max(1, int(kwargs.get("limit", 200)))]

    def get_audit_session_summary(
        self,
        *,
        source: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        text: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return sessions enriched with audit-oriented metrics for the audit viewer."""
        sessions = self.list_sessions_rich(source=source, limit=limit, offset=offset)
        results = []
        for session in sessions:
            if text:
                haystack = " ".join(
                    str(session.get(key) or "")
                    for key in ("id", "title", "preview", "source", "model")
                ).lower()
                if text.lower() not in haystack:
                    continue
            metrics = self.get_session_metrics(session["id"])
            results.append({**session, "metrics": metrics})
        return results

    def _decode_event_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        payload = None
        if row.get("payload_json"):
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                payload = row["payload_json"]
        return {
            "id": row.get("id"),
            "event_id": row.get("event_id"),
            "session_id": row.get("session_id"),
            "seq": row.get("seq"),
            "ts": row.get("ts"),
            "kind": row.get("kind"),
            "phase": row.get("phase"),
            "span_id": row.get("span_id"),
            "parent_span_id": row.get("parent_span_id"),
            "tool_name": row.get("tool_name"),
            "status": row.get("status"),
            "duration_ms": row.get("duration_ms"),
            "is_error": bool(row.get("is_error")),
            "title": row.get("title"),
            "preview": row.get("preview"),
            "payload": payload,
            "source_platform": row.get("source_platform"),
            "source_surface": row.get("source_surface"),
            "scope": _event_scope(row.get("kind"), bool(row.get("is_error"))),
            "synthetic": False,
        }

    def _synthesize_events_from_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Best-effort event timeline for older sessions with no persisted events."""
        messages = self.get_messages(session_id)
        session = self.get_session(session_id) or {}
        source_platform = session.get("source")
        pending_calls: List[Dict[str, Any]] = []
        events: List[Dict[str, Any]] = []
        seq = 0

        for msg in messages:
            msg_ts = float(msg.get("timestamp") or session.get("started_at") or time.time())
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tool_call in msg["tool_calls"]:
                    seq += 1
                    function = tool_call.get("function") or {}
                    tool_name = function.get("name") or "tool"
                    preview = _normalize_preview(function.get("arguments"), 180) or tool_name
                    span_id = tool_call.get("id") or f"synthetic_{session_id}_{seq}"
                    pending_calls.append({"span_id": span_id, "tool_name": tool_name})
                    events.append(
                        {
                            "id": None,
                            "event_id": f"synthetic:{session_id}:{seq}",
                            "session_id": session_id,
                            "seq": seq,
                            "ts": msg_ts,
                            "kind": "tool_start",
                            "phase": "tool",
                            "span_id": span_id,
                            "parent_span_id": None,
                            "tool_name": tool_name,
                            "status": "running",
                            "duration_ms": None,
                            "is_error": False,
                            "title": f"{tool_name} started",
                            "preview": preview,
                            "payload": {
                                "tool_call": _redact_event_payload(tool_call),
                            },
                            "source_platform": source_platform,
                            "source_surface": source_platform,
                            "scope": "tools",
                            "synthetic": True,
                        }
                    )
            elif msg.get("role") == "tool":
                seq += 1
                pending = pending_calls.pop(0) if pending_calls else {}
                tool_name = msg.get("tool_name") or pending.get("tool_name") or "tool"
                content = msg.get("content") or ""
                status = "error" if "[error" in content.lower() or "error" in content.lower() else "ok"
                events.append(
                    {
                        "id": None,
                        "event_id": f"synthetic:{session_id}:{seq}",
                        "session_id": session_id,
                        "seq": seq,
                        "ts": msg_ts,
                        "kind": "tool_finish",
                        "phase": "tool",
                        "span_id": pending.get("span_id"),
                        "parent_span_id": None,
                        "tool_name": tool_name,
                        "status": status,
                        "duration_ms": None,
                        "is_error": status == "error",
                        "title": f"{tool_name} finished",
                        "preview": _normalize_preview(content, 220),
                        "payload": {"result": _normalize_preview(content, 1000)},
                        "source_platform": source_platform,
                        "source_surface": source_platform,
                        "scope": "errors" if status == "error" else "tools",
                        "synthetic": True,
                    }
                )
            elif msg.get("role") == "assistant" and msg.get("content"):
                seq += 1
                content = msg.get("content") or ""
                events.append(
                    {
                        "id": None,
                        "event_id": f"synthetic:{session_id}:{seq}",
                        "session_id": session_id,
                        "seq": seq,
                        "ts": msg_ts,
                        "kind": "message",
                        "phase": "done",
                        "span_id": None,
                        "parent_span_id": None,
                        "tool_name": None,
                        "status": "ok",
                        "duration_ms": None,
                        "is_error": False,
                        "title": "Assistant response",
                        "preview": _normalize_preview(content, 220),
                        "payload": {"content": _normalize_preview(content, 2000)},
                        "source_platform": source_platform,
                        "source_surface": source_platform,
                        "scope": "results",
                        "synthetic": True,
                    }
                )

        return events

    def get_messages_as_conversation(self, session_id: str) -> List[Dict[str, Any]]:
        """
        Load messages in the OpenAI conversation format (role + content dicts).
        Used by the gateway to restore conversation history.
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT role, content, tool_call_id, tool_calls, tool_name "
                "FROM messages WHERE session_id = ? ORDER BY timestamp, id",
                (session_id,),
            )
            rows = cursor.fetchall()
        messages = []
        for row in rows:
            msg = {"role": row["role"], "content": row["content"]}
            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            if row["tool_name"]:
                msg["tool_name"] = row["tool_name"]
            if row["tool_calls"]:
                try:
                    msg["tool_calls"] = json.loads(row["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    pass
            messages.append(msg)
        return messages

    # =========================================================================
    # Search
    # =========================================================================

    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        """Sanitize user input for safe use in FTS5 MATCH queries.

        FTS5 has its own query syntax where characters like ``"``, ``(``, ``)``,
        ``+``, ``*``, ``{``, ``}`` and bare boolean operators (``AND``, ``OR``,
        ``NOT``) have special meaning.  Passing raw user input directly to
        MATCH can cause ``sqlite3.OperationalError``.

        Strategy:
        - Preserve properly paired quoted phrases (``"exact phrase"``)
        - Strip unmatched FTS5-special characters that would cause errors
        - Wrap unquoted hyphenated terms in quotes so FTS5 matches them
          as exact phrases instead of splitting on the hyphen
        """
        # Step 1: Extract balanced double-quoted phrases and protect them
        # from further processing via numbered placeholders.
        _quoted_parts: list = []

        def _preserve_quoted(m: re.Match) -> str:
            _quoted_parts.append(m.group(0))
            return f"\x00Q{len(_quoted_parts) - 1}\x00"

        sanitized = re.sub(r'"[^"]*"', _preserve_quoted, query)

        # Step 2: Strip remaining (unmatched) FTS5-special characters
        sanitized = re.sub(r'[+{}()\"^]', " ", sanitized)

        # Step 3: Collapse repeated * (e.g. "***") into a single one,
        # and remove leading * (prefix-only needs at least one char before *)
        sanitized = re.sub(r"\*+", "*", sanitized)
        sanitized = re.sub(r"(^|\s)\*", r"\1", sanitized)

        # Step 4: Remove dangling boolean operators at start/end that would
        # cause syntax errors (e.g. "hello AND" or "OR world")
        sanitized = re.sub(r"(?i)^(AND|OR|NOT)\b\s*", "", sanitized.strip())
        sanitized = re.sub(r"(?i)\s+(AND|OR|NOT)\s*$", "", sanitized.strip())

        # Step 5: Wrap unquoted hyphenated terms (e.g. ``chat-send``) in
        # double quotes.  FTS5's tokenizer splits on hyphens, turning
        # ``chat-send`` into ``chat AND send``.  Quoting preserves the
        # intended phrase match.
        sanitized = re.sub(r"\b(\w+(?:-\w+)+)\b", r'"\1"', sanitized)

        # Step 6: Restore preserved quoted phrases
        for i, quoted in enumerate(_quoted_parts):
            sanitized = sanitized.replace(f"\x00Q{i}\x00", quoted)

        return sanitized.strip()

    def search_messages(
        self,
        query: str,
        source_filter: List[str] = None,
        role_filter: List[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Full-text search across session messages using FTS5.

        Supports FTS5 query syntax:
          - Simple keywords: "docker deployment"
          - Phrases: '"exact phrase"'
          - Boolean: "docker OR kubernetes", "python NOT java"
          - Prefix: "deploy*"

        Returns matching messages with session metadata, content snippet,
        and surrounding context (1 message before and after the match).
        """
        if not query or not query.strip():
            return []

        query = self._sanitize_fts5_query(query)
        if not query:
            return []

        # Build WHERE clauses dynamically
        where_clauses = ["messages_fts MATCH ?"]
        params: list = [query]

        if source_filter is not None:
            source_placeholders = ",".join("?" for _ in source_filter)
            where_clauses.append(f"s.source IN ({source_placeholders})")
            params.extend(source_filter)

        if role_filter:
            role_placeholders = ",".join("?" for _ in role_filter)
            where_clauses.append(f"m.role IN ({role_placeholders})")
            params.extend(role_filter)

        where_sql = " AND ".join(where_clauses)
        params.extend([limit, offset])

        sql = f"""
            SELECT
                m.id,
                m.session_id,
                m.role,
                snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet,
                m.content,
                m.timestamp,
                m.tool_name,
                s.source,
                s.model,
                s.started_at AS session_started
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE {where_sql}
            ORDER BY rank
            LIMIT ? OFFSET ?
        """

        with self._lock:
            try:
                cursor = self._conn.execute(sql, params)
            except sqlite3.OperationalError:
                # FTS5 query syntax error despite sanitization — return empty
                return []
            matches = [dict(row) for row in cursor.fetchall()]

            # Add surrounding context (1 message before + after each match)
            for match in matches:
                try:
                    ctx_cursor = self._conn.execute(
                        """SELECT role, content FROM messages
                           WHERE session_id = ? AND id >= ? - 1 AND id <= ? + 1
                           ORDER BY id""",
                        (match["session_id"], match["id"], match["id"]),
                    )
                    context_msgs = [
                        {"role": r["role"], "content": (r["content"] or "")[:200]}
                        for r in ctx_cursor.fetchall()
                    ]
                    match["context"] = context_msgs
                except Exception:
                    match["context"] = []

        # Remove full content from result (snippet is enough, saves tokens)
        for match in matches:
            match.pop("content", None)

        return matches

    def search_sessions(
        self,
        source: str = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List sessions, optionally filtered by source."""
        with self._lock:
            if source:
                cursor = self._conn.execute(
                    "SELECT * FROM sessions WHERE source = ? ORDER BY started_at DESC LIMIT ? OFFSET ?",
                    (source, limit, offset),
                )
            else:
                cursor = self._conn.execute(
                    "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            return [dict(row) for row in cursor.fetchall()]

    # =========================================================================
    # Utility
    # =========================================================================

    def session_count(self, source: str = None) -> int:
        """Count sessions, optionally filtered by source."""
        if source:
            cursor = self._conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE source = ?", (source,)
            )
        else:
            cursor = self._conn.execute("SELECT COUNT(*) FROM sessions")
        return cursor.fetchone()[0]

    def message_count(self, session_id: str = None) -> int:
        """Count messages, optionally for a specific session."""
        if session_id:
            cursor = self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
            )
        else:
            cursor = self._conn.execute("SELECT COUNT(*) FROM messages")
        return cursor.fetchone()[0]

    # =========================================================================
    # Export and cleanup
    # =========================================================================

    def export_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Export a single session with all its messages as a dict."""
        session = self.get_session(session_id)
        if not session:
            return None
        messages = self.get_messages(session_id)
        return {**session, "messages": messages}

    def export_all(self, source: str = None) -> List[Dict[str, Any]]:
        """
        Export all sessions (with messages) as a list of dicts.
        Suitable for writing to a JSONL file for backup/analysis.
        """
        sessions = self.search_sessions(source=source, limit=100000)
        results = []
        for session in sessions:
            messages = self.get_messages(session["id"])
            results.append({**session, "messages": messages})
        return results

    def clear_messages(self, session_id: str) -> None:
        """Delete all messages for a session and reset its counters."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )
            self._conn.execute(
                "DELETE FROM session_events WHERE session_id = ?", (session_id,)
            )
            self._conn.execute(
                "UPDATE sessions SET message_count = 0, tool_call_count = 0 WHERE id = ?",
                (session_id,),
            )
            self._conn.commit()

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its messages. Returns True if found."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE id = ?", (session_id,)
            )
            if cursor.fetchone()[0] == 0:
                return False
            self._conn.execute("DELETE FROM session_events WHERE session_id = ?", (session_id,))
            self._conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self._conn.commit()
            return True

    def prune_sessions(self, older_than_days: int = 90, source: str = None) -> int:
        """
        Delete sessions older than N days. Returns count of deleted sessions.
        Only prunes ended sessions (not active ones).
        """
        import time as _time
        cutoff = _time.time() - (older_than_days * 86400)

        with self._lock:
            if source:
                cursor = self._conn.execute(
                    """SELECT id FROM sessions
                       WHERE started_at < ? AND ended_at IS NOT NULL AND source = ?""",
                    (cutoff, source),
                )
            else:
                cursor = self._conn.execute(
                    "SELECT id FROM sessions WHERE started_at < ? AND ended_at IS NOT NULL",
                    (cutoff,),
                )
            session_ids = [row["id"] for row in cursor.fetchall()]

            for sid in session_ids:
                self._conn.execute("DELETE FROM session_events WHERE session_id = ?", (sid,))
                self._conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
                self._conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))

            self._conn.commit()
        return len(session_ids)


class AuditEventStore:
    """Small helper wrapper around SessionDB for writing/querying audit events."""

    def __init__(self, db: SessionDB):
        self.db = db

    def append(self, session_id: str, **kwargs) -> Dict[str, Any]:
        return self.db.append_event(session_id, **kwargs)

    def list(self, session_id: str, **kwargs) -> List[Dict[str, Any]]:
        return self.db.get_events(session_id, **kwargs)

    def since(self, after_id: int = 0, **kwargs) -> List[Dict[str, Any]]:
        return self.db.get_events_since(after_id=after_id, **kwargs)

    def metrics(self, session_id: str) -> Dict[str, Any]:
        return self.db.get_session_metrics(session_id)
