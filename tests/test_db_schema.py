"""The frozen initial migration must match ORM metadata and downgrade cleanly."""

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from db.models import Base


def test_migration_matches_models(store):
    with store.engine.connect() as c:
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "tensor_0001"
        assert compare_metadata(MigrationContext.configure(c), Base.metadata) == []
        assert set(inspect(c).get_table_names()) == set(Base.metadata.tables) | {"alembic_version"}


def test_initial_has_no_live_model_import():
    root = Path(__file__).resolve().parents[1]
    path = root / "deploy/migrate/alembic/versions"
    assert sorted(p.name for p in path.glob("*.py")) == ["0001_initial.py"]
    assert "db.models" not in (path / "0001_initial.py").read_text()


def test_downgrade_and_upgrade(store, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("DATABASE_URL", store.engine.url.render_as_string(hide_password=False))
    cfg = Config(str(root / "deploy/migrate/alembic.ini"))
    cfg.set_main_option("script_location", str(root / "deploy/migrate/alembic"))
    command.downgrade(cfg, "base")
    with store.engine.connect() as c:
        assert inspect(c).get_table_names() == ["alembic_version"]
    command.upgrade(cfg, "head")
    test_migration_matches_models(store)
