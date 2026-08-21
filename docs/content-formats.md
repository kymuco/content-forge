# Content formats

This document captures the initial content taxonomy that motivated Content Forge. The list is descriptive, not exhaustive. The architecture must support new formats by composition rather than by expanding a hard-coded enum of every internet trend.

## Three separate dimensions

Every project should be understood across at least three independent dimensions.

### 1. Content kind

What the material *is* or why it is interesting.

Examples:

```text
funny_clip
anime_moment
character_moment
game_news_moment
art_single
art_story
comic_story
manga_panels
voiced_story
sync_meme
reaction_story
```

### 2. Presentation template

How the material is laid out on the canvas.

Examples:

```text
hook_overlay
hook_topbar
social_post
meme_white_header
content_frame
art_reveal
panel_sequence
sync_stack
reaction_bottom
comment_card
```

### 3. Workflow

What preparation/review steps are required before rendering.

Examples:

```text
clip_basic
clip_with_hook
art_single
art_sequence
panel_sequence
voiced_dialogue
localized_variant
```

A project can therefore compose these dimensions freely. For example:

```text
character_moment + hook_overlay + clip_with_hook
character_moment + sync_stack + clip_with_hook
art_story + reaction_bottom + art_sequence
manga_panels + panel_sequence + panel_sequence
manga_panels + panel_sequence + voiced_dialogue
funny_clip + social_post + clip_with_hook
```

## Family A — Funny/reaction clips

### Source pattern

- one short video;
- often an already funny or surprising real-world moment;
- sometimes a meme premise/header provides context.

### Useful templates

- `social_post`;
- `meme_white_header`;
- `hook_topbar`;
- `hook_overlay`.

### Typical operations

- trim;
- crop/contain/cover;
- headline/caption;
- optional avatar/channel-style card;
- audio normalization;
- optional watermark/credit.

### Automation level

Very high. Source selection and the caption/hook are usually the main human-value decisions.

## Family B — Anime moments in a frame

### Source pattern

- short anime clip;
- decorative header/footer art or a branded frame;
- hook/title and profile information;
- sometimes subtitles already exist in source.

### Useful templates

- `content_frame`;
- `hook_topbar`;
- `hook_overlay`.

### Typical operations

- fit the video into a central rect;
- render static branding as background/foreground layers;
- profile/avatar/handle overlay;
- caption or hook;
- subtitles;
- optional CTA.

### Design rule

Most decoration should be precomposed or componentized instead of generating every ornament through FFmpeg filters.

## Family C — Character-magnet game moments

### Source pattern

A visually strong character or animation from games such as character-driven gacha/action titles or hero games. The content need not be sexualized: the hook is often simply strong character design, animation, expression, timing, or fandom recognition.

Examples of moment types:

```text
idle_animation
ultimate_animation
character_reveal
skin_reveal
expression
funny_animation
trailer_moment
interaction
design_comparison
small_detail
cutscene_moment
```

### Useful templates

- `hook_overlay`;
- `hook_topbar`;
- `sync_stack`;
- `reaction_bottom`;
- `content_frame`.

### Hook families

```text
visual       "Her idle is way too good"
reaction     "They didn't have to animate this"
news         "They changed her animation"
detail       "You probably never noticed this"
comparison   "The new design is much cleaner"
question     "Best animation in the game?"
```

### Why this deserves a first-class content kind

The source itself carries much of the attention value. That makes these projects cheap to produce, globally understandable, and well suited to hook/format experiments.

## Family D — Game news/reveal moments

Closely related to `character_moment`, but the semantic reason to watch is freshness/change rather than merely visual attraction.

Typical sources:

- official teaser;
- developer video;
- new character/skin;
- beta/design change;
- animation comparison;
- event preview.

Typical workflow additions:

- retain exact source URL/title/date;
- optional LLM-assisted hook variants;
- avoid overstating what changed;
- fast turnaround matters more than elaborate editing.

## Family E — Single art

### Source pattern

One illustration with enough visual or narrative content to hold attention.

### Useful templates/workflows

- full-screen image + subtle motion;
- framed art;
- crop-to-full reveal;
- blur reveal;
- reaction-bottom composition;
- optional comment card.

### Motion primitives

```text
slow_zoom
pan
focus_crop
crop_reveal
blur_reveal
hold
```

### Required metadata

Where relevant:

- artist/creator;
- source URL;
- credit text;
- permission/use note.

## Family F — Multi-art story

### Source pattern

Several illustrations by one creator or around one small narrative/theme.

### Narrative mechanisms

- chronological sequence;
- setup -> continuation -> punchline;
- progressive reveal;
- final reaction meme;
- text inside the art creates natural reading time.

### Typical timeline

```text
scene 1: art_01, 2.5s, slow_zoom
scene 2: art_02, 2.8s, pan_down
scene 3: art_03, 3.0s, hold
scene 4: optional reaction, 1.2s
```

### Useful components

- `ArtistCredit`;
- `Reaction`;
- `CommentCard`;
- `KenBurns`;
- `BlurReveal`;
- `CropReveal`.

## Family G — Comic/fan-art story

### Source pattern

One or more images contain dialogue, speech bubbles, or an explicit mini-story.

The art itself already provides:

- characters;
- dialogue;
- conflict/setup;
- punchline;
- reading-time retention.

The system should preserve legibility over decorative motion. A transition that prevents reading is not an improvement merely because it is animated.

## Family H — Manga/manhwa panel sequence

### Source pattern

A pack of roughly several panels/pages/images, often already curated by users around a scene, question, or memorable interaction.

### Non-voiced mode

```text
ordered images
-> crop/fit per image
-> pan/zoom/transitions
-> music
-> optional hook
-> export
```

Possible pacing presets:

- `fast_edit`;
- `readable_story`;
- `punchline_hold`;
- `focus_reveal`.

Pacing may intentionally create a pause/rewatch impulse, but the system should not rely on illegibility as a universal strategy. The preview/review loop must make it easy to tune durations.

## Family I — Voiced panel story

This is a later, richer workflow over the same panel sources.

### Conceptual pipeline

```text
panels
-> OCR
-> text review
-> dialogue order
-> speaker assignment
-> persistent character/cast mapping
-> TTS per line
-> scene timing from speech
-> timed text
-> camera focus/pan/zoom
-> music/ambience mix
-> preview
-> render
```

### Core design requirement

This must not introduce a separate video editor. It produces the same `Scene`/`Timeline` primitives as every other format.

### Voice cast

A channel/project profile may maintain reusable voice identities such as:

```text
female_energetic
female_calm
male_protagonist
male_older
villain
narrator
```

Characters map to cast entries, with per-project overrides.

## Family J — Synchronized stacked meme

### Source pattern

One clip or image is duplicated two or three times, usually synchronized, with a simple meme premise and sometimes a reaction character.

Conceptually:

```text
headline
+ N synchronized copies of source
+ optional reaction image/video
+ music/original audio
```

### Template

`sync_stack`

Parameters may include:

```text
copies: 2 | 3
layout: vertical | horizontal | grid
sync: true
spacing
border
background
headline
reaction
```

This is extremely cheap to render and should be supported as composition rather than a special-purpose script.

## Family K — Reaction overlay

A reaction image/video creates a second emotional layer over otherwise straightforward source material.

The reaction may mean surprise, success, disbelief, exhaustion, approval, or another channel-specific interpretation.

The system should treat reaction media as reusable local assets referenced by stable IDs/tags, not as hard-coded named memes in source code.

## Family L — Comment-card punchline

A stylized comment/reaction card can add a final beat to art or clip content.

Component fields can include:

```text
avatar (optional)
username/display label
comment text
secondary metadata (optional)
```

A generated card should not pretend to be a real comment from a real person when it is invented. If reproducing an actual comment, provenance should be retained.

## Language strategy

Many formats are visually dominant and need little text. The system should therefore support localized variants without duplicating source media.

Potentially localized fields:

```text
hook
subtitle
title
description
hashtags
CTA
font
```

The renderer should not assume English-only line breaking or fonts.

## Template candidates for the initial pack

### `hook_overlay`
Full-screen media with a short hook on top of the visual.

### `hook_topbar`
Dedicated header region plus media area.

### `social_post`
Avatar/name/handle/caption style header with media below.

### `meme_white_header`
Simple white meme header plus media.

### `content_frame`
Decorative header/profile/footer surrounding a media slot.

### `art_story`
Image or image sequence with subtle motion, credit, and optional reaction/comment.

### `panel_sequence`
Readable sequence of comic/manga/manhwa panels with timing and transitions.

### `sync_stack`
Two/three synchronized media copies with premise text and optional reaction.

## What should *not* become a content type

Avoid encoding transient details such as:

- a specific meme character;
- a particular game/anime title;
- one creator/artist;
- a font;
- a color palette;
- one exact channel layout.

Those belong to assets, tags, skins, templates, or project metadata.

## Success criterion for extensibility

When a new channel format is discovered, the first question should be:

> Can this be represented using existing sources, scenes, components, and audio with a new template/workflow?

If yes, core code should not change.
