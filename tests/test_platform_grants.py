"""Platform intake cannot forge proof, promotion or coordinator state."""

from pathlib import Path

from sqlalchemy import text


def test_platform_privileges(store):
    with store.engine.begin() as c:
        c.exec_driver_sql("CREATE ROLE platform_api")
        grants = (
            Path(__file__).resolve().parents[1] / "deploy/platform/platform_api.sql"
        ).read_text()
        c.execute(text(grants))
        assert c.scalar(
            text("SELECT has_column_privilege('platform_api','submissions','hotkey','INSERT')")
        )
        assert not c.scalar(
            text("SELECT has_column_privilege('platform_api','submissions','state','INSERT')")
        )
        assert not c.scalar(
            text("SELECT has_table_privilege('platform_api','promotions','INSERT')")
        )
        assert c.scalar(
            text(
                "SELECT has_column_privilege('platform_api','coordinator_state',"
                "'publication','SELECT')"
            )
        )
        assert not c.scalar(
            text(
                "SELECT has_column_privilege('platform_api','coordinator_state',"
                "'leader_token','SELECT')"
            )
        )
        c.exec_driver_sql("DROP OWNED BY platform_api")
        c.exec_driver_sql("DROP ROLE platform_api")
