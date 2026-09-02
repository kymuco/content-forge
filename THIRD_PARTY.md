# Third-party software and content

Content Forge source code and repository-owned documentation/assets are licensed under the Apache License 2.0 unless a file states otherwise. That license does **not** replace or override the licenses of third-party software, runtime tools, media, fonts, templates, models, or other independently copyrighted material.

This document records the current licensing boundary. It is an inventory aid, not a substitute for the authoritative license text distributed by each dependency or tool.

## Python dependencies

The project declares direct Python dependencies in `pyproject.toml`, currently including:

- FastAPI;
- Pydantic;
- python-multipart;
- PyYAML;
- Segno;
- Uvicorn.

Development/test dependencies currently include HTTPX and pytest.

Optional provider extras currently include:

- `chatgpt-web-adapter` for the PR15 LLM provider boundary;
- `qwen-tts==0.1.1` for the PR20 local Qwen3-TTS provider boundary;
- `huggingface-hub` for resolving PR20 Qwen model repositories at exact immutable commit snapshots;
- `google-api-python-client`, `google-auth`, `google-auth-httplib2`, and `google-auth-oauthlib` for the PR28 YouTube Data API v3 publishing adapter and installed-application OAuth flow.

These packages and their transitive dependencies are third-party works and remain governed by their respective upstream licenses. Their inclusion as dependencies does not make them Apache-2.0 works.

## YouTube Data API and Google OAuth runtime

PR28 can use the external Google API Python client and OAuth libraries as an optional publishing runtime. Content Forge does not bundle Google OAuth client secrets, access tokens, refresh tokens, user credentials, or YouTube account data in the repository.

The OAuth client-secrets JSON is supplied by the operator from Google Cloud Console. The authorized-user token created by `content-forge-youtube-auth` is local runtime state and is written to an explicitly selected owner-only path. Neither credential file is part of `PublishRequest`, publish approval identity, SQLite publishing evidence, API request bodies, or the PWA.

Use of the YouTube Data API remains subject to Google's and YouTube's current API terms, policies, quotas, verification/audit requirements, and account restrictions. The ability to upload through the API does not grant rights to publish underlying media. Operators are responsible for the rights, permissions, disclosures, and platform settings applicable to their content and channel.

## Qwen3-TTS models and runtime

PR20 can use the external `qwen-tts` package and Qwen3-TTS model checkpoints as an optional local provider runtime. Content Forge does not bundle Qwen3-TTS Python source, PyTorch/CUDA binaries, tokenizer/model weights, or downloaded Hugging Face/ModelScope caches in this repository.

PR20 resolves configured Hugging Face Qwen repositories at an explicit immutable commit SHA before model construction so the model, processor, tokenizer/config, and generation files are loaded from one repository snapshot. The snapshot cache remains local runtime data and is not repository content.

The currently targeted official Qwen3-TTS repository and released Qwen 12Hz model cards identify their code/model licensing as Apache-2.0. Downstream packaging must still inspect the authoritative license and metadata for the **exact package version and model checkpoint being distributed**, together with licenses of all transitive runtime dependencies.

Local model downloads remain runtime data and must not be committed to the Content Forge repository. If a future installer, image, appliance, archive, or other distribution begins bundling Qwen3-TTS weights or its heavyweight runtime dependencies, this inventory and any required license/NOTICE material must be reviewed before that distribution ships.

## FFmpeg and ffprobe

Content Forge invokes `ffmpeg` and `ffprobe` as external runtime tools. The repository does not currently bundle or redistribute FFmpeg binaries.

FFmpeg licensing depends on the exact build and enabled components. A downstream package, installer, appliance, container, or other distribution that begins bundling FFmpeg must review the license and configuration of the **specific binary being redistributed** and satisfy its LGPL/GPL and attribution/source obligations as applicable.

The Apache-2.0 license of Content Forge does not relicense FFmpeg.

## User and production media

User-provided or locally acquired media remains subject to its own copyright, license, platform terms, permissions, and provenance. This includes, for example:

- downloaded video or audio;
- game/anime/film footage;
- artwork and photographs;
- music and sound effects;
- fonts;
- voice/reference recordings;
- third-party templates or branding assets.

Placing such material in a Content Forge library or processing it with Content Forge does not grant redistribution rights and does not make that material Apache-2.0 licensed.

Voice cloning deserves the same boundary explicitly: technical ability to use a reference recording does not establish consent or rights to impersonate, publish, or redistribute that voice. PR20 treats reference audio as local user-controlled production data and does not bundle example real-person voice material.

The public repository is intended to contain only project-owned code/documentation, synthetic fixtures, and assets that are safe to redistribute.

## Packaged third-party material

If future versions vendor or redistribute third-party source, binaries, fonts, media, model files, or other licensed assets, this inventory must be updated before release. Any required attribution, source offer, license copy, or NOTICE material must be shipped in the form required by the relevant upstream license.

Content Forge does not currently add a repository `NOTICE` file because there are no project-level attribution notices that need to be propagated under Apache-2.0. If a future bundled dependency requires such notices, they should be added without changing the terms of the project license.
