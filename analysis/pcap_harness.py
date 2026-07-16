#!/usr/bin/env python3
"""
Phase 2 analysis harness for DJI Goggles 2 O3 captures.

Loads a .pcap (from Phase 1 — a rooted-Android DJI Fly session or a passive USB capture
dissected by Wireshark), summarizes IP conversations by bytes/direction, flags the most
likely video flow (the fat one-way goggles->host stream), and dumps that flow's raw
payload to a file so it can be inspected for H.265 NAL start codes / DJI framing.

Usage:
    python3 analysis/pcap_harness.py path/to/capture.pcap [--goggles-ip 192.168.42.2]

Requires: scapy (pip3 install scapy)
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

try:
    from scapy.all import rdpcap, IP, TCP, UDP
except ImportError:
    print("scapy is required: pip3 install scapy", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
import duml

NAL_START_CODE = b"\x00\x00\x00\x01"
NAL_START_CODE_SHORT = b"\x00\x00\x01"

# HEVC NAL unit type is bits [1:6] of the first NAL header byte (after the start code),
# shifted right by 1. VPS=32, SPS=33, PPS=34 are the ones we care about for "is this HEVC".
HEVC_INTERESTING_NAL_TYPES = {32: "VPS", 33: "SPS", 34: "PPS"}


def conversation_key(pkt):
    if IP not in pkt:
        return None
    src, dst = pkt[IP].src, pkt[IP].dst
    sport = dport = None
    proto = None
    if TCP in pkt:
        proto, sport, dport = "TCP", pkt[TCP].sport, pkt[TCP].dport
    elif UDP in pkt:
        proto, sport, dport = "UDP", pkt[UDP].sport, pkt[UDP].dport
    else:
        return None
    # Normalize so both directions of a flow map to the same key.
    endpoints = tuple(sorted([(src, sport), (dst, dport)]))
    return (proto, endpoints)


def summarize(packets):
    """Returns dict: key -> {bytes: int, packets: int, first_src: str, directions: {src: bytes}}"""
    convs = defaultdict(lambda: {"bytes": 0, "packets": 0, "directions": defaultdict(int)})
    for pkt in packets:
        key = conversation_key(pkt)
        if key is None:
            continue
        size = len(pkt)
        convs[key]["bytes"] += size
        convs[key]["packets"] += 1
        convs[key]["directions"][pkt[IP].src] += size
    return convs


def describe_key(key):
    proto, endpoints = key
    (ip_a, port_a), (ip_b, port_b) = endpoints
    return f"{proto} {ip_a}:{port_a} <-> {ip_b}:{port_b}"


def find_nal_start_codes(payload):
    """Return list of (offset, code_len, nal_type_or_None) for start codes found."""
    hits = []
    i = 0
    while True:
        idx4 = payload.find(NAL_START_CODE, i)
        idx3 = payload.find(NAL_START_CODE_SHORT, i)
        candidates = [(idx4, 4) for idx4 in [idx4] if idx4 != -1] + \
                     [(idx3, 3) for idx3 in [idx3] if idx3 != -1]
        if not candidates:
            break
        idx, code_len = min(candidates, key=lambda c: c[0])
        nal_type = None
        header_pos = idx + code_len
        if header_pos < len(payload):
            first_byte = payload[header_pos]
            nal_type = (first_byte >> 1) & 0x3F
        hits.append((idx, code_len, nal_type))
        i = idx + code_len
    return hits


def extract_payload_bytes(packets, key):
    proto, endpoints = key
    out = bytearray()
    for pkt in packets:
        if conversation_key(pkt) != key:
            continue
        if TCP in pkt and pkt[TCP].payload:
            out += bytes(pkt[TCP].payload)
        elif UDP in pkt and pkt[UDP].payload:
            out += bytes(pkt[UDP].payload)
    return bytes(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pcap", type=Path, help="path to capture file")
    ap.add_argument("--goggles-ip", default=None,
                    help="goggles IP if known (helps flag direction); otherwise inferred")
    ap.add_argument("--out-dir", type=Path, default=Path("analysis/dumps"),
                    help="directory to write extracted payloads (default: analysis/dumps)")
    ap.add_argument("--top", type=int, default=15, help="how many conversations to list")
    args = ap.parse_args()

    if not args.pcap.exists():
        print(f"No such file: {args.pcap}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading {args.pcap} ...")
    packets = rdpcap(str(args.pcap))
    print(f"Loaded {len(packets)} packets.")

    convs = summarize(packets)
    ranked = sorted(convs.items(), key=lambda kv: kv[1]["bytes"], reverse=True)

    print(f"\nTop {min(args.top, len(ranked))} conversations by total bytes:")
    print(f"{'#':>3} {'bytes':>12} {'pkts':>8}  conversation")
    for i, (key, stats) in enumerate(ranked[: args.top]):
        print(f"{i:>3} {stats['bytes']:>12} {stats['packets']:>8}  {describe_key(key)}")

    if not ranked:
        print("No IP conversations found in capture.")
        return

    print(f"\nScanning top {min(args.top, len(ranked))} conversations for DUML packets "
          f"(DJI's internal system-bus protocol — see analysis/duml.py)...")
    for key, stats in ranked[: args.top]:
        payload = extract_payload_bytes(packets, key)
        hits = duml.scan(payload)
        if hits:
            cmd_sets = sorted({duml.CMD_SET_NAMES.get(p["cmd_set"], p["cmd_set"]) for _, p in hits}, key=str)
            print(f"  {describe_key(key)}: {len(hits)} DUML packets, cmd_sets seen: {cmd_sets}")

    # Flag the likely video flow: highest-byte-count conversation that is heavily
    # one-directional (one side sends the vast majority of bytes) — video flows
    # goggles->host with only small ACKs/keepalives the other way, unlike a
    # bidirectional control channel.
    likely_video_key = None
    for key, stats in ranked:
        dirs = stats["directions"]
        if len(dirs) < 2:
            continue
        biggest = max(dirs.values())
        total = sum(dirs.values())
        if total > 0 and biggest / total > 0.9:
            likely_video_key = key
            break
    if likely_video_key is None:
        likely_video_key = ranked[0][0]

    print(f"\nLikely video flow (highest volume + one-directional): {describe_key(likely_video_key)}")
    if args.goggles_ip:
        proto, endpoints = likely_video_key
        ips_in_flow = {ip for ip, _port in endpoints}
        if args.goggles_ip not in ips_in_flow:
            print(f"  NOTE: goggles IP {args.goggles_ip} not in this flow — double-check "
                  f"the direction heuristic against your own knowledge of the capture.")

    payload = extract_payload_bytes(packets, likely_video_key)
    print(f"\nExtracted {len(payload)} bytes of payload from likely video flow.")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    proto, endpoints = likely_video_key
    fname_hint = "_".join(f"{ip}-{port}" for ip, port in endpoints).replace(".", "-")
    out_path = args.out_dir / f"{args.pcap.stem}_{proto}_{fname_hint}.bin"
    out_path.write_bytes(payload)
    print(f"Payload written to: {out_path}")

    hits = find_nal_start_codes(payload)
    if hits:
        print(f"\nFound {len(hits)} candidate NAL start codes. First 20:")
        for offset, code_len, nal_type in hits[:20]:
            label = HEVC_INTERESTING_NAL_TYPES.get(nal_type, str(nal_type))
            print(f"  offset={offset:<8} start_code_len={code_len} nal_type={label}")
        print("\n=> Clean Annex-B NAL start codes found: this flow is likely raw/thin-wrapped H.265.")
    else:
        print("\nNo raw NAL start codes found in this flow's payload.")
        print("=> Either this isn't the video flow, or DJI wraps/obfuscates frames before the")
        print("   Annex-B layer (length-prefixed, RTP-ish, or custom header). Inspect the dump")
        print("   with a hex viewer (e.g. `xxd | less`) and look for a repeating header pattern")
        print("   before each apparent frame boundary.")


if __name__ == "__main__":
    main()
