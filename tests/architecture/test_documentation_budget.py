from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CURRENT_ARCHITECTURE = ROOT / "knowledge" / "architecture" / "current.md"
CURRENT_ARCHITECTURE_CHARACTER_BUDGET = 60_000


def test_current_architecture_entry_point_stays_within_its_character_budget() -> None:
    text = CURRENT_ARCHITECTURE.read_text(encoding="utf-8")

    characters = len(text)

    assert characters <= CURRENT_ARCHITECTURE_CHARACTER_BUDGET
