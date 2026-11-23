"""Turn frames recorded by record.mjs into small GIFs (one shared palette, no dithering).

Usage: python tools/readme-gifs/make_gif.py <frames-dir> <out-dir> [scene ...]
Needs Pillow (pip install pillow).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageChops


def build(scene_dir: Path, out: Path) -> None:
    frames = json.loads((scene_dir / "frames.json").read_text())
    images: list[Image.Image] = []
    durations: list[int] = []
    for f in frames:
        img = Image.open(scene_dir / f["file"]).convert("RGB")
        if images and ImageChops.difference(img, images[-1]).getbbox() is None:
            durations[-1] += f["ms"]
            continue
        images.append(img)
        durations.append(f["ms"])

    # One palette for the whole clip, from a strip of evenly spaced frames, so colours don't flicker.
    picks = images[:: max(1, len(images) // 12)]
    w, h = images[0].size
    strip = Image.new("RGB", (w, h * len(picks)))
    for i, img in enumerate(picks):
        strip.paste(img, (0, h * i))
    palette = strip.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    paletted = [img.quantize(palette=palette, dither=Image.Dither.NONE) for img in images]

    # GIF delays are in 10 ms units; keep the total length right.
    durations = [max(20, round(d / 10) * 10) for d in durations]
    durations[-1] = max(durations[-1], 1200)
    paletted[0].save(
        out,
        save_all=True,
        append_images=paletted[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=1,
    )
    print(f"{out.name}: {len(paletted)} frames, {sum(durations) / 1000:.1f} s, {out.stat().st_size / 1024:.0f} KB")


def main() -> None:
    frames_dir, out_dir, *only = sys.argv[1:]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for scene in only or sorted(p.name for p in Path(frames_dir).iterdir() if (p / "frames.json").exists()):
        build(Path(frames_dir) / scene, out / f"{scene}.gif")


if __name__ == "__main__":
    main()
