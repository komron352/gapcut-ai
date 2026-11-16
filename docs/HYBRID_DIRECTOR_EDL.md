# Hybrid AI Video Director — implementation notes

## Current milestone

This branch introduces the first deterministic Edit Decision List (EDL) layer while preserving the existing Telegram Mini App markup and styling.

### Implemented in this milestone

- `gapcut_engine.py` converts recognized word timings, semantic scene analysis, and selected B-roll cards into a serializable, source-timeline-first EDL.
- The EDL includes scene ranges, transcript-grounded hook candidates, caption cues, restrained motion cues, B-roll references, and a source-to-output timeline map.
- Invalid duration values are rejected. Invalid or overlapping word intervals are excluded from EDL decisions rather than used to invent timing.
- A hook is emitted only when the existing scene analysis marks a hook/question/high-importance scene. The text is copied from recognized source words; no hook text is generated.
- The existing scene, color, and audio-analysis results are now passed into the local renderer. Previously the scene analysis was stored on the job but not reliably forwarded into the local render options.
- The UI's `dynamic` camera mode now resolves to the existing scene-aware `auto` motion logic, instead of only zooming at generic caption boundaries.
- Renderster receives `music: none` by default and only receives music when explicitly requested in render settings. Its speech-matched stock B-roll is enabled only when the user's B-roll setting is `auto`; `off` disables it. Retakes are set to `keep` by default.
- The Mini App files are unchanged.

### Not implemented yet

- Face detection/tracking and face-aware reframing.
- True speech VAD (current pause detection is FFmpeg energy-based silence detection).
- Full semantic alignment of competing Tajik/Russian transcripts.
- Per-word calibrated confidence scores from the speech provider.
- User-facing transcript correction controls (would require a separately approved UI change; current Mini App must remain unchanged).
- B-roll relevance scoring and asset rights verification beyond the existing candidate selection path.
- Persistent queue/job recovery across Render restarts.
- Full frame-by-frame and audio/video decode QA.
- Real-media acceptance tests for Tajik, Russian, and mixed speech.

## EDL contract

The EDL is JSON-serializable and uses source timestamps as the ground truth:

- `schema_version`
- `engine`, `style`, `format`
- `source_duration`, `output_duration`
- `timeline[]`: source ranges mapped to output ranges
- `scenes[]`: scene roles and importance on both timelines
- `hook`: optional exact transcript excerpt from a director-marked source scene
- `captions[]`: caption text and word boundaries
- `motion[]`: sparse, scene-driven motion suggestions
- `broll[]`: selected-card references, flagged as requiring asset validation
- `policy`: original voice retained, no music, no invented transcript, no unvalidated speech cuts

## Safety and rollout

- FFmpeg remains the deterministic renderer.
- The EDL is a planning/audit layer; it does not claim that every planned motion/B-roll cue has been visually verified.
- The local renderer remains constrained to one heavy processing job at a time and one FFmpeg thread in the main render command.
- No production Render deployment is part of this milestone.
- Merge to `main` is not authorized.
- Run `python -m unittest discover -s tests -v` in CI before any production test.
- Real sample videos are still required before claiming subtitle or render quality is fixed.

## Transcript QA additions

The backend now returns a transcript QA summary alongside the existing transcript:

- total valid word count
- invalid/ambiguous timing count
- low segment-average-logprob flags with timestamps
- corrections with original and corrected token text
- language word counts and language switches
- long-pause intervals
- detected language ranges

Important limitation: Groq's current word timing response does not provide a calibrated probability for every word in this implementation. The QA report labels low flags as segment-level average-logprob signals and does not misrepresent them as per-word confidence scores.

Context-dependent Tajik token rewrites have been removed from the hard-coded dictionary list. Only the unambiguous `имруз` → `имрӯз` normalization remains there; other tokens are left to the language-aware dictionary/LLM checks, which preserve the original token when a correction is accepted.

## Test status

The first GitHub Actions test run executed 8 tests and exposed two failures:
- the EDL test expected a subtle zoom for a high-importance scene, while the planner correctly chose a push-in;
- the mixed transcript test exposed that a Russian-pass word could override a clearly Tajik token at the same timestamp.

Both issues have been addressed in subsequent commits. A successful test run for the latest branch head has not yet been observed, so this milestone is not marked test-passed.

## Audio and output QA additions

- The Renderster path requests `clean_audio: auto`; music is disabled unless explicitly requested. Speech-matched stock B-roll follows the user's `auto`/`off` selection.
- The local FFmpeg proxy now applies a conservative voice chain by default: high-pass rumble reduction, mild `afftdn` noise reduction, loudness normalization, and peak limiting. The `clean_audio: off` option bypasses this chain.
- The final output is decoded once with FFmpeg in single-thread mode using `-xerror` before a job can be marked complete. Output size is also checked against the configured cap.

These filters still need listening tests against real quiet/noisy Tajik and Russian samples. Their presence is not proof that every voice recording will sound better; they must be tuned against sample clips before production.

## Runtime monitoring

The existing `/health` response now includes best-effort resident-memory usage from Linux procfs, active job count, whether the single heavy-processing lock is held, and the configured stored-job cap. This is telemetry only; it does not make in-memory jobs restart-safe.

## Safe aspect-ratio reframing

The local proxy now preserves the source aspect ratio instead of resizing every source directly to the requested output canvas. For a large source/target aspect-ratio mismatch, the renderer uses a blurred, darkened fill behind the complete source frame. This avoids distorting a landscape speaker into a vertical frame or silently center-cropping the subject.

This is a safe fallback, not face tracking. Face-aware tracking remains available only through the optional Renderster path at this stage; the local FFmpeg renderer does not yet detect or follow faces.
