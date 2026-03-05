"""Tests for tools/send_message_tool.py."""

import sys
import types

import pytest

from tools import send_message_tool as send_tool


class _FakeResponse:
    def __init__(self, status=201, body=None):
        self.status = status
        self._body = body if body is not None else {"id": "m1"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return str(self._body)

    async def json(self):
        return self._body if isinstance(self._body, dict) else {"raw": self._body}


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        if not self._responses:
            raise AssertionError("No fake responses left for session.post()")
        return self._responses.pop(0)


def _install_fake_aiohttp(monkeypatch, session):
    fake_aiohttp = types.SimpleNamespace(ClientSession=lambda: session)
    monkeypatch.setitem(sys.modules, "aiohttp", fake_aiohttp)


class TestChunkText:
    def test_chunk_text_splits_by_requested_size(self):
        text = "abcdef"
        chunks = send_tool._chunk_text(text, 2)
        assert chunks == ["ab", "cd", "ef"]

    def test_chunk_text_empty_input(self):
        assert send_tool._chunk_text("", 10) == [""]


class TestSendDiscord:
    @pytest.mark.asyncio
    async def test_send_discord_posts_embed_payload(self, monkeypatch):
        session = _FakeSession([_FakeResponse(201, {"id": "msg-1"})])
        _install_fake_aiohttp(monkeypatch, session)

        result = await send_tool._send_discord("token", "12345", "hello world")

        assert result["success"] is True
        assert result["format"] == "embed"
        assert result["message_ids"] == ["msg-1"]
        assert session.calls[0]["json"] == {"embeds": [{"description": "hello world"}]}

    @pytest.mark.asyncio
    async def test_send_discord_chunks_by_embed_limit(self, monkeypatch):
        message = "x" * (send_tool.DISCORD_EMBED_DESCRIPTION_LIMIT + 5)
        session = _FakeSession([
            _FakeResponse(201, {"id": "msg-1"}),
            _FakeResponse(201, {"id": "msg-2"}),
        ])
        _install_fake_aiohttp(monkeypatch, session)

        result = await send_tool._send_discord("token", "12345", message)

        assert result["success"] is True
        assert len(session.calls) == 2
        for call in session.calls:
            desc = call["json"]["embeds"][0]["description"]
            assert len(desc) <= send_tool.DISCORD_EMBED_DESCRIPTION_LIMIT

    @pytest.mark.asyncio
    async def test_send_discord_surfaces_api_error(self, monkeypatch):
        session = _FakeSession([_FakeResponse(403, {"message": "Missing Permissions"})])
        _install_fake_aiohttp(monkeypatch, session)

        result = await send_tool._send_discord("token", "12345", "hello world")

        assert "error" in result
        assert "Discord API error (403)" in result["error"]
