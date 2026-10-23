#!/usr/bin/env python3
"""Build a DiffSynth-style image LoRA dataset from a folder of images.

Zero dependencies (stdlib only). For every image it writes:
  <out>/<name>            image (hardlink first, copy fallback)
  <out>/<stem>.txt        caption (source .txt if present, else template)
  <out>/metadata.csv      header "image,prompt", image paths relative with "/"

Usage:
    python tools/make_image_dataset.py --images <dir> --trigger "my style" --out <dir>
    python tools/make_image_dataset.py --images <dir> --trigger t --out d --copy
    python tools/make_image_dataset.py --images <dir> --trigger t --out d --caption-template "a photo of {trigger}, detailed"

Exit codes: 0 = ok, 2 = fatal (empty dir / no images / bad args), 1 = warnings only? (still 0)
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import struct
import sys
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
DEFAULT_TEMPLATE = "a photo of {trigger}"
ALIGN = 16


def png_size(path: Path):
    with open(path, "rb") as handle:
        head = handle.read(33)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", head[16:24])


def jpeg_size(path: Path):
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:2] != b"\xff\xd8":
        return None
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        if marker in (
            0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
            0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
        ):
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return width, height
        index += 2 + length
    return None


def image_size(path: Path):
    suffix = path.suffix.lower()
    try:
        if suffix == ".png":
            return png_size(path)
        if suffix in (".jpg", ".jpeg"):
            return jpeg_size(path)
    except (OSError, struct.error):
        return None
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build a DiffSynth image LoRA dataset")
    parser.add_argument("--images", required=True, help="source folder of images")
    parser.add_argument("--trigger", required=True, help="trigger word for captions")
    parser.add_argument("--out", required=True, help="output dataset folder")
    parser.add_argument(
        "--caption-template",
        default=DEFAULT_TEMPLATE,
        help="caption template; {trigger} is replaced (default: %r)" % DEFAULT_TEMPLATE,
    )
    parser.add_argument("--copy", action="store_true", help="copy instead of hardlink")
    parser.add_argument("--link", action="store_true", help="hardlink (default: link then copy)")
    args = parser.parse_args(argv)

    src = Path(args.images).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    trigger = (args.trigger or "").strip()
    if not trigger:
        print("ERROR: --trigger must not be empty", file=sys.stderr)
        return 2
    if not src.is_dir():
        print("ERROR: --images is not a directory: %s" % src, file=sys.stderr)
        return 2
    if src == out:
        print("ERROR: --out must differ from --images", file=sys.stderr)
        return 2

    images = sorted(
        (p for p in src.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS),
        key=lambda item: item.name.lower(),
    )
    if not images:
        print("ERROR: no jpg/jpeg/png images in %s" % src, file=sys.stderr)
        return 2

    out.mkdir(parents=True, exist_ok=True)
    template = args.caption_template
    if "{trigger}" not in template:
        template = template + " " + "{trigger}"

    seen = {}
    rows = []
    sizes = []
    generated_captions = 0
    missing_16 = []
    copied = 0

    for image in images:
        key = image.name.lower()
        if key in seen:
            print("WARN: duplicate image name (skipped): %s" % image.name, file=sys.stderr)
            continue
        seen[key] = image

        dest = out / image.name
        if dest.exists():
            dest.unlink()
        if args.copy:
            shutil.copy2(image, dest)
            copied += 1
        else:
            try:
                os.link(image, dest)
            except OSError:
                shutil.copy2(image, dest)
                copied += 1

        source_caption = image.with_suffix(".txt")
        if source_caption.is_file():
            # utf-8-sig: Notepad/PowerShell write UTF-8 with BOM; never let it
            # become the first character of a prompt.
            prompt = source_caption.read_text(
                encoding="utf-8-sig", errors="replace"
            ).replace("\ufeff", "").strip()
        else:
            prompt = ""
        if not prompt:
            prompt = template.format(trigger=trigger).strip()
            generated_captions += 1
        (out / (image.stem + ".txt")).write_text(prompt + "\n", encoding="utf-8")

        size = image_size(image)
        if size:
            sizes.append((image.name, size[0], size[1]))
            if size[0] % ALIGN or size[1] % ALIGN:
                missing_16.append("%s (%dx%d)" % (image.name, size[0], size[1]))

        rows.append({"image": image.name.replace(os.sep, "/"), "prompt": prompt})

    metadata = out / "metadata.csv"
    with open(metadata, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["image", "prompt"])
        writer.writeheader()
        writer.writerows(rows)

    print("dataset:", out)
    print("images: %d  captions: %d  (template-generated: %d)" % (len(rows), len(rows), generated_captions))
    print("links: %d  copies: %d" % (len(rows) - copied, copied))
    if sizes:
        widths = [w for _, w, _ in sizes]
        heights = [h for _, _, h in sizes]
        print("size range: %dx%d .. %dx%d" % (min(widths), min(heights), max(widths), max(heights)))
    if missing_16:
        print("WARN: %d image(s) not %d-divisible (DiffSynth will align them up):" % (len(missing_16), ALIGN))
        for item in missing_16:
            print("  - " + item)
    print("metadata:", metadata)
    print("rows:", len(rows))
    return 0 if rows else 2


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
