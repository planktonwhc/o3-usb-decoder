#!/usr/bin/env python3
"""Dump SPECIAL (cs=1) + low-freq identity exchanges, and first-occurrence timeline."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import duml

def load(path):
    for line in Path(path).read_text(errors="replace").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        ts_s, direction, _ep, _len, hexstr = parts
        if direction in ("TXG", "TXP") or direction not in ("IN", "OUT"):
            continue
        try:
            ts = float(ts_s); payload = bytes.fromhex(hexstr.strip())
        except ValueError:
            continue
        for _off, pk in duml.scan(payload):
            yield ts, direction, pk

def ascii(b): return "".join(chr(c) if 32<=c<127 else "." for c in b)

path = sys.argv[1]
pkts = list(load(path))
t0 = pkts[0][0]

print("="*78); print("cs=1 (SPECIAL) packets — both directions"); print("="*78)
seen = {}
for ts, d, pk in pkts:
    if pk["cmd_set"] != 1: continue
    k = (d, pk["cmd_id"], pk["packet_type"], pk["sender_type"], pk["receiver_type"], pk["payload"].hex())
    if k in seen:
        seen[k][0]+=1; continue
    seen[k]=[1, ts]
for (d, cid, pt, st, rt, hx), (cnt, ts) in sorted(seen.items(), key=lambda x:x[1][1]):
    typ = "REQ" if pt==0 else "RSP"
    print(f"  t+{ts-t0:6.2f}s {d} {typ} id=0x{cid:02x} s{st}->r{rt} [{cnt}x] pl={hx}  {ascii(bytes.fromhex(hx))}")

for target_cs, target_id in [(0,0xdd),(0,0x07),(2,0xb5),(2,0x54)]:
    print("\n"+"="*78); print(f"cs={target_cs} id=0x{target_id:02x} exchanges"); print("="*78)
    for ts, d, pk in pkts:
        if pk["cmd_set"]==target_cs and pk["cmd_id"]==target_id:
            typ="REQ" if pk["packet_type"]==0 else "RSP"
            print(f"  t+{ts-t0:6.2f}s {d} {typ} s{pk['sender_type']}->r{pk['receiver_type']} pl={pk['payload'].hex()}  {ascii(pk['payload'])}")

print("\n"+"="*78); print("FIRST-OCCURRENCE TIMELINE (each cs/id/dir/ptype), first 40 by time"); print("="*78)
first={}
for ts,d,pk in pkts:
    k=(d,pk["cmd_set"],pk["cmd_id"],pk["packet_type"])
    if k not in first: first[k]=ts
for (d,cs,cid,pt),ts in sorted(first.items(), key=lambda x:x[1])[:40]:
    nm=duml.CMD_SET_NAMES.get(cs,"?"); typ="REQ" if pt==0 else "RSP"
    print(f"  t+{ts-t0:7.3f}s  {d} {typ} cs={cs}({nm}) id=0x{cid:02x}")
