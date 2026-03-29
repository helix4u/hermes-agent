# OBS WebSocket setup

## Enable OBS WebSocket

OBS 28+ ships with obs-websocket built in.

In OBS:

1. Open `Tools` -> `WebSocket Server Settings`
2. Enable `Enable WebSocket server`
3. Note the port, usually `4455`
4. Set a password if you want one

## Common targets

- `Browser Source` for embedded web content
- `Window Capture` for a specific app window
- `Display Capture` for a whole monitor
- A top-level scene if several sources are composed together

## Troubleshooting

- If the script cannot connect, verify OBS is running and the WebSocket server is enabled.
- If authentication fails, confirm the password matches the OBS setting.
- If a source capture looks wrong, list sources for the scene and use the exact name OBS shows.
- If the source name is unknown, capture the scene first and then narrow down.

## Example commands

```bash
python skills/integrations/obs-scene-capture/scripts/obs_still.py --list-scenes
python skills/integrations/obs-scene-capture/scripts/obs_still.py --list-sources --scene "Main"
python skills/integrations/obs-scene-capture/scripts/obs_still.py --source "Perplexity Browser" --output TMP/obs/perplexity.png
```
