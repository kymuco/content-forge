# Safety and provenance

## Scope

Content Forge is a media-production tool, not a rights-clearance service. It should nevertheless make source provenance, credits, permission notes, platform suitability, and repository hygiene first-class data so production does not depend on memory or lost browser history.

The guiding principle is:

> Preserve what is known, distinguish facts from guesses, and make important uncertainty visible before publishing.

## Credit is not permission

A visible credit such as `Art by @creator` is useful and respectful, but it does not by itself grant permission to reproduce or monetize a work.

Content Forge should therefore store separate fields for:

```text
creator/artist identity
source URL
credit text
permission status
permission/usage note
```

Do not collapse these into one `credited=true` flag.

## Suggested permission states

The exact enum will be frozen later, but the model should distinguish states such as:

```text
unknown
official_source
creator_allows_repost
explicit_permission
licensed
public_domain_or_compatible_license
user_owned
restricted_or_do_not_use
```

These labels are metadata aids, not legal conclusions produced by the software.

If status is unknown, it should remain unknown rather than being guessed from platform, popularity, or presence of reposts elsewhere.

## Provenance records

For every imported source where available, retain:

```text
source URL
platform/site
creator/artist/channel
original title/caption
collection timestamp
user note
credit text
permission note
```

For downloaded files, keep the immutable asset hash independently from provenance. If the same bytes are later discovered at the original creator source, the asset can gain a better source record without changing identity.

## Creator credit component

The `ArtistCredit`/creator-credit component should render from project/source metadata rather than requiring the operator to retype names for every video.

Templates may decide position/style, but the canonical creator identity should stay in provenance metadata.

A project can require credit as a workflow/QC policy when its source metadata indicates it.

## Actual comments vs invented comment cards

Content Forge may include a reusable `CommentCard` visual component.

Two cases must be distinguishable:

### Reproducing a real comment

Store source/provenance for the comment where practical and preserve the actual text/identity accurately.

### Writing a fictional/stylized reaction

Do not present it as a real quote/comment from a real identifiable person. Use a clearly stylized channel/reaction presentation instead.

The tool should not encourage fabricated screenshots that falsely attribute statements to real people.

## Platform policy and advertiser suitability

A media item being technically uploadable does not mean it is equally suitable for recommendation, monetization, or all audiences.

Projects can later carry non-authoritative risk notes such as:

```text
sexualized_visuals
violence
graphic_content
copyright_claim_likely
reused_content_risk
music_rights_unknown
```

These are review aids, not automatic policy verdicts.

For character/art content, the system should not assume that `SFW` automatically means advertiser-friendly. Likewise, high views do not guarantee monetizable views or stable channel economics.

## Sexualized and boundary content

The observed content formats sometimes use attractive characters, provocative framing, or fandom art as an attention hook. The architecture should support ordinary character-magnet content without depending on sexualization.

Do not optimize the product around finding the narrowest possible boundary of platform sexual-content rules. That is fragile and makes the channel dependent on enforcement changes.

A more robust content strategy can use the same attention mechanisms through:

- strong character design;
- animation quality;
- expressions;
- costume/skin reveals that are platform-safe;
- surprising interactions;
- humor;
- visual detail;
- game/anime fandom recognition.

## Minors/young-looking characters

Production workflows should be conservative around sexualized depictions of minors or characters presented as minors/young-looking. Such material should not be treated as ordinary attention-bait inventory.

The project does not need a complex automated classifier in v0.1, but it should make manual exclusion easy and should not build templates specifically to sexualize such characters.

## Copyrighted media and public repository hygiene

The GitHub repository is public. It must not become a warehouse of production source material.

Do not commit:

- anime episodes/clips;
- manga/manhwa pages;
- fan art without redistribution permission;
- game trailers/assets merely because they are publicly viewable;
- Reddit image packs;
- downloaded social-media videos;
- copyrighted music;
- private user assets;
- generated voice datasets derived from restricted sources;
- cookies/session files/API tokens.

Tests should use synthetic or redistributable fixtures.

## Local runtime separation

Runtime storage should live outside the repository by default.

Suggested categories:

```text
assets/
proxies/
thumbnails/
projects/
cache/
exports/
db/
secrets/config-local/
```

The repository `.gitignore` should also defensively ignore common local runtime paths, but the application should prefer a user data directory that is not inside the Git checkout at all.

## Music

Music provenance should be tracked similarly to visual sources:

```text
track/source
creator
license/permission note
usage restrictions
```

Do not assume that background music is safe merely because another Short uses it.

A future publishing integration may treat platform-native music differently from audio burned into the exported file.

## Reused/inauthentic content risk

Content Forge intentionally makes repetitive editing cheap. That creates a product risk: a technically excellent batch renderer can also make outputs look mechanically repetitive.

The system should therefore make variation and transformation easy without pretending that cosmetic changes automatically solve platform monetization rules.

Useful transformation dimensions include:

- meaningful hook/context;
- commentary/voiceover where appropriate;
- sequencing/story construction;
- localization;
- reactions that add context;
- comparison/explanation;
- original narration;
- distinct presentation and channel identity.

The tool should not encode a promise that a given template is sufficient for monetization.

## Source deletion and auditability

If an asset is removed from the local library, project manifests should retain enough metadata to explain what was referenced, even if re-rendering is no longer possible.

Do not silently rewrite provenance when an original source disappears.

Later tooling may support:

- mark source unavailable;
- mark creator requested removal;
- block future use;
- locate projects/exports that referenced an asset/creator.

## Privacy and credentials

Provider credentials/session state may include:

- ChatGPT web-adapter session/cookies;
- future publishing tokens;
- local auth tokens;
- private-network configuration.

Rules:

1. never serialize secrets into project manifests;
2. never include secrets in render manifests/log output;
3. never send provider cookies to the phone UI;
4. never commit them;
5. keep provider logs redacted by default.

## Provenance-aware UX

The best time to capture source information is when the user discovers the material.

Mobile ingest should therefore make it cheap to send both:

```text
media bytes
+ source URL / creator hint / note
```

A ten-second provenance step during discovery is cheaper than trying to rediscover an artist two months later.

## QC/readiness questions

Before a project is considered publish-ready, Content Forge may eventually surface checks such as:

```text
Is required credit present?
Is source URL known?
Is permission status known/acceptable for this channel policy?
Is the content-risk note unresolved?
Is music provenance known?
```

These checks can be channel/profile-specific and configurable. They should not masquerade as legal advice.

## Repository license

No open-source software license is selected at repository creation time. Public visibility alone does not define reuse terms for the code. License selection should be an explicit future decision rather than an accidental default.
