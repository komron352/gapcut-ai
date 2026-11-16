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
- Renderster receives `music: none`, `stock_broll: false`, and `retakes: keep`. This matches the product rule that music is opt-in, avoids unrequested provider stock footage, and avoids provider retake deletion by default.
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
