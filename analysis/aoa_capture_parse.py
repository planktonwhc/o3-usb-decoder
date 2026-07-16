#!/usr/bin/env python3
"""
Pi-MITM capture analyser — isolate DJI Fly's live-view START command.

This is the payoff tool for the Raspberry-Pi-4 USB-proxy approach (see
docs/pi4_mitm_setup.md). With the Pi sitting between the goggles (USB host) and the
phone running DJI Fly (USB accessory), *every* byte DJI Fly sends to the goggles
crosses the wire we control — including the exact live-view trigger command that
static RE (runtime-bound transmit, unresolvable) and blind live-enumeration
(cmd_set=1 0x06-0xFF exhausted) both failed to pin down. AppGuard cannot see this
capture: it lives on the USB bus, outside the phone.

Two input formats are accepted (auto-detected):

  1. A usbmon .pcap/.pcapng captured on the Pi's USB-A (host->phone) bus while the
     proxy ran, e.g.:
         sudo modprobe usbmon
         sudo tshark -i usbmon1 -w captures/2026-XX-XX_pi-mitm_fwXXXXX.pcapng
     (usbmon sees the xHCI host<->phone URBs, which carry the full proxied AOA
     stream. The dwc2 *gadget* side is NOT visible to usbmon, so always capture on
     the USB-A host side.)

  2. A raw hex/byte dump — e.g. usb-proxy's own `-v` stdout redirected to a file, or
     any file containing hex. The parser strips non-hex and resyncs on the AOA magic.

What it does:
  - Reassembles the AOA transport framing (55 CC 30 75 + LE32 length + DUML packet;
    confirmed 100%-match framing, see protocol_notes.md "Outer transport framing").
  - Parses every embedded DUML packet with analysis/duml.py (CRC-validated).
  - Builds a time/offset-ordered timeline, tags direction (app->goggles vs
    goggles->app) where the capture format carries it.
  - Estimates a rolling byte-rate and flags the moment bandwidth jumps to video
    scale (~Mbit/s vs the ~10-18 kbit/s telemetry baseline).
  - Prints the app->goggles (host->device) DUML command(s) sent in the window
    immediately BEFORE that jump — the prime suspect(s) for the start-video trigger.

Usage:
    python3 analysis/aoa_capture_parse.py <capture.pcapng|dump.txt> [--video-kbps 500]
    python3 analysis/aoa_capture_parse.py --help
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import duml  # noqa: E402

AOA_MAGIC = bytes([0x55, 0xCC, 0x30, 0x75])
# Direction as seen from the USB HOST (the Pi's USB-A port, facing the phone):
#   OUT = host->phone.  In our topology the phone is the accessory that *emits* DJI
#   Fly's commands, so DJI Fly's app->goggles traffic is phone->host = usbmon "IN".
#   We label by "who spoke" using the DUML sender field, which is unambiguous and
#   format-independent, and cross-check against URB direction when present.


def deframe_aoa(blob):
    """Yield (offset, duml_bytes) for every AOA-framed DUML packet in a byte blob.

    Resyncs on the 4-byte AOA magic so it tolerates gaps, retries, and concatenated
    URB payloads. Falls back to a raw duml.scan() for any region with no magic (in
    case a capture strips the AOA wrapper)."""
    i = 0
    n = len(blob)
    seen_any_magic = False
    while True:
        j = blob.find(AOA_MAGIC, i)
        if j < 0:
            break
        seen_any_magic = True
        if j + 8 > n:
            break
        length = int.from_bytes(blob[j + 4 : j + 8], "little")
        if length < 13 or length > 0x100000 or j + 8 + length > n:
            # bogus length -> not a real frame boundary; step past this magic
            i = j + 4
            continue
        pkt = blob[j + 8 : j + 8 + length]
        parsed = duml.parse(pkt)
        if parsed is not None:
            yield j, pkt, parsed
        i = j + 8 + length
    if not seen_any_magic:
        # No AOA wrapper anywhere — treat the whole blob as a raw DUML stream.
        for off, parsed in duml.scan(blob):
            yield off, blob[off : off + parsed["length"]], parsed


def load_bytes_from_pcap(path):
    """Return a list of (ts, urb_dir, payload_bytes) from a usbmon pcap.

    urb_dir: 'IN' (phone->host, i.e. DJI Fly speaking) / 'OUT' (host->phone) / None."""
    try:
        from scapy.all import rdpcap  # noqa: E402
    except ImportError:
        sys.exit("scapy required for pcap input: .venv/bin/pip install scapy")
    records = []
    for pkt in rdpcap(str(path)):
        raw = bytes(pkt)
        # usbmon linktype: the URB header is 64 bytes; byte 0x0a ('endpoint') high
        # bit = direction (0x80 = IN). Data (if any) follows the header. scapy may or
        # may not dissect usbmon depending on version, so we slice defensively and
        # rely on the AOA magic resync to find real frames regardless of alignment.
        ts = float(pkt.time) if hasattr(pkt, "time") else 0.0
        urb_dir = None
        if len(raw) > 0x0a:
            urb_dir = "IN" if (raw[0x0a] & 0x80) else "OUT"
        payload = raw[64:] if len(raw) > 64 else b""
        if payload:
            records.append((ts, urb_dir, payload))
    return records


def load_bytes_from_proxylog(path):
    """Return [(ts, urb_dir, payload_bytes)] from a patched-usb-proxy payload log.

    Format (one transfer per line): "<epoch.us> <IN|OUT> <ep_hex> <len> <hexbytes>".
    IN = phone->goggles (DJI Fly speaking); OUT = goggles->phone. This is the
    full-fidelity substitute for a usbmon pcap (usbmon binary is broken on the Pi
    kernel and usbmon text truncates at 32 bytes)."""
    records = []
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
        if payload:
            records.append((ts, direction, payload))
    return records


def _is_proxylog(path):
    for line in Path(path).read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        return bool(re.match(r"^\d+\.\d+ (IN|OUT|TXG|TXP) ", line))
    return False


def delivery_audit(path):
    """Compare INGRESS vs DELIVERED per DUML command, using the 4 direction tokens
    the instrumented proxy emits (see proxy.cpp header):
        IN  = app spoke into proxy      TXG = delivered to goggles (app->goggles)
        OUT = goggles spoke into proxy  TXP = delivered to phone   (goggles->app)
    A large IN/TXG gap is the smoking gun for the app->goggles relay dropping
    commands (the 2026-07-13 hypothesis: DJI Fly's version queries never reach the
    goggles, so it never finishes recognition / enters live-view).
    Returns True if TXG/TXP delivery lines were present (i.e. capture is from the
    instrumented proxy), False otherwise."""
    ingress = {"IN": {}, "OUT": {}}
    delivered = {"TXG": {}, "TXP": {}}
    saw_delivery = False
    for line in Path(path).read_text(errors="replace").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        _ts, direction, _ep, _len, hexstr = parts
        if direction in ("TXG", "TXP"):
            saw_delivery = True
        bucket = ingress.get(direction) if direction in ingress else delivered.get(direction)
        if bucket is None:
            continue
        try:
            payload = bytes.fromhex(hexstr.strip())
        except ValueError:
            continue
        for _off, pk in duml.scan(payload):
            key = (pk["cmd_set"], pk["cmd_id"])
            bucket[key] = bucket.get(key, 0) + 1

    print("[*] DELIVERY AUDIT (ingress vs actually delivered across the bus)")
    if not saw_delivery:
        print("    (no TXG/TXP lines — this capture predates the instrumented proxy;")
        print("     re-capture with the rebuilt usb-proxy to get delivery confirmation.)\n")
        return False

    def report(title, in_tok, out_tok, ing, dlv):
        print(f"\n    == {title} ==")
        print(f"    {'cmd_set/id':16s} {in_tok:>7s} {out_tok:>7s}  {'delivered%':>10s}")
        keys = sorted(set(ing) | set(dlv), key=lambda k: -(ing.get(k, 0)))
        for cs, ci in keys:
            i = ing.get((cs, ci), 0)
            d = dlv.get((cs, ci), 0)
            pct = (d / i * 100) if i else float("nan")
            flag = "  <-- DROPPING" if i >= 5 and d < 0.5 * i else ""
            name = duml.CMD_SET_NAMES.get(cs, "?")
            print(f"    cs={cs:<2d}/0x{ci:02x} {name:8.8s} {i:7d} {d:7d}  {pct:9.0f}%{flag}")

    report("app -> goggles", "IN", "TXG", ingress["IN"], delivered["TXG"])
    report("goggles -> app", "OUT", "TXP", ingress["OUT"], delivered["TXP"])
    print()
    return True


def load_bytes_from_hexdump(path):
    """Extract a single byte blob from a text file full of hex (usb-proxy -v etc.).

    Handles both spaced ('55 cc 30 75 ...') and continuous ('55cc3075...') hex by
    concatenating every hex digit in the file and decoding pairwise. Input should be
    pure hex (no `hexdump -C`-style ASCII column, whose letters would corrupt it).
    Common separators/prefixes (0x, :, -, ,) are stripped."""
    text = Path(path).read_text(errors="replace")
    text = re.sub(r"0x", "", text, flags=re.IGNORECASE)
    digits = re.sub(r"[^0-9a-fA-F]", "", text)
    if len(digits) % 2:  # drop a dangling nibble
        digits = digits[:-1]
    return bytes.fromhex(digits)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("capture", help="usbmon .pcap/.pcapng OR a hex/byte dump (.txt)")
    ap.add_argument("--video-kbps", type=float, default=500.0,
                    help="byte-rate threshold (kbit/s) that counts as 'video started' "
                         "(default 500; telemetry baseline measured at ~10-18)")
    ap.add_argument("--window", type=float, default=1.0,
                    help="rolling window seconds for byte-rate (pcap input only)")
    ap.add_argument("--dump-prefix", default=None,
                    help="if set, write pre-trigger app->goggles commands to "
                         "<prefix>_trigger_candidates.txt")
    args = ap.parse_args()

    path = Path(args.capture)
    if not path.exists():
        sys.exit(f"no such file: {path}")

    is_pcap = path.suffix.lower() in (".pcap", ".pcapng") or path.read_bytes()[:4] in (
        b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x0a\x0d\x0d\x0a")
    is_proxylog = not is_pcap and _is_proxylog(path)

    # Build a unified timeline of DUML events: (ts, direction, parsed, nbytes)
    timeline = []
    if is_pcap or is_proxylog:
        if is_proxylog:
            print(f"[*] parsing usb-proxy payload log: {path}")
            delivery_audit(path)
            records = load_bytes_from_proxylog(path)
        else:
            print(f"[*] parsing usbmon pcap: {path}")
            records = load_bytes_from_pcap(path)
        for ts, urb_dir, payload in records:
            for _off, _pkt, parsed in deframe_aoa(payload):
                timeline.append((ts, urb_dir, parsed, len(payload)))
    else:
        print(f"[*] parsing hex/byte dump: {path}")
        blob = load_bytes_from_hexdump(path)
        for off, _pkt, parsed in deframe_aoa(blob):
            timeline.append((off, None, parsed, parsed["length"]))

    if not timeline:
        sys.exit("[!] no AOA-framed DUML packets found. Check the capture side "
                 "(must be the USB-A host bus) and that the proxy was forwarding.")

    print(f"[*] {len(timeline)} DUML packets recovered\n")

    # --- cmd_set histogram (sanity: should look like the passive baseline) ---
    hist = {}
    for _ts, _d, p, _n in timeline:
        key = (p["cmd_set"], p["cmd_id"])
        hist[key] = hist.get(key, 0) + 1
    print("[*] top (cmd_set,cmd_id) by count:")
    for (cs, ci), c in sorted(hist.items(), key=lambda kv: -kv[1])[:15]:
        name = duml.CMD_SET_NAMES.get(cs, "?")
        print(f"      {c:6d}  cmd_set={cs} ({name:14s}) cmd_id=0x{ci:02x}")
    print()

    # --- find the video-bandwidth jump (needs a time axis; hex dump has none) ---
    if is_pcap or is_proxylog:
        base_ts = timeline[0][0]
        # bucket bytes into rolling windows
        thr_Bps = args.video_kbps * 1000 / 8.0
        jump_ts = None
        win = args.window
        # simple sliding: for each event, sum bytes in [ts-win, ts]
        tss = [e[0] for e in timeline]
        for idx, ts in enumerate(tss):
            lo = ts - win
            lo_i = idx
            while lo_i > 0 and tss[lo_i - 1] >= lo:
                lo_i -= 1
            byts = sum(timeline[k][3] for k in range(lo_i, idx + 1))
            if byts / win >= thr_Bps:
                jump_ts = ts
                break
        if jump_ts is None:
            print(f"[!] byte-rate never reached {args.video_kbps} kbit/s — either video "
                  f"never started in this capture, or lower --video-kbps.\n"
                  f"    Peak was ~{max_rate(timeline, win)*8/1000:.1f} kbit/s.")
        else:
            print(f"[+] VIDEO-SCALE BANDWIDTH JUMP at t+{jump_ts-base_ts:.3f}s "
                  f"(>= {args.video_kbps} kbit/s)\n")
            print("[+] app->goggles (phone->host / DUML sender=MOBILE_APP=2) commands "
                  "in the 2.0s BEFORE the jump — PRIME SUSPECTS for start-video:\n")
            candidates = []
            for ts, urb_dir, p, _n in timeline:
                if jump_ts - 2.0 <= ts <= jump_ts:
                    # DJI Fly is sender_type=2 (MOBILE_APP). Keep its requests.
                    if p["sender_type"] == 2 and p["packet_type"] == 0:
                        candidates.append((ts, p))
            if not candidates:
                print("      (none — widen the window, or the sender field differs; "
                      "re-run with a raw dump and inspect manually)")
            for ts, p in candidates:
                print(f"      t+{ts-base_ts:.3f}s  cmd_set={p['cmd_set']} "
                      f"({duml.CMD_SET_NAMES.get(p['cmd_set'],'?')}) "
                      f"cmd_id=0x{p['cmd_id']:02x}  recv={p['receiver_type']}  "
                      f"payload={p['payload'].hex()}")
            if args.dump_prefix and candidates:
                out = Path(f"{args.dump_prefix}_trigger_candidates.txt")
                out.write_text("\n".join(
                    f"cmd_set={p['cmd_set']} cmd_id=0x{p['cmd_id']:02x} "
                    f"recv={p['receiver_type']} payload={p['payload'].hex()}"
                    for _ts, p in candidates))
                print(f"\n[*] wrote {out}")
    else:
        print("[i] hex-dump input has no time axis — showing all app->goggles "
              "(sender=2) requests in order so you can eyeball the trigger:\n")
        for _off, _d, p, _n in timeline:
            if p["sender_type"] == 2 and p["packet_type"] == 0:
                print(f"      cmd_set={p['cmd_set']} "
                      f"({duml.CMD_SET_NAMES.get(p['cmd_set'],'?')}) "
                      f"cmd_id=0x{p['cmd_id']:02x} recv={p['receiver_type']} "
                      f"payload={p['payload'].hex()}")


def max_rate(timeline, win):
    tss = [e[0] for e in timeline]
    best = 0.0
    for idx, ts in enumerate(tss):
        lo = ts - win
        lo_i = idx
        while lo_i > 0 and tss[lo_i - 1] >= lo:
            lo_i -= 1
        byts = sum(timeline[k][3] for k in range(lo_i, idx + 1))
        best = max(best, byts / win)
    return best


if __name__ == "__main__":
    main()
