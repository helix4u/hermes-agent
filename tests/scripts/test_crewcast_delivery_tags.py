from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _script_text(name: str) -> str:
    return (PROJECT_ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_make_crew_debate_emits_delivery_tags() -> None:
    text = _script_text("make_crew_debate.py")
    assert "=== Hermes delivery tags ===" in text
    assert "[[audio_as_voice]]" in text
    assert "MEDIA:{delivery_ogg}" in text


def test_make_crew_recap_emits_delivery_tags() -> None:
    text = _script_text("make_crew_recap.py")
    assert "=== Hermes delivery tags ===" in text
    assert "[[audio_as_voice]]" in text
    assert "MEDIA:{delivery_ogg}" in text
