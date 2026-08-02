import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_worldwide_python_entrypoints_resolve_repo_package() -> None:
    for script in (
        "tools/import_tcgdex_worldwide.py",
        "tools/import_pokemontcg_worldwide.py",
        "tools/report_worldwide_staging.py",
        "tools/run_pokemon_asia_locales.py",
        "tools/add_tcgdex_image_candidates.py",
        "tools/import_pokemon_asia_checkpoint.py",
    ):
        result = subprocess.run(
            [sys.executable, str(ROOT / script), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
