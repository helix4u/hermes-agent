# youtube-intake plugin

Windows-safe YouTube transcript intake plugin for Hermes.

## What it does

Provides one model-callable tool:

- `youtube_digest`

The tool:
- extracts a YouTube video ID from a URL or raw ID
- fetches title/channel/duration with `yt-dlp`
- tries `youtube_transcript_api` first
- falls back to `yt-dlp --write-auto-subs` when transcript API access is unavailable
- writes all artifacts under `TMP/youtube-intake/<video_id>/`
- normalizes noisy VTT captions into a clean transcript text file
- cleans up stray `#data/tmp/yt/*.vtt` fallout for the current video if encountered

## Why this exists

This plugin is a proof of concept for moving YouTube transcript retrieval out of ad hoc model-built shell recipes and into a deterministic capability with host-aware temp handling.

It specifically targets the Windows-native failure mode where manual `yt-dlp` subtitle fallback can leave unexpected root residue like `#data/tmp/yt/...`.

## Current limitations

- This plugin returns metadata plus a clean transcript artifact. The model still writes the final prose summary.
- It does not yet register a slash command. Tool registration works today; plugin command registration in Hermes is still in flux.
- Language fallback is intentionally simple and currently biased toward English.

## Project-plugin loading

This plugin is checked into the repo under `.hermes/plugins/youtube-intake/`.

To load project plugins during development, enable:

- `HERMES_ENABLE_PROJECT_PLUGINS=1`

Hermes will then discover the plugin from the current project directory.
