from __future__ import annotations

from content_forge.storage import LocalLibrary


def test_pr27_local_library_does_not_create_publishing_schema_until_first_use(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    with library.database.connection() as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'publish_operations'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'publish_attempts'"
        ).fetchone() is None

    repository = library.publishing
    assert repository is library.publishing

    with library.database.connection() as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'publish_operations'"
        ).fetchone() is not None
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'publish_attempts'"
        ).fetchone() is not None
        row = connection.execute(
            "SELECT version FROM application_schema WHERE component = 'publishing'"
        ).fetchone()
        assert row is not None and int(row["version"]) == 1
