from gateway.browser_bridge import build_browser_context_message


def test_context_message_enables_live_browser_actions_for_explicit_requests():
    message = build_browser_context_message(
        {
            "note": "Open github and navigate to issues.",
            "url": "https://example.com",
            "title": "Example",
        }
    )
    assert "explicitly asking for a live browser action" in message
    assert "Execute the requested browser navigation/action first." in message
    assert "Do not preempt that explicit browser action with memory/worldview file work" in message


def test_context_message_keeps_no_live_recheck_default_for_reference_turns():
    message = build_browser_context_message(
        {
            "note": "Summarize this page and extract action items.",
            "url": "https://example.com",
            "title": "Example",
        }
    )
    assert "Do not call browser navigation/snapshot/vision tools" in message
    assert "explicitly asking for a live browser action" not in message

