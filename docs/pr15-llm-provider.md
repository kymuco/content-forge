# PR15 — LLM provider boundary and chatgpt-web-adapter

## Purpose

PR15 adds optional language/semantic assistance without making an LLM part of Content Forge's canonical renderer, storage, or project authority.

The core rule is:

```text
provider output -> validated proposal -> existing ReviewSuggestion -> human/workflow acceptance
```

A generated value is not canonical merely because a model returned it.

## Provider contract

`content_forge.providers.LLMProvider` is task-oriented rather than a generic chat API. It exposes only the capabilities Content Forge needs:

- `suggest_hooks(...)`;
- `suggest_metadata(...)`;
- `clean_text(...)`;
- `translate(...)`;
- `classify(...)`.

Provider methods receive bounded request models and return bounded result models. They do not receive a `Project` object and cannot directly mutate project state.

This keeps model/provider-specific conversation mechanics out of the application and leaves room for a different future provider implementing the same protocol.

## Proposal authority

PR15 reuses the existing PR10 review contracts. `to_review_suggestions(...)` converts validated LLM results into ordinary `ReviewSuggestion` values carrying provider evidence.

`replace_provider_suggestions(...)` may replace only proposals owned by the named provider on an **open** review task. It deliberately does not alter:

- `accepted_value`;
- review status;
- attention mode;
- priority/blocking authority;
- suggestions from another provider or manual source;
- the canonical `Variant`, template selection, or project lifecycle.

Closed review tasks reject provider-suggestion replacement so accepted review evidence cannot be rewritten after resolution.

## Structured output boundary

Every task builds a deterministic prompt containing:

- a fixed task identifier;
- a fixed output shape;
- canonically serialized request JSON.

Project/source text is placed only inside `INPUT_JSON` and is explicitly treated as untrusted data. This is defense in depth, not a claim that prompt injection is solved by prompting alone.

The stronger application boundary is output validation:

- exactly one JSON object is accepted;
- trailing prose is rejected;
- unknown fields are rejected;
- candidate counts and generated string sizes are bounded;
- duplicate candidates/tags are rejected where identity matters;
- classification content kinds and template IDs must come from caller-supplied allowlists.

A single whole-response JSON code fence is tolerated for provider compatibility, but mixed Markdown/prose is not.

## Classification authority

Classification is advisory. The caller supplies closed allowlists of currently valid content-kind and template registry keys. A provider result outside either allowlist fails closed rather than inventing new canonical registry identities.

This means an LLM may rank or suggest from known choices but does not extend the template/content taxonomy by returning arbitrary text.

## chatgpt-web-adapter implementation

`ChatGPTWebAdapterLLMProvider` wraps the current primary production surface from `chatgpt-web-adapter` 0.2:

```text
assemble_product_runtime(...)
  -> ChatGPTProductRuntime
  -> send_text_observed(...)
  -> validated Content Forge result
```

The integration is an optional package extra:

```text
pip install "content-forge[llm]"
```

`chatgpt-web-adapter` is not a base dependency. Importing Content Forge, loading projects, compiling timelines, and rendering continue to work when it is absent.

The adapter imports CWA lazily only when runtime assembly is requested.

### Defaults

The PR15 adapter defaults to:

- transport: `browser-owned`;
- model profile: `BALANCED`;
- conversation mode: `temporary`;
- one new provider task turn per invocation.

`normal` conversation mode is configurable. Temporary Chat is the default so stateless production suggestions do not intentionally accumulate ordinary persistent conversation history.

## Failure semantics and retries

Provider failures are explicit:

- `LLMUnavailableError` — dependency/session/runtime unavailable;
- `LLMExecutionError` — task execution failed before a validated result;
- `LLMResponseError` — returned text violates the Content Forge response contract.

PR15 performs **no automatic product-write retry**. The CWA production boundary can encounter ambiguous delegated-write outcomes that require reconciliation; blindly repeating a request could create duplicate product turns.

An unavailable provider degrades only the optional suggestion feature. Existing manual fields, review flows, timeline compilation, and rendering remain usable.

## Provenance

Each validated result carries sanitized `LLMInvocationEvidence`:

- Content Forge LLM contract version;
- provider ID/version;
- transport;
- requested model profile;
- observed model when available;
- semantic request SHA-256;
- raw assistant-response SHA-256;
- canonical-completion evidence when exposed by the runtime;
- completion source.

Conversation IDs, message IDs, auth/session material, cookies, and browser identifiers are not copied into canonical review suggestions.

The semantic request digest includes the PR15 contract version, task identity, and canonical request JSON. It is a reproducibility/cache identity boundary; PR15 does not claim that model output is deterministic for a repeated request.

## Tests

CI uses fake providers/runtimes and requires no live ChatGPT account, browser session, or network call.

Coverage includes:

- deterministic request digest/prompt construction;
- untrusted-input delimiting;
- exact JSON parsing;
- proposal conversion through existing `ReviewSuggestion`;
- preservation of review acceptance/authority and other-provider proposals;
- refusal to modify closed review tasks;
- CWA runtime assembly defaults;
- all five PR15 task shapes;
- provider health degradation;
- unknown-field/trailing-prose rejection;
- classification allowlist enforcement;
- bounded generated values and candidate counts.

## Explicit exclusions

PR15 does not add:

- automatic acceptance of hooks or metadata;
- automatic project/template mutation;
- LLM-owned project state transitions;
- generic agent/tool execution;
- web scraping or source permission decisions;
- OCR extraction itself;
- persistent provider-result cache/storage;
- a live ChatGPT dependency in CI;
- renderer dependence on any LLM.

PR16 can build language variants on top of these proposal/translation contracts without changing the renderer/provider separation.
