"""app.db est une façade : tout ce que définissent les modules db_*.py doit y être réexporté."""

import ast
from pathlib import Path

import pytest

from app import db

APP = Path(__file__).resolve().parent.parent / "app"
MODULES = sorted(p for p in APP.glob("db_*.py"))
INTERNAL = {"db_path"}   # détail de db_core, volontairement non réexporté


def _top_level_names(path: Path) -> set[str]:
    names = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return names


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.stem)
def test_tout_est_reexporte_par_la_facade(path):
    missing = sorted(n for n in _top_level_names(path) if n not in INTERNAL and not hasattr(db, n))
    assert not missing, f"à ajouter à la liste d'imports de app/db.py : {missing}"


@pytest.mark.parametrize("module", [p.stem for p in MODULES])
def test_import_isole_sans_cycle(module):
    import subprocess
    import sys

    result = subprocess.run([sys.executable, "-c", f"import app.{module}"], cwd=APP.parent, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_chemin_de_base_modifiable_pour_tous_les_modules(tmp_db):
    # tmp_db a remplacé db.DB_PATH : une écriture via un module de domaine doit aller dans CETTE base
    db.upsert_port("Binic", 48.6, -2.82)
    assert tmp_db.exists() and [p["name"] for p in db.list_ports()] == ["Binic"]
