#!/usr/bin/env python3
"""
Direct (no-Pi) reference-capture auditor for the aoa_sniffer diagnostic app.

Companion to the Pi-MITM DELIVERY AUDIT in aoa_capture_parse.py. When aoa_sniffer
holds the AOA accessory itself (no Pi, no DJI Fly in the path) and probe_battery.sh
fires DUML straight at the goggles, this parses the resulting logcat to answer the
2026-07-13 question directly on real hardware:

    "Do the goggles actually ANSWER cmd_set=0/0x01 version queries, and from which
     modules?"

If they answer here (direct) but DJI Fly got 0-3% answered THROUGH the Pi bridge,
the bridge's app->goggles relay is the confirmed drop point (see the
o3-phaseA-capture-recipe memory + poc/usb_proxy_o3/README.md).

Input: an `adb logcat -s AoaSniffer:V` dump containing the app's own lines:
    SEND cmdset=0x0 cmdid=0x1 recv=14 payload=[]  ->  55cc...
    RESP seq=123 src=14 dst=2 cmdset=0x0 cmdid=0x1 payload=00 12 7a ...

Usage:
    python3 analysis/reference_audit.py <logcat_dump.txt>
"""
import re
import sys
from collections import defaultdict

SEND_RE = re.compile(
    r"SEND cmdset=0x([0-9a-f]+) cmdid=0x([0-9a-f]+) recv=(\d+)")
RESP_RE = re.compile(
    r"RESP seq=(\d+) src=(\d+) dst=(\d+) cmdset=0x([0-9a-f]+) cmdid=0x([0-9a-f]+) payload=(.*)")

# receiver_type low-5-bits -> friendly name (dji-firmware-tools device types)
DEVNAME = {0: "any/0", 1: "CAMERA", 2: "APP", 3: "FLYCTRL", 4: "GIMBAL", 6: "RC",
           7: "WIFI", 8: "DM3xx_SKY", 9: "MCU_SKY", 10: "PC", 11: "BATTERY",
           13: "DM368_GND", 14: "OFDM_GND", 17: "GPS", 31: "0x1f"}


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    text = open(sys.argv[1], errors="replace").read()

    queried = defaultdict(int)                 # (cmdset,cmdid,recv) -> count sent
    answered = defaultdict(int)                 # (cmdset,cmdid,src)  -> count responses
    ver = {}                                    # src -> decoded version-ish payload
    for line in text.splitlines():
        m = SEND_RE.search(line)
        if m:
            cs, ci, rx = int(m.group(1), 16), int(m.group(2), 16), int(m.group(3))
            queried[(cs, ci, rx)] += 1
            continue
        m = RESP_RE.search(line)
        if m:
            src = int(m.group(2))
            cs, ci = int(m.group(4), 16), int(m.group(5), 16)
            pl = m.group(6).strip()
            answered[(cs, ci, src)] += 1
            if (cs, ci) == (0, 1):
                raw = bytes.fromhex(pl.replace(" ", "")) if pl else b""
                ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in raw)
                ver[src] = ascii_

    if not queried and not answered:
        sys.exit("[!] no SEND/RESP lines found — is this an AoaSniffer logcat dump?")

    print("[*] REFERENCE AUDIT (direct phone<->goggles, no Pi)\n")

    # Focus: cmd_set=0/0x01 version queries — the exact thing DJI Fly retry-stormed.
    print("    == GENERAL/0x01 VersionInquiry: which receivers answered ==")
    rxs = sorted({rx for (cs, ci, rx) in queried if (cs, ci) == (0, 1)})
    srcs_ans = {src for (cs, ci, src) in answered if (cs, ci) == (0, 1)}
    print(f"    {'receiver':16s} {'queried':>7s} {'answered':>8s}")
    for rx in rxs:
        sent = queried[(0, 1, rx)]
        got = answered.get((0, 1, rx), 0)
        name = DEVNAME.get(rx, f"t{rx}")
        mark = "" if got else "  <-- silent"
        print(f"    {rx:2d} {name:11s} {sent:7d} {got:8d}{mark}")
    # any module that answered but we didn't explicitly poll (bus is chatty)
    extra = sorted(srcs_ans - set(rxs))
    if extra:
        print(f"    (also answered, unpolled src: {extra})")

    if ver:
        print("\n    == version strings returned (proves the goggles DID respond) ==")
        for src in sorted(ver):
            print(f"    src {src:2d} {DEVNAME.get(src,''):11s} ver='{ver[src]}'")

    # Everything else that answered — full response inventory for reference.
    print("\n    == all responses seen (cmd_set/id: responders) ==")
    bycmd = defaultdict(set)
    for (cs, ci, src), n in answered.items():
        bycmd[(cs, ci)].add(src)
    for (cs, ci) in sorted(bycmd):
        print(f"    cs={cs}/0x{ci:02x}  <- src {sorted(bycmd[(cs,ci)])}")
    print()


if __name__ == "__main__":
    main()
