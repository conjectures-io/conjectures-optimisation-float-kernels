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
        for column in ("id", "config_hash", "config_evidence"):
            assert c.scalar(
                text(
                    "SELECT has_column_privilege('platform_api',"
                    "'coordinator_state',:column,'SELECT')"
                ),
                {"column": column},
            )
        assert c.scalar(
            text(
                "SELECT has_column_privilege('platform_api',"
                "'source_disqualifications','active','SELECT')"
            )
        )
        assert not c.scalar(
            text(
                "SELECT has_column_privilege('platform_api',"
                "'source_disqualifications','actor','SELECT')"
            )
        )
        c.exec_driver_sql("DROP OWNED BY platform_api")
        c.exec_driver_sql("DROP ROLE platform_api")


def test_publication_pins_board_measurement_identities(store, config):
    from db.evaluations import EvaluationsDb

    coordinator = EvaluationsDb(store.sessions)
    coordinator.set_leader("api-test")
    coordinator.reconcile(config)
    publication = store.publications.publication()
    for name, board in publication["boards"].items():
        assert board["config_hash"] == config.identity(name)
        assert board["initial_reference"] == config.boards[name]["reference"]["sha256"]
