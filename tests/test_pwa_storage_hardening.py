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


def test_share_target_bounds_fetch_event_stream_before_multipart_parse() -> None:
    worker = static_path("sw.js").read_text(encoding="utf-8")

    # Content-Length can be absent on the pre-network FetchEvent request. It is only an
    # optional fast rejection; the body stream itself is always bounded before parsing.
    assert 'const raw = request.headers.get("content-length")' in worker
    assert "if (raw == null) return" in worker
    assert "request.body.getReader()" in worker
    assert "totalBytes += value.byteLength" in worker
    assert "totalBytes > LIMITS.maxShareBodyBytes" in worker
    assert "await reader.cancel" in worker
    assert "new Blob(chunks, { type: contentType })" in worker
    assert 'new Response(bounded, { headers: { "Content-Type": contentType } }).formData()' in worker
    parse_call = "const data = await boundedMultipartFormData(request, contentType)"
    assert parse_call in worker
    assert "const data = await request.formData()" not in worker
    assert worker.index("self.CFStore.getToken()") < worker.index(parse_call)
    assert worker.index("self.CFStore.queueUsage()") < worker.index(parse_call)
