"""youtube-intake plugin registration."""

from . import schemas, tools


def register(ctx):
    ctx.register_tool(
        name="youtube_digest",
        toolset="youtube_intake",
        schema=schemas.YOUTUBE_DIGEST,
        handler=tools.youtube_digest,
        description=(
            "Windows-safe YouTube transcript intake and digest prep with transcript API first, yt-dlp fallback, and TMP scratch-dir cleanup."
        ),
        emoji="",
    )
