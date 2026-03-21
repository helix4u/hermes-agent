import json

import pytest
import pytest_asyncio

from hermes_state import SessionDB
from hermes_cli.audit_server import HermesAuditServer


pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer


@pytest_asyncio.fixture()
async def audit_client(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session(session_id="sess-1", source="cli", model="test/model")
    db.append_message("sess-1", role="user", content="hello")
    db.append_message("sess-1", role="assistant", content="hi there")
    db.append_event(
        "sess-1",
        kind="tool_start",
        phase="tool",
        tool_name="terminal",
        status="running",
        title="terminal started",
        preview="python app.py",
        source_platform="cli",
        source_surface="cli",
    )
    db.append_event(
        "sess-1",
        kind="tool_finish",
        phase="tool",
        tool_name="terminal",
        status="ok",
        duration_ms=250,
        title="terminal finished",
        preview="exit=0",
        payload={"user_id": "999", "result": "ok"},
        source_platform="cli",
        source_surface="cli",
    )
    db.close()

    server = HermesAuditServer(db_path=db_path)
    client = TestClient(TestServer(server.build_app()))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_sessions_endpoint_returns_metrics(audit_client):
    response = await audit_client.get("/api/sessions")
    assert response.status == 200
    data = await response.json()
    assert data["sessions"]
    session = data["sessions"][0]
    assert session["id"] == "sess-1"
    assert session["metrics"]["tool_count"] == 1


@pytest.mark.asyncio
async def test_events_endpoint_redacts_sensitive_payload(audit_client):
    response = await audit_client.get("/api/sessions/sess-1/events?scopes=tools")
    assert response.status == 200
    data = await response.json()
    assert len(data["events"]) == 2
    finish = data["events"][-1]
    assert finish["payload"]["user_id"] == "[redacted]"
    assert finish["duration_ms"] == 250


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_combined_timeline(audit_client):
    response = await audit_client.get("/api/sessions/sess-1/metrics")
    assert response.status == 200
    data = await response.json()
    assert data["metrics"]["message_count"] == 2
    assert any(entry["entry_type"] == "transcript" for entry in data["timeline"])
    assert any(entry["entry_type"] == "event" for entry in data["timeline"])


@pytest.mark.asyncio
async def test_stream_endpoint_emits_new_event(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session(session_id="sess-stream", source="cli")
    db.close()

    server = HermesAuditServer(db_path=db_path)
    client = TestClient(TestServer(server.build_app()))
    await client.start_server()
    try:
        response = await client.get("/api/stream?session_id=sess-stream")
        db2 = SessionDB(db_path=db_path)
        db2.append_event(
            "sess-stream",
            kind="message",
            phase="done",
            status="ok",
            title="Assistant response",
            preview="hello world",
            source_platform="cli",
            source_surface="cli",
        )
        db2.close()

        chunks = []
        while len("".join(chunks)) < 20:
            chunks.append((await response.content.readany()).decode("utf-8"))
            if "session_event" in "".join(chunks):
                break
        payload = "".join(chunks)
        assert "session_event" in payload
        assert "hello world" in payload
    finally:
        await client.close()
        await server.close()
