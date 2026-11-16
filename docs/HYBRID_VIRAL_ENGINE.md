# GAPCUT AI Hybrid Viral Editing Engine

## Goal

Keep the existing Telegram bot and Mini App UI unchanged while making the backend editing pipeline reliable first and visually expressive second.

## Current pipeline (repository inspection)

1. Upload and inspect the source with FFprobe.
2. Extract mono 16 kHz audio.
3. Transcribe with Groq Whisper (whisper-large-v3-turbo, optional best model) with word-level timestamps.
4. For tg_ru, the current implementation transcribes each audio chunk twice (Tajik and Russian), then selects one transcript for the entire chunk using a script score. This can lose one language when a speaker code-switches inside the same chunk.
5. Apply dictionary/glossary/LLM spelling corrections while retaining word timings.
6. Analyze scenes, visual cuts, color, music mood and B-roll candidates.
7. Render through Renderster when enabled; otherwise use the built-in FFmpeg renderer.
8. Return the MP4 through the existing job API and Telegram notification.

## Hybrid provider policy

- Renderster is an optional primary auto-edit provider.
- GAPCUT's FFmpeg renderer is the mandatory fallback.
- Provider failures (quota/trial, HTTP errors, timeout, malformed response or download failure) must not fail the whole job if the local renderer can complete it.
- Remove any partial provider output before starting local rendering.
- Never report a provider render as successful until a non-empty video file has been downloaded and validated.
- Do not change the Mini App or Telegram interface as part of backend reliability work.

## Editing architecture

### Stage A — transcript integrity (P0)

- Keep source audio and transcript timestamps on the original timeline until all transcript edits are complete.
- Use word-level timestamps as the single source of truth; derive caption lines from words.
- For mixed Tajik/Russian speech, do not choose a single language for a whole long chunk. Introduce sentence/phrase-level language decisions and align candidate transcripts before merging.
- Never apply dictionary substitutions blindly: preserve the original token, language tag, confidence and correction reason.
- Reject suspicious corrections that alter token count, timing order or script unexpectedly.
- Generate a transcript QA report: low-confidence words, possible language switches, overlaps, missing timestamp spans and correction count.
- Let users review/edit transcript lines before destructive cutting.

### Stage B — safe cut planning (P0)

- Build keep-ranges from word timings with configurable handles before/after speech.
- Cut only long gaps; do not delete recognized speech, breaths that carry meaning, or the start/end of a word.
- Merge overlapping keep-ranges and clamp them to the probed source duration.
- Create one explicit source-to-output time map. Apply it to captions, scene markers, B-roll, transitions, sound effects and music cues.
- If timestamps are invalid or a proposed cut is too aggressive, keep the original video unchanged.
- Validate the rendered duration against the planned duration and verify both video and audio streams before marking the job done.

### Stage C — Viral Director (P1)

Create a structured edit decision list (EDL), not ad-hoc FFmpeg filters:

- Hook (0–2.5 s): choose a strong opening phrase; do not fabricate words.
- Main dialogue: split into semantic beats and keep the speaker visible.
- B-roll: only use relevant, rights-cleared assets; prefer short overlays/cutaways and never cover every moment of the speaker.
- Motion: restrained punch-ins, reframing, motion graphics and scene-aware transitions.
- Captions: Tajik-safe font, 2–5 words per cue, active-word highlight, safe margins and readable line breaks.
- Sound: voice-first mix, duck music under speech, optional transition SFX; music remains opt-in/configurable.
- Ending: clear CTA only when supported by the source or user settings.

### Stage D — rendering and QA (P1)

- Keep FFmpeg as deterministic renderer and fallback.
- Use a proxy for low-memory rendering, then validate frame size, duration, frame rate, audio presence, output size and decodeability.
- Generate a compact render report with selected cuts, caption count, B-roll count, provider used, warnings and output metadata.
- Do not claim a render succeeded if FFmpeg exits non-zero or the output fails validation.

## Rollout order

1. Fix transcript correctness and mixed-language handling.
2. Add unit tests for word normalization, timestamp remapping, cut safety and caption timing.
3. Add output QA and failure-safe provider fallback.
4. Add the Viral Director EDL and motion/B-roll/audio decisions.
5. Tune style presets and compare outputs against approved reference videos.

## Acceptance tests

- A Tajik-only clip preserves Tajik letters: ғ, ӣ, қ, ӯ, ҳ, ҷ.
- A Russian-only clip remains Russian.
- A mixed-language sentence preserves both languages in the same clip.
- Every caption cue stays within the corresponding spoken word interval (with a small display tolerance).
- A cut never starts or ends inside a recognized word.
- After cuts, all captions and overlays remain synchronized with the output timeline.
- If Renderster returns an error, times out or exhausts credits, the local FFmpeg renderer is attempted.
- The output is marked done only after media validation passes.
- Existing Telegram Mini App routes, fields and UI remain backward-compatible.

## Status of this branch

Implemented in this branch: generic Renderster failure fallback to the built-in FFmpeg renderer, with partial output cleanup.

Not yet implemented: mixed-language transcript merging, transcript QA, expanded cut/render tests, output validation and the Viral Director EDL. These require dedicated fixtures and runtime verification before production rollout.