from __future__ import annotations

from content_forge.web import static_path


def test_file_picker_commits_one_atomic_queue_batch() -> None:
    client = static_path("app.js").read_text(encoding="utf-8")

    assert "async function queueFiles(files)" in client
    assert "const records = files.map" in client
    assert "await window.CFStore.enqueueShares(records)" in client
    assert "for (const file of files) await window.CFStore.enqueueShare" not in client


def test_runtime_config_is_network_first_with_offline_cache_fallback() -> None:
    worker = static_path("sw.js").read_text(encoding="utf-8")

    assert 'const CONFIG_URL = appUrl("config.js")' in worker
    assert "async function networkFirstConfig(request, event)" in worker
    assert 'fetch(request, { cache: "no-store" })' in worker
    assert "const cached = await caches.match(request)" in worker
    assert "if (cached) return cached" in worker
    assert "event.respondWith(networkFirstConfig(request, event))" in worker
    assert "content-forge-shell:${self.registration.scope}:" in worker
    assert "${CACHE_PREFIX}v4" in worker
