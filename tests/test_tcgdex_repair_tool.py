from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools/repair_tcgdex_export.mts"


def test_repair_tool_is_narrow_and_preserves_original_error() -> None:
    source = TOOL.read_text(encoding="utf-8")
    assert "broken_dot_set_import_to_sibling_set_module" in source
    assert "original_error: originalError" in source
    assert "Input and output must differ" in source
    assert "Source path escapes root" in source
