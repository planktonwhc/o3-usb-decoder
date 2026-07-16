#!/usr/bin/env python3
"""
CLI: extract playable H.264 from a Pi-MITM USBPROXY_PAYLOAD_LOG.

  python3 analysis/extract_video_stream.py captures/pi_pull/...._payload.log \
      -o captures/pi_pull/video_extract/out

Writes:
  out.from_sps.h264  — ES from first SPS (playable with ffplay -f h264)
  out.all.bin        — full payload concat (debug)
  out.mp4            — re-encoded playable MP4 (ffmpeg libx264)
  out.stats.txt
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from video_deframe import write_extract  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("payload_log", type=Path)
    ap.add_argument("-o", "--out-prefix", type=Path, required=True)
    ap.add_argument("--dirs", default="OUT", help="direction tokens, comma-separated (default OUT)")
    ap.add_argument("--no-mp4", action="store_true", help="skip ffmpeg remux")
    args = ap.parse_args()

    dirs = [d.strip() for d in args.dirs.split(",") if d.strip()]
    print(f"[*] {args.payload_log} dirs={dirs}")
    stats = write_extract(
        args.payload_log,
        args.out_prefix,
        dirs=dirs,
        make_mp4=not args.no_mp4,
    )
    for k, v in stats.items():
        print(f"  {k}: {v}")
    h264 = stats.get("h264_path")
    if h264:
        print(f"[*] play:  ffplay -f h264 {h264}")
        if stats.get("mp4_path"):
            print(f"[*] or:    ffplay {stats['mp4_path']}")


if __name__ == "__main__":
    main()
