from __future__ import annotations

from fastapi.testclient import TestClient

from content_forge.api import create_app
from content_forge.application import PublishingService


def test_pr27_pwa_shell_serves_separate_publish_approval_and_execution_controls(tmp_path) -> None:
    app = create_app(root=tmp_path)
    client = TestClient(app)
    try:
        assert isinstance(app.state.publishing, PublishingService)

        shell = client.get("/app/")
        assert shell.status_code == 200
        assert 'id="publishing-panel"' in shell.text
        assert '<script src="publishing.js"></script>' in shell.text
        assert 'id="publishing-candidate-form"' in shell.text
        assert 'id="publishing-approve"' in shell.text
        assert 'id="publishing-execute"' in shell.text
        assert "Approve exact request" in shell.text
        assert "Execute approved attempt" in shell.text
        assert "execute the durable attempt as a separate action" in shell.text

        # PR27's browser surface may name a provider/destination but must not accept
        # runtime filesystem paths or provider credentials/secrets from the PWA.
        forbidden_input_ids = (
            "publishing-media-path",
            "publishing-file-path",
            "publishing-token",
            "publishing-secret",
            "publishing-api-key",
            "publishing-credential",
        )
        for input_id in forbidden_input_ids:
            assert f'id="{input_id}"' not in shell.text

        script = client.get("/app/publishing.js")
        assert script.status_code == 200
        assert script.headers["Cache-Control"] == "no-cache"
        assert 'apiJson("publishing/candidates"' in script.text
        assert 'apiJson("publishing/attempts"' in script.text
        assert "publishing/attempts/${encodeURIComponent(attemptId)}/execute" in script.text
        assert "Candidate invalidated because publishing inputs changed" in script.text
        assert "Remote execution has not started" in script.text
        assert "Automatic retry is blocked" in script.text
        assert "media_path" not in script.text
        assert "access_token" not in script.text
        assert "refresh_token" not in script.text

        worker = client.get("/app/sw.js")
        assert worker.status_code == 200
        assert "${CACHE_PREFIX}v14" in worker.text
        assert "${CACHE_PREFIX}v15" in worker.text
        assert 'appUrl("publishing.js")' in worker.text
    finally:
        app.state.runtime_lease.close()
