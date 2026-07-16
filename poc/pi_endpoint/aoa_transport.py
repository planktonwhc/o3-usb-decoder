"""AOA outer framing + DUML session helpers for Phase B (capture-derived)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "analysis"))
import duml  # noqa: E402

# Outer AOA control-plane framing (CONFIRMED).
AOA_MAGIC = bytes([0x55, 0xCC, 0x30, 0x75])
# Video bulk framing (CONFIRMED gold capture).
VIDEO_MAGIC = bytes([0x55, 0xCC, 0x4A, 0x57])

# DJI Fly identity strings (CONFIRMED adb dumpsys / aoa_trigger).
AOA_STRINGS = {
    0: "DJI",
    1: "com.dji.logiclink",
    2: "DJI glass",
    3: "v0.0.0.0",
    4: "www.dji.com",
    5: "0",
}

MOBILE_APP = 2  # DUML sender_type for the phone/app role we emulate


def aoa_wrap(duml_pkt: bytes) -> bytes:
    return AOA_MAGIC + len(duml_pkt).to_bytes(4, "little") + duml_pkt


def aoa_unwrap_stream(buf: bytearray):
    """
    Consume complete AOA-framed DUML packets from buf (mutates buf).
    Yields raw DUML packet bytes.
    """
    while True:
        j = bytes(buf).find(AOA_MAGIC)
        if j < 0:
            if len(buf) > 3:
                del buf[:-3]
            return
        if j > 0:
            del buf[:j]
        if len(buf) < 8:
            return
        length = int.from_bytes(buf[4:8], "little")
        if length < 13 or length > 0x100000:
            del buf[0]
            continue
        need = 8 + length
        if len(buf) < need:
            return
        pkt = bytes(buf[8:need])
        del buf[:need]
        if duml.parse(pkt) is not None:
            yield pkt


# Exact app→goggles response payloads, lifted byte-for-byte from the gold natural-flow
# capture (2026-07-13_103110). These are what real DJI Fly replies to the goggles'
# identity/status probes; using the captured bytes verbatim avoids the earlier hand-built
# 0x81 template that had 051c at the wrong offset.
#   cs=0/0x81 → app reply (64B), identity ack: 00 'APP' … 02000000 … 051c …
#   cs=0/0x82 → app reply (1B): 00
#   cs=0/0x88 → app reply (5B): 1a 00 00 00 00  (goggles req payload was 19 00)
_IDENTITY_RESP = {
    0x81: bytes.fromhex(
        "0041505000000000000000000000000000000000000000000000000000000000"
        "020000000000000000000000051c000000000000000000000000000000000000"
    ),
    0x82: bytes.fromhex("00"),
    0x88: bytes.fromhex("1a00000000"),
}


def build_identity_response(req: dict) -> bytes:
    """
    Answer cs=0 / cmd_id 0x81 / 0x82 / 0x88 the way DJI Fly does in the gold capture.
    packet_type=response(1), ack_type=0, swap sender/receiver, echo seq.
    """
    cmd_id = req["cmd_id"]
    seq = req["seq"]
    # Request came from goggles-side module → we reply as APP(2) back to that module.
    recv_type = req["sender_type"]
    recv_idx = req["sender_idx"]

    pl = _IDENTITY_RESP.get(cmd_id)
    if pl is None:
        raise ValueError(f"not an identity cmd: 0x{cmd_id:02x}")

    # sender_type embeds index in high bits of the address byte inside duml.build
    # analysis/duml.py uses sender_type / receiver_type as 5-bit type only; idx via kwargs if any.
    # Check duml.build signature.
    return duml.build(
        sender_type=MOBILE_APP,
        receiver_type=recv_type,
        seq=seq,
        cmd_set=0,
        cmd_id=cmd_id,
        payload=pl,
        pack_type=1,  # response
        ack_type=0,
        sender_idx=0,
        receiver_idx=recv_idx,
    )


def maybe_handle_duml(pkt: bytes) -> list[bytes]:
    """
    Given a raw DUML request from the goggles, return zero or more DUML response
    packets to send back (not yet AOA-wrapped).
    """
    p = duml.parse(pkt)
    if not p:
        return []
    # Only handle requests (packet_type=0) that expect a reply.
    if p["packet_type"] != 0:
        return []
    if p["cmd_set"] == 0 and p["cmd_id"] in (0x81, 0x82, 0x88):
        # Gold: app answers 0x81/0x82/0x88 from goggles modules (esp. type 28 ZV900).
        try:
            return [build_identity_response(p)]
        except Exception:
            return []
    # Version inquiry etc. — extend as we learn more from gold.
    if p["cmd_set"] == 0 and p["cmd_id"] == 0x01 and p["ack_type"] != 0:
        # Minimal version-style reply: empty success is wrong; leave unanswered for now
        # unless we capture a stable template. Video flowed without us answering all.
        return []
    return []
