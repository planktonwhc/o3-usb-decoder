#!/usr/bin/env python3
"""
ProductType hunt — find the DUML exchange that tells DJI Fly what product it's
talking to, in the 2026-07-13 golden capture.

Context (see memory o3-phaseA-capture-recipe + docs/protocol_notes.md):
DJI Fly RECOGNIZES the goggles through the Pi bridge now, but misidentifies the
ProductType (renders a Mini-3-style standard-camera UI, never the FPV/live-view
screen) so it never arms the cmd_set=1 RequestIFrameForLiveView path. We want the
goggles->app response that carries the product-type field, so we can spoof the
correct value via usb-proxy DUML injection.

Strategy:
  - Decode every DUML packet with direction (IN=app->goggles, OUT=goggles->app).
  - Bucket by (dir, sender_type, cmd_set, cmd_id, packet_type) and show the DISTINCT
    payloads for each -- product identity is a low-cardinality, mostly-static field,
    so responses whose payload is constant across the session are prime candidates.
  - Pair requests to responses by seq to label who-asked-what.
  - Highlight GENERAL (cs=0) responses (0x01 version, 0x07/0xdd product-ish) and any
    payload containing recognizable ASCII (firmware/product strings).
"""
import sys
from collections import defaultdict, Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import duml  # noqa: E402


def load(path):
    """Yield (ts, direction, parsed) for every DUML packet in the proxy log."""
    for line in Path(path).read_text(errors="replace").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        ts_s, direction, _ep, _len, hexstr = parts
        if direction not in ("IN", "OUT", "TXG", "TXP"):
            continue
        # ingress tokens only (IN/OUT); TXG/TXP are delivery echoes of the same bytes
        if direction in ("TXG", "TXP"):
            continue
        try:
            ts = float(ts_s)
            payload = bytes.fromhex(hexstr.strip())
        except ValueError:
            continue
        for _off, pk in duml.scan(payload):
            yield ts, direction, pk


def printable(b):
    return "".join(chr(c) if 32 <= c < 127 else "." for c in b)


def main():
    path = sys.argv[1]
    pkts = list(load(path))
    print(f"[*] {len(pkts)} DUML packets\n")

    # --- distinct payloads per (dir, cs, id, ptype) ---
    buckets = defaultdict(Counter)
    for ts, d, pk in pkts:
        key = (d, pk["cmd_set"], pk["cmd_id"], pk["packet_type"],
               pk["sender_type"], pk["receiver_type"])
        buckets[key][pk["payload"].hex()] += 1

    # Focus: goggles->app RESPONSES (packet_type=1) with low payload cardinality,
    # esp. GENERAL. These carry stable identity/version info.
    print("=" * 78)
    print("GOGGLES->APP RESPONSES (packet_type=1), sorted: GENERAL first, then by cs")
    print("=" * 78)
    resp_keys = [k for k in buckets if k[0] == "OUT" and k[3] == 1]
    resp_keys.sort(key=lambda k: (k[1] != 0, k[1], k[2]))
    for k in resp_keys:
        d, cs, cid, pt, st, rt = k
        payloads = buckets[k]
        name = duml.CMD_SET_NAMES.get(cs, "?")
        nuniq = len(payloads)
        total = sum(payloads.values())
        # skip the huge high-rate streaming telemetry with tons of unique payloads
        flag = ""
        if nuniq <= 6:
            flag = "  <== LOW-CARD (identity candidate)"
        print(f"\ncs={cs}({name}) id=0x{cid:02x} sndr={st} rcvr={rt}  "
              f"{total} pkts, {nuniq} distinct payload(s){flag}")
        if nuniq <= 8:
            for hx, cnt in payloads.most_common():
                b = bytes.fromhex(hx)
                print(f"    [{cnt:5d}x] len={len(b):3d} {hx}")
                if any(32 <= c < 127 for c in b):
                    print(f"             ascii: {printable(b)}")


if __name__ == "__main__":
    main()
