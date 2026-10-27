"""Migration head must match ORM metadata; revisions stay independent of live models."""

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from db.models import Base


def migration_config():
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "deploy/migrate/alembic.ini"))
    cfg.set_main_option("script_location", str(root / "deploy/migrate/alembic"))
    return cfg


def test_migration_matches_models(store):
    head = ScriptDirectory.from_config(migration_config()).get_current_head()
    assert head is not None
    with store.engine.connect() as c:
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == head
        assert compare_metadata(MigrationContext.configure(c), Base.metadata) == []
        assert set(inspect(c).get_table_names()) == set(Base.metadata.tables) | {"alembic_version"}


def test_migrations_have_no_live_model_import():
    revisions = list(ScriptDirectory.from_config(migration_config()).walk_revisions())
    assert revisions
    for revision in revisions:
        assert "db.models" not in Path(revision.path).read_text(), revision.revision


def test_downgrade_and_upgrade(store, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", store.engine.url.render_as_string(hide_password=False))
    cfg = migration_config()
    command.downgrade(cfg, "base")
    with store.engine.connect() as c:
        assert inspect(c).get_table_names() == ["alembic_version"]
    command.upgrade(cfg, "head")
    test_migration_matches_models(store)
