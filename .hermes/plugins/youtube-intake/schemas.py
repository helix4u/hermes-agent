"""Schemas for the youtube-intake plugin."""

YOUTUBE_DIGEST = {
    "name": "youtube_digest",
    "description": (
        "Fetch YouTube video metadata and transcript in a Windows-safe way without ad hoc shell choreography. "
        "Use this when the user sends a YouTube URL or video ID and wants a transcript, a clean summary-ready transcript artifact, "
        "or structured metadata. The tool prefers transcript API access first and falls back to yt-dlp auto-subs in a plugin-owned TMP scratch directory. "
        "It also cleans up stray #data yt-dlp fallout for the current video when encountered."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "YouTube URL or raw 11-character video ID.",
            },
            "language": {
                "type": "string",
                "description": "Preferred transcript language code such as 'en' or 'en-US'. Optional.",
            },
            "include_timestamps": {
                "type": "boolean",
                "description": "When true, include timestamps in the returned transcript text.",
                "default": False,
            },
            "keep_artifacts": {
                "type": "boolean",
                "description": "When true, keep downloaded raw subtitle files in TMP/youtube-intake/<video_id> instead of deleting the raw fallback artifact after normalization.",
                "default": False,
            },
        },
        "required": ["url"],
    },
}
