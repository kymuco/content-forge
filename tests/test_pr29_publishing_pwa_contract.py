from __future__ import annotations

from fastapi.testclient import TestClient

from content_forge.api import create_app


def test_pr29_pwa_builds_only_explicit_v2_declaration_candidates(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        script = client.get("/app/publishing.js")
        assert script.status_code == 200
        text = script.text

        assert '"pr29_publish_contract_v2"' in text
        assert '"publishing-child-directed"' in text
        assert '"publishing-synthetic-media"' in text
        assert "Made for kids / child-directed?" in text
        assert "Realistic altered or synthetic media?" in text
        assert "must be explicitly answered Yes or No" in text
        assert "child_directed" in text
        assert "contains_realistic_altered_or_synthetic_media" in text
        assert "declarationSummary(request)" in text
        assert "Review its exact digest and declarations" in text

        # No PWA declaration default may silently choose false/true before the user acts.
        assert 'choose.value = ""' in text
        assert "choose.selected = true" in text

        worker = client.get("/app/sw.js")
        assert worker.status_code == 200
        assert "${CACHE_PREFIX}v16" in worker.text
    finally:
        app.state.runtime_lease.close()
