#!/usr/bin/env python3
"""
poc/pi_endpoint/endpoint.py — Phase B standalone DJI O3 video endpoint (Pi 4).

Replaces the phone: presents as Google AOA accessory 18d1:2d01 to the goggles,
answers capture-derived identity DUML (cs=0 / 0x81, 0x82), deframes live video
chunks (55 CC 4A 57 → H.264), and optionally pipes to ffplay/mpv/file.

Prerequisites (on the Pi, root):
  1. dwc2 peripheral + raw_gadget not holding the UDC
       sudo systemctl stop any usb-proxy; sudo pkill usb-proxy || true
  2. ./scripts/gadget_o3_accessory.sh start
  3. sudo python3 poc/pi_endpoint/endpoint.py --ffs /dev/ffs-o3acc \\
         --bind-udc fe980000.usb --out /tmp/o3live.h264

Then plug goggles → Pi USB-C (VBUS isolated; Pi powered via GPIO).

Capture-derived behaviour only (gold 2026-07-13_103110):
  - Video: 55 CC 4A 57 framing → H.264 High 1080p (analysis/video_deframe.py)
  - Identity: answer 0x81 with 00'APP'… and 0x82 with 00 (aoa_transport.py)
  - No fabricated SPECIAL I-frame trigger: steady-state FPV did not use it.

This is still experimental: goggles may require more DUML than 0x81/0x82 before
video starts when no phone is present. Use --verbose and a --out dump to compare
against the gold MITM capture.
"""
from __future__ import annotations

import argparse
import os
import select
import struct
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "analysis"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from video_deframe import StreamingVideoDeframer  # noqa: E402
from aoa_transport import (  # noqa: E402
    AOA_MAGIC,
    VIDEO_MAGIC,
    aoa_wrap,
    aoa_unwrap_stream,
    maybe_handle_duml,
)
from ffs_descriptors import build_descriptors, build_strings  # noqa: E402

# FunctionFS event types (linux/usb/functionfs.h)
FFS_ENABLE = 2
FFS_DISABLE = 3
FFS_SETUP = 4


def _read_exact(fd: int, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = os.read(fd, n - len(buf))
        if not chunk:
            raise EOFError("functionfs fd closed")
        buf += chunk
    return buf


class FunctionFSAccessory:
    """Minimal FunctionFS client: one bulk OUT (host→us) + one bulk IN (us→host)."""

    def __init__(self, ffs_dir: Path):
        self.ffs_dir = Path(ffs_dir)
        self.ep0 = None
        self.ep_out = None  # host writes here (video + goggles DUML)
        self.ep_in = None   # we write responses here
        self.enabled = False

    def open_and_write_descriptors(self):
        ep0_path = self.ffs_dir / "ep0"
        if not ep0_path.exists():
            raise SystemExit(
                f"{ep0_path} missing — run: sudo ./scripts/gadget_o3_accessory.sh start"
            )
        self.ep0 = os.open(ep0_path, os.O_RDWR)
        descs = build_descriptors()
        strs = build_strings()
        os.write(self.ep0, descs)
        os.write(self.ep0, strs)
        # After descriptors, FunctionFS creates ep* nodes (names vary by kernel).
        time.sleep(0.2)
        self._open_endpoints()

    def _open_endpoints(self):
        # Typical names: ep1, ep2 or ep1out, ep1in — probe.
        candidates = sorted(p for p in self.ffs_dir.iterdir() if p.name.startswith("ep") and p.name != "ep0")
        if len(candidates) < 2:
            # wait a bit longer
            for _ in range(20):
                time.sleep(0.1)
                candidates = sorted(
                    p for p in self.ffs_dir.iterdir() if p.name.startswith("ep") and p.name != "ep0"
                )
                if len(candidates) >= 2:
                    break
        if len(candidates) < 2:
            raise SystemExit(f"FunctionFS endpoints not created yet in {self.ffs_dir}: {list(self.ffs_dir.iterdir())}")
        # Convention from our descriptor order: first non-ep0 is OUT (0x01), second is IN (0x81)
        # Kernel may expose as ep1 / ep2. Open both RDWR.
        self.ep_out = os.open(candidates[0], os.O_RDWR | os.O_NONBLOCK)
        self.ep_in = os.open(candidates[1], os.O_RDWR | os.O_NONBLOCK)
        print(f"[*] FFS endpoints: OUT={candidates[0]} IN={candidates[1]}")

    def bind_udc(self, udc: str, gadget_name: str = "o3acc"):
        gdir = Path(f"/sys/kernel/config/usb_gadget/{gadget_name}")
        udc_path = gdir / "UDC"
        if not udc_path.exists():
            raise SystemExit(f"missing {udc_path} — gadget_o3_accessory.sh start first")
        current = udc_path.read_text().strip()
        if current:
            print(f"[*] UDC already bound: {current}")
            return
        udc_path.write_text(udc)
        print(f"[*] bound UDC {udc}")

    def poll_ep0_events(self):
        """Non-blocking drain of FunctionFS ep0 events."""
        if self.ep0 is None:
            return
        try:
            r, _, _ = select.select([self.ep0], [], [], 0)
            if not r:
                return
            # struct usb_functionfs_event { union {}; __u8 type; __u8 pad[3]; } ~12 bytes
            data = os.read(self.ep0, 12 * 8)
        except BlockingIOError:
            return
        except OSError:
            return
        # walk events (12 bytes each on most kernels)
        for off in range(0, len(data) - 11, 12):
            typ = data[off + 8] if len(data) >= off + 9 else data[off]
            # Actually layout is: 8-byte union + u8 type + pad[3]
            typ = data[off + 8]
            if typ == FFS_ENABLE:
                self.enabled = True
                print("[*] FunctionFS ENABLE (host configured us)")
            elif typ == FFS_DISABLE:
                self.enabled = False
                print("[*] FunctionFS DISABLE")
            elif typ == FFS_SETUP:
                # ctrl request — for a pure bulk accessory we stall unknown setups
                print("[*] FunctionFS SETUP (ignored)")

    def read_host(self, max_bytes: int = 4096 * 4) -> bytes:
        try:
            return os.read(self.ep_out, max_bytes)
        except BlockingIOError:
            return b""
        except OSError as e:
            print(f"[!] read OUT: {e}")
            return b""

    def write_host(self, data: bytes) -> None:
        if not data:
            return
        view = memoryview(data)
        while view:
            try:
                n = os.write(self.ep_in, view)
                view = view[n:]
            except BlockingIOError:
                select.select([], [self.ep_in], [], 0.5)
            except OSError as e:
                print(f"[!] write IN: {e}")
                return

    def close(self):
        for fd in (self.ep_out, self.ep_in, self.ep0):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


def run_session(args):
    ffs = FunctionFSAccessory(Path(args.ffs))
    ffs.open_and_write_descriptors()
    if args.bind_udc:
        ffs.bind_udc(args.bind_udc, gadget_name=args.gadget_name)

    deframer = StreamingVideoDeframer()
    aoa_buf = bytearray()
    out_fh = open(args.out, "wb") if args.out else None
    player = None
    if args.play:
        # Live decode: feed annex-B into ffplay stdin
        player = subprocess.Popen(
            ["ffplay", "-loglevel", "warning", "-f", "h264", "-i", "pipe:0"],
            stdin=subprocess.PIPE,
            bufsize=0,
        )
        print("[*] ffplay started")

    print("[*] waiting for goggles (plug USB-C now). Ctrl-C to stop.")
    last_stats = 0.0
    try:
        while True:
            ffs.poll_ep0_events()
            data = ffs.read_host()
            if not data:
                # also allow ep0-only wakeups
                select.select([ffs.ep_out, ffs.ep0], [], [], 0.05)
                continue

            # Split: video magic chunks vs AOA/DUML control
            # Host bulk stream may interleave both on the same OUT endpoint.
            i = 0
            while i < len(data):
                if data.startswith(VIDEO_MAGIC, i) or (
                    i == 0 and VIDEO_MAGIC in data[:64]
                ):
                    # Prefer feeding whole buffer to video deframer when it looks like video
                    break
                i += 1

            # Feed everything to video deframer (it resyncs on magic)
            new_chunks = deframer.feed(data)
            if new_chunks and out_fh:
                # Write only ES from SPS once available; else buffer via deframer
                pass

            # Also scan for AOA-framed DUML control (55 CC 30 75)
            if AOA_MAGIC in data:
                aoa_buf.extend(data)
                for pkt in aoa_unwrap_stream(aoa_buf):
                    replies = maybe_handle_duml(pkt)
                    for rep in replies:
                        framed = aoa_wrap(rep)
                        ffs.write_host(framed)
                        if args.verbose:
                            print(f"[*] answered DUML cmd_id=0x{rep[10]:02x} ({len(framed)} B AOA)")

            # Also try bare DUML (some paths omit AOA wrapper on bulk)
            if data[:1] == b"\x55" and data[:4] != VIDEO_MAGIC and data[:4] != AOA_MAGIC:
                # single packet attempt
                import duml as duml_mod
                # try scan first packet
                if len(data) >= 13:
                    ln = (data[1] | (data[2] << 8)) & 0x3FF
                    if 13 <= ln <= len(data):
                        replies = maybe_handle_duml(data[:ln])
                        for rep in replies:
                            ffs.write_host(aoa_wrap(rep))

            # Emit ES to file/player once we have SPS
            st = deframer.stats
            if st["has_sps"]:
                es = deframer.elementary_from_sps()
                # write only new tail — track offset
                if not hasattr(run_session, "_es_written"):
                    run_session._es_written = 0
                if len(es) > run_session._es_written:
                    chunk = es[run_session._es_written :]
                    run_session._es_written = len(es)
                    if out_fh:
                        out_fh.write(chunk)
                        out_fh.flush()
                    if player and player.stdin:
                        try:
                            player.stdin.write(chunk)
                        except BrokenPipeError:
                            print("[!] player exited")
                            player = None

            now = time.time()
            if args.verbose and now - last_stats > 2.0:
                last_stats = now
                print(f"[*] video stats: {st}")

    except KeyboardInterrupt:
        print("\n[*] stopping")
    finally:
        if out_fh:
            out_fh.close()
            print(f"[*] wrote {args.out}")
        if player:
            try:
                player.stdin.close()
            except Exception:
                pass
            player.terminate()
        ffs.close()
        print(f"[*] final: {deframer.stats}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ffs", default="/dev/ffs-o3acc", help="FunctionFS mount path")
    ap.add_argument("--bind-udc", default="", help="UDC name to bind after FFS setup (e.g. fe980000.usb)")
    ap.add_argument("--gadget-name", default="o3acc")
    ap.add_argument("--out", default="", help="write H.264 ES to this path")
    ap.add_argument("--play", action="store_true", help="pipe live ES to ffplay")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument(
        "--offline-test",
        metavar="PAYLOAD_LOG",
        help="do not touch USB; deframe a payload log to verify the pipeline",
    )
    args = ap.parse_args()

    if args.offline_test:
        from video_deframe import write_extract
        out = Path(args.out) if args.out else Path(args.offline_test).with_suffix("")
        # write_extract wants a prefix
        prefix = Path(str(out) + "_offline")
        stats = write_extract(Path(args.offline_test), prefix, dirs=("OUT",), make_mp4=True)
        print("[*] offline-test OK:", stats)
        return

    if os.geteuid() != 0:
        print("!! need root for FunctionFS/UDC (sudo)", file=sys.stderr)
        sys.exit(1)
    run_session(args)


if __name__ == "__main__":
    main()
