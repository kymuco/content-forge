# Security Policy

Content Forge is a local-first media production runtime with authenticated API, LAN/TLS, filesystem, render-job, and artifact-integrity boundaries. Security reports are welcome and should be handled privately until a fix or disclosure plan exists.

## Supported versions

Content Forge is currently pre-1.0. Security fixes are developed against the latest `main` branch. Older snapshots and unmerged feature branches are not maintained as independently supported releases.

## Reporting a vulnerability

Please do **not** disclose vulnerability details in a public GitHub issue.

If GitHub exposes a private **Report a vulnerability** / Security Advisory flow for this repository, use that channel. If no private reporting form is available, open only a minimal public issue asking the maintainer to establish a private reporting channel; do not include reproduction steps, exploit details, secrets, or sensitive evidence in that issue.

A useful private report includes:

- the affected commit or version;
- the relevant deployment mode (loopback, LAN/TLS, mounted/root-path, etc.);
- impact and attacker prerequisites;
- minimal reproduction steps or a proof of concept;
- any evidence needed to distinguish a security defect from expected fail-closed behavior.

Do not include live bearer tokens, private keys, cookies, credentials, personal media, or other secrets in reports unless a private channel has been established and the material is strictly necessary.

## Security-sensitive areas

Reports are especially useful for issues involving:

- authentication, pairing, bearer-session handling, or authorization bypass;
- LAN/TLS and browser-origin / host validation;
- path traversal, arbitrary filesystem access, or unsafe artifact download;
- command or argument injection into render/tool execution;
- source, render-plan, receipt, or artifact-integrity confusion;
- unsafe recovery after crashes or concurrent state changes;
- malicious media/file handling that crosses a documented trust boundary;
- disclosure of local runtime paths, credentials, tokens, or private production data.

## Disclosure

Please allow reasonable time to investigate, reproduce, fix, and validate a report before public disclosure. Once a fix is ready, the project may publish a coordinated advisory describing affected versions, impact, remediation, and any required credential rotation or data cleanup.

This policy is for vulnerability reporting and does not expand the authority or trust guarantees documented by the runtime itself.
