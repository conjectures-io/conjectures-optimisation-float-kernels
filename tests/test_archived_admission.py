"""Replay keeps archived code isolated across the schema/module refactor."""

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "archive_replay", ROOT / "scripts/replay-model-family-admission.py"
)
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)


@pytest.mark.parametrize("layout", ["validator", "validator/exact"])
def test_archived_code_uses_its_own_config_and_rejects_tampering(tmp_path, layout):
    archive = tmp_path / "raw/code" / layout
    archive.mkdir(parents=True)
    sources = {"config": "marker = 'archived'\n", "scoring": "from .config import marker\n"}
    manifest = {}
    for name, source in sources.items():
        (archive / f"{name}.py").write_text(source)
        manifest[name] = hashlib.sha256(source.encode()).hexdigest()
    assert replay.archived_scoring(tmp_path, manifest).marker == "archived"
    (archive / "config.py").write_text("marker = 'tampered'\n")
    with pytest.raises(ValueError, match="does not match manifest"):
        replay.archived_scoring(tmp_path, manifest)
