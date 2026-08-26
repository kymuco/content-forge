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
    assert "${CACHE_PREFIX}v6" in worker


def test_live_share_authority_refreshes_before_body_consumption() -> None:
    worker = static_path("sw.js").read_text(encoding="utf-8")

    assert 'const LIVE_CONFIG_URL = appUrl("config.json")' in worker
    assert "async function currentShareLimits()" in worker
    fetch_call = 'response = await fetch(LIVE_CONFIG_URL, { cache: "no-store" })'
    body_read = "body = await response.text()"
    json_parse = "payload = JSON.parse(body)"
    validate = "return validateLiveLimits(payload)"
    assert fetch_call in worker
    assert body_read in worker
    assert json_parse in worker
    assert validate in worker
    # Fetch rejection and an interrupted response body are genuine offline/network
    # failures. Malformed bytes received in full are parsed only afterwards and fail
    # closed rather than being confused with an offline fallback.
    assert worker.count("return LIMITS") >= 2
    assert worker.index(fetch_call) < worker.index(body_read)
    assert worker.index(body_read) < worker.index(json_parse)
    assert worker.index(json_parse) < worker.index(validate)
    assert "throw new Error(\"invalid live PWA limits\")" in worker

    assert "const activeLimits = await currentShareLimits()" in worker
    parse_call = "const data = await boundedMultipartFormData(request, contentType, activeLimits)"
    assert parse_call in worker
    assert worker.index("const activeLimits = await currentShareLimits()") < worker.index(parse_call)
    assert "file.size > activeLimits.maxUploadBytes" in worker
    assert "contentLength > activeLimits.maxShareBodyBytes" in worker


def test_service_worker_update_bypasses_http_cache_for_imports() -> None:
    client = static_path("app.js").read_text(encoding="utf-8")

    assert 'navigator.serviceWorker.register("sw.js", { scope: "./", updateViaCache: "none" })' in client


def test_413_preserves_local_capture_without_poisoning_later_queue_items() -> None:
    client = static_path("app.js").read_text(encoding="utf-8")

    preserved = client.index("if (error.status === 413)")
    permanent = client.index("if (isPermanentQueueRejection(error.status))")
    assert preserved < permanent
    branch = client[preserved:permanent]
    assert "window.CFStore.deleteShare(record.id)" not in branch
    assert "The item remains queued" in branch
    assert "Later captures will continue" in branch
    assert "continue;" in branch


def test_existing_pairing_cannot_be_silently_replaced_by_qr() -> None:
    client = static_path("app.js").read_text(encoding="utf-8")

    start = client.index("async function autoPairFromFragment()")
    end = client.index("async function revokeSession()")
    auto_pair = client[start:end]
    assert "history.replaceState" in auto_pair
    assert "if (bearerToken)" in auto_pair
    assert "already paired" in auto_pair
    assert auto_pair.index("if (bearerToken)") < auto_pair.index("exchangePairing(challengeId, code)")


def test_share_target_streams_through_byte_cap_before_multipart_parse() -> None:
    worker = static_path("sw.js").read_text(encoding="utf-8")

    # Content-Length can be absent on the pre-network FetchEvent request. It is only an
    # optional hint; currentShareLimits supplies the authority used by the stream wrapper.
    assert 'const raw = request.headers.get("content-length")' in worker
    assert "if (raw == null) return null" in worker
    assert "request.body.getReader()" in worker
    assert "const boundedStream = new ReadableStream" in worker
    assert "totalBytes += value.byteLength" in worker
    assert "totalBytes > limits.maxShareBodyBytes" in worker
    assert "await reader.cancel" in worker
    assert "controller.enqueue(value)" in worker
    assert "new Response(boundedStream" in worker
    assert ").formData()" in worker
    assert "const chunks = []" not in worker
    assert "new Blob(chunks" not in worker
    parse_call = "const data = await boundedMultipartFormData(request, contentType, activeLimits)"
    assert parse_call in worker
    assert "const data = await request.formData()" not in worker
    assert worker.index("self.CFStore.getToken()") < worker.index(parse_call)
    assert worker.index("self.CFStore.queueUsage()") < worker.index(parse_call)
