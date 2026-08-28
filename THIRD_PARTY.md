# Third-party software and content

Content Forge source code and repository-owned documentation/assets are licensed under the Apache License 2.0 unless a file states otherwise. That license does **not** replace or override the licenses of third-party software, runtime tools, media, fonts, templates, or other independently copyrighted material.

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

These packages are third-party works and remain governed by their respective upstream licenses. Their inclusion as dependencies does not make them Apache-2.0 works.

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
- voice data;
- third-party templates or branding assets.

Placing such material in a Content Forge library or processing it with Content Forge does not grant redistribution rights and does not make that material Apache-2.0 licensed.

The public repository is intended to contain only project-owned code/documentation, synthetic fixtures, and assets that are safe to redistribute.

## Packaged third-party material

If future versions vendor or redistribute third-party source, binaries, fonts, media, model files, or other licensed assets, this inventory must be updated before release. Any required attribution, source offer, license copy, or NOTICE material must be shipped in the form required by the relevant upstream license.

Content Forge does not currently add a repository `NOTICE` file because there are no project-level attribution notices that need to be propagated under Apache-2.0. If a future bundled dependency requires such notices, they should be added without changing the terms of the project license.
