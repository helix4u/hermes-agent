#!/usr/bin/env python3
"""Capture still images from OBS scenes or sources via obs-websocket."""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path


def _get_attr(value, key, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _load_client(host: str, port: int, password: str):
    try:
        import obsws_python as obs  # type: ignore
    except Exception as exc:  # pragma: no cover - import guard
        raise RuntimeError(
            "Missing dependency 'obsws-python'. Install it with `pip install obsws-python`."
        ) from exc

    return obs.ReqClient(host=host, port=port, password=password, timeout=10)


def _decode_image_data(image_data: str) -> bytes:
    value = str(image_data or "").strip()
    if not value:
        raise RuntimeError("OBS returned empty image data.")

    if value.startswith("data:"):
        _, _, payload = value.partition(",")
        value = payload

    return base64.b64decode(value)


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)


def _print_scene_list(client) -> int:
    response = client.get_scene_list()
    scenes = _get_attr(response, "scenes", []) or []
    if not scenes:
        print("No OBS scenes found.")
        return 0

    for scene in scenes:
        print(_get_attr(scene, "sceneName", "") or _get_attr(scene, "scene_name", ""))
    return 0


def _print_source_list(client, scene_name: str) -> int:
    response = client.get_scene_item_list(scene_name)
    items = _get_attr(response, "sceneItems", []) or _get_attr(response, "scene_items", []) or []
    if not items:
        print(f"No sources found in scene: {scene_name}")
        return 0

    for item in items:
        source_name = _get_attr(item, "sourceName", "") or _get_attr(item, "source_name", "")
        input_kind = _get_attr(item, "inputKind", "") or _get_attr(item, "input_kind", "")
        source_type = _get_attr(item, "sourceType", "") or _get_attr(item, "source_type", "")
        parts = [part for part in (source_name, input_kind or source_type) if part]
        print(" | ".join(parts))
    return 0


def _capture_target(client, target_name: str, output_path: Path, width: int, height: int) -> int:
    width_arg = width if width and width >= 8 else 1920
    height_arg = height if height and height >= 8 else 1080
    response = client.get_source_screenshot(
        target_name,
        "png",
        width=width_arg,
        height=height_arg,
        quality=-1,
    )
    image_data = _get_attr(response, "imageData", "") or _get_attr(response, "image_data", "")
    payload = _decode_image_data(image_data)
    _write_bytes(output_path, payload)
    print(str(output_path))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture OBS scene/source stills via obs-websocket.")
    parser.add_argument("--host", default=os.getenv("OBS_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.getenv("OBS_PORT", "4455")))
    parser.add_argument("--password", default=os.getenv("OBS_PASSWORD", ""))
    parser.add_argument("--list-scenes", action="store_true")
    parser.add_argument("--list-sources", action="store_true")
    parser.add_argument("--scene", default="")
    parser.add_argument("--source", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--width", type=int, default=0)
    parser.add_argument("--height", type=int, default=0)
    args = parser.parse_args()

    action_count = sum(
        1 for enabled in (args.list_scenes, args.list_sources, bool(args.scene), bool(args.source)) if enabled
    )
    if action_count == 0:
        parser.error("Choose one action: --list-scenes, --list-sources --scene NAME, --scene NAME --output PATH, or --source NAME --output PATH.")
    if args.scene and args.source:
        parser.error("Choose either --scene or --source for capture, not both.")
    if args.list_sources and not args.scene:
        parser.error("--list-sources requires --scene.")
    if (args.scene or args.source) and not args.output and not args.list_sources:
        parser.error("Capturing a scene or source requires --output.")

    client = _load_client(args.host, args.port, args.password)

    if args.list_scenes:
        return _print_scene_list(client)
    if args.list_sources:
        return _print_source_list(client, args.scene)

    target_name = args.source or args.scene
    output_path = Path(args.output).expanduser()
    return _capture_target(client, target_name, output_path, args.width, args.height)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
