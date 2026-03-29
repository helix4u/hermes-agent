# Hermes Sidecar

This is a no-build Chrome extension that opens a companion side panel and keeps a
real Hermes chat session beside the active tab.

## What it can do

- Keep a dedicated browser-side Hermes session with transcript history
- Switch between recent sidecar sessions from the session history picker
- Send normal chat messages to that session
- Interrupt an in-flight sidecar response chain when you need Hermes to stop
- Share the current page into the same conversation when you want more context
- Include page title, URL, visible text, selection, and metadata when sharing
- For YouTube watch pages: include the transcript the first time that video is
  shared in the current browser session

## Load it in Chrome

1. Open `chrome://extensions`.
2. Enable `Developer mode`.
3. Click `Load unpacked`.
4. Select the `hermes-agent/browser-extension` folder.
5. Pin the extension if you want one-click access from the toolbar.

## Connect it to Hermes

1. Start the local gateway:

   ```bash
   hermes gateway start
   ```

2. Read the browser bridge token from `~/.hermes/browser_bridge_token`.

3. Open extension options (`Right click extension -> Options`) and save:
   - `Bridge URL`: `http://127.0.0.1:8765/inject`
   - `Bridge token`: the value from `~/.hermes/browser_bridge_token`
   - Voice/STT settings now use dropdown defaults for local/OpenAI transcription models so saving the page does not blank out STT model config
4. Saving settings now auto-checks bridge health.

## Use it

1. Click the extension icon to open the side panel.
2. Type a normal chat message to Hermes.
3. Enable `Use the current page in this turn` whenever the active tab
   should be part of the turn.
4. If the page is a YouTube video, optionally include the transcript the first
   time you share that video.
5. Use `New chat` to reset just the sidecar session.
6. Use `Interrupt` while Hermes is working if you need to stop the current turn.
7. Use the `Session history` picker to jump between recent sidecar sessions.

## Notes

- The bridge only listens on `127.0.0.1` by default.
- The side panel follows the active tab in the current Chrome window and
  refreshes page context automatically.
- The extension stores the bridge URL and token in Chrome sync storage.
- The sidecar session is separate from the CLI session.
- `Enter` sends and `Shift+Enter` inserts a newline in the composer.
- If you resend the same YouTube page in the same browser session, the
  transcript is omitted after the first successful share to avoid duplication.
