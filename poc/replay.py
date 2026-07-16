#!/usr/bin/env python3
"""
Phase 3 PoC: replay a captured DJI Fly <-> Goggles 2 session from scratch, no phone.

This is a SKELETON. The handshake bytes, control port, and framing below are all
placeholders — do NOT fabricate real values here. Every TODO must be filled in from a
real Phase 1 capture (see captures/) analyzed with analysis/pcap_harness.py.

Expected flow once filled in:
    1. RNDIS interface is already up (see scripts/phase0_recon.sh) and goggles are
       reachable at GOGGLES_IP.
    2. Open a TCP (or UDP) socket to the control port, send the captured handshake.
    3. Goggles respond; if there's a challenge/response step, reproduce that logic here.
    4. Answer periodic keepalives on the control channel.
    5. Open/read the video socket, strip DJI framing (if any) down to raw H.265 Annex-B,
       and write it out as a .h265 file (or pipe to ffplay for live validation).

Usage:
    python3 poc/replay.py --goggles-ip 192.168.42.2 --out captures/replay_out.h265
    ffplay captures/replay_out.h265        # validate after a run, or pipe live (see below)
"""
import argparse
import socket
import sys
import time
from pathlib import Path

# TODO(Phase 2): confirm these against a real capture. Values here are placeholders only.
CONTROL_PORT = None          # e.g. one of 8901-8904 per CLAUDE.md's port-map notes
VIDEO_PORT = None            # the "fat" one-way flow's port, identified by pcap_harness.py
HANDSHAKE_BYTES = None       # TODO: raw bytes DJI Fly sends to start streaming. NEVER fabricate
                              # this — copy it verbatim from a decoded Phase 1 pcap.
KEEPALIVE_BYTES = None        # TODO: periodic small packet(s) observed on the control channel.
KEEPALIVE_INTERVAL_S = 1.0    # TODO: measure actual cadence from the capture.

# TODO(Phase 2): does the video flow carry raw Annex-B NAL units, or are they wrapped
# (length-prefixed / RTP-like / custom header)? If wrapped, implement stripping here.
def strip_framing(raw_chunk: bytes) -> bytes:
    """Placeholder passthrough. Replace once framing is understood (see docs/)."""
    return raw_chunk


def do_handshake(control_sock: socket.socket) -> None:
    if HANDSHAKE_BYTES is None:
        raise NotImplementedError(
            "HANDSHAKE_BYTES is not set. Capture a real Phase 1 session, analyze it with "
            "analysis/pcap_harness.py, and paste the exact bytes DJI Fly sends here before "
            "attempting a real connection. Do not guess these values."
        )
    control_sock.sendall(HANDSHAKE_BYTES)
    # TODO: read + validate the goggles' response. If there's a challenge/response
    # transform (see CLAUDE.md "Risks & gotchas"), implement it here.
    reply = control_sock.recv(4096)
    print(f"[handshake] goggles replied with {len(reply)} bytes: {reply[:64]!r}...")


def keepalive_loop(control_sock: socket.socket, stop_flag: list) -> None:
    if KEEPALIVE_BYTES is None:
        print("[keepalive] KEEPALIVE_BYTES not set — skipping keepalives (may cause "
              "the goggles to stop streaming after their timeout).", file=sys.stderr)
        return
    while not stop_flag[0]:
        control_sock.sendall(KEEPALIVE_BYTES)
        time.sleep(KEEPALIVE_INTERVAL_S)


def read_video(goggles_ip: str, video_port: int, out_path: Path, duration_s: float) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as vsock:
        vsock.connect((goggles_ip, video_port))
        deadline = time.monotonic() + duration_s
        with out_path.open("wb") as out_f:
            total = 0
            while time.monotonic() < deadline:
                chunk = vsock.recv(65536)
                if not chunk:
                    print("[video] connection closed by goggles.")
                    break
                out_f.write(strip_framing(chunk))
                total += len(chunk)
            print(f"[video] wrote {total} bytes to {out_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--goggles-ip", required=True, help="goggles IP on the RNDIS link, e.g. 192.168.42.2")
    ap.add_argument("--out", type=Path, default=Path("captures/replay_out.h265"))
    ap.add_argument("--duration", type=float, default=30.0, help="seconds of video to capture")
    args = ap.parse_args()

    if CONTROL_PORT is None or VIDEO_PORT is None:
        print(
            "CONTROL_PORT / VIDEO_PORT are not set. Run scripts/phase0_recon.sh to enumerate "
            "open ports, capture a real session (Phase 1), and analyze it with "
            "analysis/pcap_harness.py to identify the control vs. video ports. Then fill in "
            "the TODOs at the top of this file.",
            file=sys.stderr,
        )
        sys.exit(1)

    args.out.parent.mkdir(parents=True, exist_ok=True)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as csock:
        csock.connect((args.goggles_ip, CONTROL_PORT))
        do_handshake(csock)

        stop_flag = [False]
        # NOTE: for a real implementation, run keepalive_loop in a background thread
        # while read_video runs in the main thread. Left single-threaded here since
        # everything above is still a placeholder.
        read_video(args.goggles_ip, VIDEO_PORT, args.out, args.duration)
        stop_flag[0] = True

    print(f"Done. Validate with: ffplay {args.out}")


if __name__ == "__main__":
    main()
