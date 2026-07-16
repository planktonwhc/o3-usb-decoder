#!/usr/bin/env python3
"""
Build the DJI O3 live-view I-frame trigger (cmd_set=1 SPECIAL, cmd_id=0x01).

BACKGROUND (2026-07-13, breakthrough analysis of golden capture
captures/pi_pull/2026-07-13_063725_pi-mitm_payload.log):

The static RE lane (docs/protocol_notes.md "Trigger call chain") predicted the live-view
I-frame request is an 11-byte "special control pack" carried in a cmd_set=1 (SPECIAL)
DUML command, cmd_id UNKNOWN, where:
    pack[5]  = request flags; bit5 (0x20) = "I-frame for live view"
    pack[9]  = XOR-8 of pack[0..8]
    pack[10] = sub-command
    pack[0..4],[6..8] = device/datalink id (runtime)

The golden Pi-MITM capture CONFIRMED all of this on live hardware: DJI Fly emits
cmd_set=1 / cmd_id=0x01 packs to receiver 14 (goggles / OFDM-ground) with exactly this
11-byte layout, and the XOR-8 at pack[9] matches byte-for-byte. cmd_id=0x01 is now
PROVEN (previously only a blocklisted guess). The app's steady-state packs carry
pack[5]=0x04 (NO I-frame bit) because DJI Fly is stuck in the wrong (Mini-3-style)
product UI and never calls RequestIFrameForLiveView. Setting bit5 is what asks for video.

The two steady-state packs actually observed (app -> goggles, receiver 14):
    0000000000040000000401   (66x)
    0300000000040000000701   (18x, pack[0]=0x03 = other datalink/channel)

WHAT THIS SCRIPT DOES:
  * Prints the I-frame variant of each (pack[5] |= 0x20, pack[9] recomputed) as:
      - the raw 11-byte pack,
      - the full CRC-correct DUML packet (ready for Phase-B standalone replay),
      - a usb-proxy `injection_config` "modify" rule that flips an in-flight
        steady-state pack into an I-frame request (recomputing both the XOR-8 and the
        DUML CRC16), so we don't even need DJI Fly to reach FPV mode.

Nothing here is fabricated: every byte derives from a real captured pack; only pack[5]
and the two checksums change. VERIFY on hardware (watch for the video bandwidth jump)
before trusting — payload semantics are RE-derived, and the goggles may still gate
SPECIAL commands behind the unfinished cs=0/0xdd "gvg" activation to receiver 18.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "analysis"))
import duml  # noqa: E402

IFRAME_BIT = 0x20
RECEIVER_GOGGLES = 14  # OFDM-ground / goggles, where DJI Fly sends the real packs

# Steady-state packs captured from DJI Fly (app -> r14). pack[0] distinguishes channel.
OBSERVED_PACKS = [
    bytes.fromhex("0000000000040000000401"),
    bytes.fromhex("0300000000040000000701"),
]


def xor8(data):
    x = 0
    for b in data:
        x ^= b
    return x


def make_iframe_pack(base):
    """Return an 11-byte pack identical to `base` but requesting an I-frame."""
    p = bytearray(base)
    assert len(p) == 11, "special control pack must be 11 bytes"
    p[5] |= IFRAME_BIT           # set 'I-frame for live view'
    p[9] = xor8(p[0:9])          # recompute XOR-8 checksum
    return bytes(p)


def build_full_duml(pack, seq=0x0001):
    """Wrap an 11-byte pack in a complete CRC-correct cmd_set=1/id=0x01 DUML packet,
    app(2) -> goggles(14), request, no-ack (matches the captured framing)."""
    return duml.build(sender_type=2, receiver_type=RECEIVER_GOGGLES, seq=seq,
                      cmd_set=1, cmd_id=0x01, payload=pack,
                      pack_type=0, ack_type=0)


def usbproxy_modify_rules():
    """usb-proxy injection_config for the in-flight flip.

    A static content_pattern/replacement rule CANNOT be used: the DUML seq (bytes 6-7)
    varies per packet and the trailing CRC16 depends on it, so no fixed hex pattern
    matches reliably and no fixed replacement carries a valid CRC. The correct mechanism
    is the Lua transform (poc/pi_mitm/iframe_flip.lua) which scans by structure
    (cmd_set=1/cmd_id=0x01), sets pack[5] bit5, and recomputes XOR-8 + CRC16 per packet.
    Verified byte-exact against a real captured frame (Python mirror of the same logic)."""
    return {"bulk_out": [{"script_file": "iframe_flip.lua",
                          "_comment": "flip DJI Fly's SPECIAL control pack -> I-frame request"}]}


def main():
    print(__doc__)
    print("=" * 78)
    print("I-FRAME TRIGGER ARTIFACTS")
    print("=" * 78)
    for base in OBSERVED_PACKS:
        ifr = make_iframe_pack(base)
        full_orig = build_full_duml(base)
        full_ifr = build_full_duml(ifr)
        assert duml.parse(full_ifr), "built I-frame DUML failed self-parse"
        print(f"\n-- based on captured steady-state pack {base.hex()} (r{RECEIVER_GOGGLES}) --")
        print(f"   11-byte I-frame pack : {ifr.hex()}   (pack[5]=0x{ifr[5]:02x}, xor8[9]=0x{ifr[9]:02x})")
        print(f"   full DUML (replay)   : {full_ifr.hex()}")
        print(f"   (app's original DUML : {full_orig.hex()})")

    print("\n" + "=" * 78)
    print("APPROACH A (in-flight flip via usb-proxy Lua) — needs DJI Fly running so it")
    print("emits the periodic pack; the Lua sets bit5 + fixes checksums, seq-agnostic:")
    print("=" * 78)
    print(json.dumps(usbproxy_modify_rules(), indent=2))
    print("\nAPPROACH B (standalone replay, Phase B / no phone): send the full DUML above")
    print("straight into the gadget IN endpoint toward the goggles (receiver 14).")
    print("\nNOTE: also try pack[5]=0x20 alone, and repeated sends (~5-10 Hz for ~2s),")
    print("mirroring the app's periodic flush. Success signal = video bandwidth jump")
    print("(analysis/aoa_capture_parse.py flags it).")


if __name__ == "__main__":
    main()
