# GAPCUT AI — Submagic API test spike

## Goal

Evaluate Submagic as an optional external editing provider without replacing the existing Hybrid Director or changing the Telegram Mini App.

## Current state

- This is an isolated backend-only spike on `feature/submagic-api-test`, based on the current Hybrid Director branch.
- No Mini App HTML, CSS, JavaScript, or design files are changed.
- The existing FFmpeg/Whisper/Hybrid Director path remains the default and is not routed through Submagic.
- `SUBMAGIC_API_KEY` is optional. With no key, the client makes no network request and raises a clear configuration error only if called.
- The integration has not been enabled on Render and no Submagic request has been sent.

## API workflow from official documentation

1. `GET https://api.submagic.co/v1/languages` — fetch the authenticated live language list.
2. `POST https://api.submagic.co/v1/projects/upload` — upload one short test clip as multipart form data.
3. `GET /v1/projects/{id}` — poll project status and inspect transcript/timecodes.
4. `POST /v1/projects/{id}/export` — request export after the project is ready.
5. Read the completed project's download URL and compare its output with the local FFmpeg result.

Official API documentation: https://docs.submagic.co/introduction

## Safety policy for the first test

- Music is omitted entirely.
- `removeBadTakes=false`; no automated bad-take removal.
- `removeSilencePace` is omitted; do not cut pauses until we have verified the transcript and speech boundaries.
- `cleanAudio=false` for the first comparison, preserving original audio.
- Magic Zooms may be tested; Magic B-roll remains off initially to avoid unnecessary generated media/cost until the first render is inspected.
- Do not send private or customer footage without explicit consent.
- Do not use Submagic output as the production result until visual/audio QA passes.

## Language gate

The public documentation's sample response lists Russian (`ru`) and several other languages, but does not show Tajik. The docs describe the endpoint as returning the supported language list and advertise 100+ languages. Therefore the sample is not enough to conclude that Tajik is unsupported.

When an API key is available:
- Call the live language endpoint.
- Check whether Tajik (`tg`, `tgk`, or the exact code returned by Submagic) exists; do not guess or silently map Tajik to Russian.
- If Tajik is absent, keep Tajik and mixed Tajik/Russian transcription/subtitles on GAPCUT's existing Whisper path. Test Submagic only with a supported Russian/English sample.
- If Tajik is present, test a short Tajik-only clip and manually compare every subtitle, including Ғ, Ӯ, Қ, Ҳ, Ҷ, and Ӣ.

## Acceptance tests

1. API key is stored only as Render environment secret `SUBMAGIC_API_KEY`, never in Git.
2. Live language list captured and Tajik support confirmed or explicitly marked unsupported.
3. One 15–30 second test clip uploaded.
4. Project reaches completed status and transcript timestamps are retrievable.
5. Export succeeds at 1080×1920, 30 FPS (if source and API support it).
6. Original speech is intact; no words or full phrases are cut.
7. Captions are synchronized and Tajik spelling is reviewed.
8. No music is present.
9. Zoom/B-roll are relevant and do not obscure the speaker.
10. Output is downloaded and passes FFprobe plus a complete decode check.
11. Compare RAM, render time, and output quality against local FFmpeg.
12. If Submagic fails or language quality is poor, fall back to existing local renderer.

## Cost and privacy gate

The official pricing page currently lists a Business + API plan with 100 API minutes/month, and additional API minute packs. Confirm the current account-specific price and credit balance before the first upload. No paid plan or credits are purchased automatically.

## Next action required

Create a Submagic account, generate an API key, and add it to the **staging/test backend only** as `SUBMAGIC_API_KEY`. Do not add it to GitHub, Mini App JavaScript, or Production Render. After the key is available, run the language probe first; only then decide whether to spend credits on a short video upload.
