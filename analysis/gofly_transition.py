#!/usr/bin/env python3
"""
GO-FLY transition dissector.

Context (memory o3-phaseA-capture-recipe, 2026-07-13): the I-frame injection is
proven on the wire, but DJI Fly lands on a Mini-3 flight profile (black camera, no
FPV video path) after GO FLY, so the O3 live-view session never arms. The owner's
reframe: initial recognition is CORRECT ("DJI O3 Air Unit" on the home screen); the
misID happens at the GO-FLY -> live-view TRANSITION. cs=1 (SPECIAL) control packs
first appear at ~t+178s, which marks the GO-FLY press.

This tool:
  1. Locates the GO-FLY moment (first cs=1 SPECIAL packet).
  2. Windows the capture and shows which (dir, cs, id, ptype, sndr->rcvr) tuples are
     NEW after GO FLY vs. what existed on the home screen.
  3. Pairs app->goggles requests to goggles responses by seq and lists app requests
     that got NO response (candidate: bridge-dropped or goggles-refused handshake
     that would pick the flight profile).
"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import duml  # noqa: E402


def load(path):
    for line in Path(path).read_text(errors="replace").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        ts_s, direction, _ep, _len, hexstr = parts
        if direction not in ("IN", "OUT"):
            continue
        try:
            ts = float(ts_s)
            payload = bytes.fromhex(hexstr.strip())
        except ValueError:
            continue
        for _off, pk in duml.scan(payload):
            yield ts, direction, pk


def ascii(b):
    return "".join(chr(c) if 32 <= c < 127 else "." for c in b)


def key(pk, d):
    return (d, pk["cmd_set"], pk["cmd_id"], pk["packet_type"],
            pk["sender_type"], pk["receiver_type"])


def label(k):
    d, cs, cid, pt, st, rt = k
    nm = duml.CMD_SET_NAMES.get(cs, "?")
    typ = "REQ" if pt == 0 else "RSP"
    return f"{d} {typ} cs={cs}({nm}) id=0x{cid:02x} s{st}->r{rt}"


def main():
    path = sys.argv[1]
    pkts = list(load(path))
    t0 = pkts[0][0]

    # --- locate GO FLY: first cs=1 SPECIAL packet ---
    gofly = None
    for ts, d, pk in pkts:
        if pk["cmd_set"] == 1:
            gofly = ts
            break
    if gofly is None:
        print("[!] no cs=1 SPECIAL packet — GO FLY never pressed in this capture")
        gofly = pkts[-1][0]
    print(f"[*] {len(pkts)} DUML packets, span {pkts[-1][0]-t0:.1f}s")
    print(f"[*] GO-FLY (first cs=1) at t+{gofly-t0:.2f}s\n")

    # --- what tuples exist BEFORE GO FLY vs first-seen AFTER GO FLY ---
    before = set()
    after_first = {}
    for ts, d, pk in pkts:
        k = key(pk, d)
        if ts < gofly:
            before.add(k)
        elif k not in before and k not in after_first:
            after_first[k] = (ts, pk)

    print("=" * 78)
    print("NEW command tuples appearing ONLY AFTER GO FLY (the transition exchange)")
    print("=" * 78)
    for k in sorted(after_first, key=lambda k: after_first[k][0]):
        ts, pk = after_first[k]
        print(f"  t+{ts-t0:7.2f}s  {label(k):46s} pl={pk['payload'].hex()[:48]}")

    # --- app requests with no response (by seq), whole capture ---
    # app->goggles = IN, packet_type 0, ack_type != 0 (expects a reply)
    reqs = {}   # (cs,id,seq) -> (ts, pk)
    resps = set()
    for ts, d, pk in pkts:
        if d == "IN" and pk["packet_type"] == 0:
            reqs[(pk["cmd_set"], pk["cmd_id"], pk["seq"])] = (ts, pk)
        elif d == "OUT" and pk["packet_type"] == 1:
            resps.add((pk["cmd_set"], pk["cmd_id"], pk["seq"]))

    unanswered = defaultdict(lambda: [0, None])
    for (cs, cid, seq), (ts, pk) in reqs.items():
        if pk["ack_type"] == 0:
            continue  # fire-and-forget, no reply expected
        if (cs, cid, seq) not in resps:
            gk = (cs, cid, pk["sender_type"], pk["receiver_type"], pk["payload"].hex())
            unanswered[gk][0] += 1
            if unanswered[gk][1] is None:
                unanswered[gk][1] = ts

    print("\n" + "=" * 78)
    print("APP REQUESTS EXPECTING A REPLY (ack_type!=0) THAT GOT NO RESPONSE (by seq)")
    print("=" * 78)
    rows = sorted(unanswered.items(), key=lambda x: x[1][1])
    for (cs, cid, st, rt, hx), (cnt, ts) in rows:
        nm = duml.CMD_SET_NAMES.get(cs, "?")
        b = bytes.fromhex(hx)
        print(f"  t+{ts-t0:7.2f}s  cs={cs}({nm}) id=0x{cid:02x} s{st}->r{rt} [{cnt}x unanswered] "
              f"pl={hx[:40]}  {ascii(b)[:20]}")

    # --- also: app requests answered vs unanswered, summary per (cs,id) ---
    print("\n" + "=" * 78)
    print("ANSWER RATE per app request (cs,id) — low rate = bridge/goggles not replying")
    print("=" * 78)
    per = defaultdict(lambda: [0, 0])  # (cs,id) -> [total_expecting, answered]
    for (cs, cid, seq), (ts, pk) in reqs.items():
        if pk["ack_type"] == 0:
            continue
        per[(cs, cid)][0] += 1
        if (cs, cid, seq) in resps:
            per[(cs, cid)][1] += 1
    for (cs, cid), (tot, ans) in sorted(per.items()):
        nm = duml.CMD_SET_NAMES.get(cs, "?")
        pct = 100 * ans / tot if tot else 0
        flag = "  <== LOW" if pct < 50 else ""
        print(f"  cs={cs}({nm}) id=0x{cid:02x}: {ans}/{tot} answered ({pct:.0f}%){flag}")


if __name__ == "__main__":
    main()
