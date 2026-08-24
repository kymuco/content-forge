from __future__ import annotations

import pytest

from content_forge.application import (
    ApplicationRepository,
    AuthManager,
    AuthenticationError,
)
from content_forge.storage import LocalLibrary


def test_pairing_code_is_one_time_and_sessions_are_revocable(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    repository = ApplicationRepository(library.database).initialize()
    auth = AuthManager(repository)

    challenge = auth.create_challenge()
    with library.database.connection() as connection:
        row = connection.execute(
            "SELECT salt, code_digest FROM pairing_challenges WHERE challenge_id = ?",
            (challenge.challenge_id,),
        ).fetchone()
    assert row is not None
    assert challenge.code not in {row["salt"], row["code_digest"]}

    wrong_code = "00000001" if challenge.code == "00000000" else "00000000"
    with pytest.raises(AuthenticationError, match="invalid_pairing_code"):
        auth.exchange(challenge.challenge_id, wrong_code)

    issued = auth.exchange(challenge.challenge_id, challenge.code, label="phone")
    assert auth.authenticate(issued.token).session_id == issued.session.session_id

    with pytest.raises(AuthenticationError, match="consumed"):
        auth.exchange(challenge.challenge_id, challenge.code)

    auth.revoke(issued.token)
    with pytest.raises(AuthenticationError, match="revoked"):
        auth.authenticate(issued.token)


def test_pairing_service_rejects_malformed_code_before_database_work(tmp_path) -> None:
    library = LocalLibrary(tmp_path)
    repository = ApplicationRepository(library.database).initialize()
    auth = AuthManager(repository)
    challenge = auth.create_challenge()

    with pytest.raises(AuthenticationError, match="invalid_pairing_code"):
        auth.exchange(challenge.challenge_id, "not-code")
