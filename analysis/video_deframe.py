#!/usr/bin/env python3
"""
Hardened DJI O3 live-video deframer.

Gold capture (2026-07-13 natural-flow) proved goggles→client video is:

  struct video_chunk {
      uint8_t  magic[4];   // 55 CC 4A 57
      uint32_t length;     // LE, payload byte count (NOT including this 8-byte hdr)
      uint8_t  payload[length];
  };

Payloads concatenate to an H.264 High 1920×1080 annex-B elementary stream.
SPS (NAL type 7, hdr 0x67) + PPS (type 8, 0x68) arrive in ~39-byte chunks;
slice NALs follow in larger chunks (often multi-URB: length≈4096 → 4104 B frame
spanning a 4096 B bulk read + 8 B of the next).

Control on the same bulk pipe uses a different outer magic (55 CC 30 75 AOA).
Continuation URBs carry raw payload with NO magic (~40% of gold OUT transfers).

Critical live bug (field 2026-07-14): a naive "always find VIDEO_MAGIC" loop treated
mid-frame payload / AOA bytes as resync gaps. Field stats showed resyncs≈chunks
(every chunk preceded by a skip) → bitty/choppy HDMI even at ~7 Mbit/s with
hdmi_drop=0. Fix: state machine that (1) never searches magic while mid-frame,
(2) peels AOA frames cleanly in IDLE, (3) only counts true garbage as resync.
"""
from __future__ import annotations

import struct
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

VIDEO_MAGIC = bytes([0x55, 0xCC, 0x4A, 0x57])
AOA_MAGIC = bytes([0x55, 0xCC, 0x30, 0x75])
MAX_PAYLOAD = 2_000_000
# Gold: largest common payload is 4096; allow headroom for rare larger slices.
MAX_REASONABLE_PAYLOAD = 256 * 1024


@dataclass
class VideoChunk:
    offset: int  # offset in source stream (if known)
    payload: bytes

    @property
    def is_param_set(self) -> bool:
        """True if payload begins with annex-B SPS/PPS."""
        p = self.payload
        if p.startswith(b"\x00\x00\x00\x01") and len(p) > 4:
            return (p[4] & 0x1F) in (7, 8)
        if p.startswith(b"\x00\x00\x01") and len(p) > 3:
            return (p[3] & 0x1F) in (7, 8)
        return False


def parse_chunks(stream: bytes, start: int = 0) -> Iterator[VideoChunk]:
    """Yield VideoChunks from a byte stream. Resyncs on magic; skips garbage."""
    i = start
    n = len(stream)
    while i + 8 <= n:
        j = stream.find(VIDEO_MAGIC, i)
        if j < 0:
            break
        if j + 8 > n:
            break
        (length,) = struct.unpack_from("<I", stream, j + 4)
        if length == 0 or length > MAX_PAYLOAD:
            i = j + 4
            continue
        end = j + 8 + length
        if end > n:
            break  # truncated — caller may retain stream[j:]
        yield VideoChunk(offset=j, payload=stream[j + 8 : end])
        i = end


class StreamingVideoDeframer:
    """
    Incremental deframer for live bulk OUT data from the goggles.

    feed(data) → list of newly completed chunk payloads

    State machine:
      IDLE     — look for VIDEO_MAGIC or AOA_MAGIC; peel AOA; start video on VIDEO
      IN_VIDEO — read remaining payload bytes only (no magic search)

    retain_payloads=False (live HDMI): do not keep every ES chunk in RAM.
    """

    def __init__(self, retain_payloads: bool = True) -> None:
        self._buf = bytearray()
        self._retain = retain_payloads
        self._chunks: list[bytes] = []
        self._chunk_count = 0
        self._total_payload = 0
        # True stream-loss / garbage skips (not AOA peels, not mid-frame).
        self._resyncs = 0
        self._aoa_peeled = 0
        self._garbage_bytes = 0
        self._bad_length = 0
        self._has_sps = False
        self._sps_index: Optional[int] = None
        # Mid-frame: total frame size (8+payload) once header is valid.
        self._frame_need: Optional[int] = None

    def feed(self, data: bytes) -> list[bytes]:
        """Append bulk bytes; return list of newly completed chunk payloads."""
        if not data:
            return []
        self._buf.extend(data)
        completed: list[bytes] = []
        while True:
            if self._frame_need is not None:
                # Mid-frame: only wait for remaining bytes — never magic-scan payload.
                if len(self._buf) < self._frame_need:
                    break
                payload = bytes(self._buf[8 : self._frame_need])
                del self._buf[: self._frame_need]
                self._frame_need = None
                self._emit_payload(payload, completed)
                continue

            # IDLE: demux AOA vs VIDEO vs garbage.
            if not self._buf:
                break
            vj = self._buf.find(VIDEO_MAGIC)
            aj = self._buf.find(AOA_MAGIC)

            if vj < 0 and aj < 0:
                # Only retain a real split-magic prefix (55 / 55 CC / 55 CC 4A|30).
                # Keeping arbitrary last-3 bytes (old behavior) left junk before the next
                # frame header → resyncs≈chunks live and bitty decode (field 2026-07-14).
                self._trim_idle_tail()
                break

            # Prefer whichever magic appears first.
            if aj >= 0 and (vj < 0 or aj < vj):
                if aj > 0:
                    self._resyncs += 1
                    self._garbage_bytes += aj
                    del self._buf[:aj]
                if not self._peel_aoa_frame():
                    break  # need more bytes for AOA length/body
                continue

            # VIDEO_MAGIC first (vj >= 0, and either no AOA or video earlier).
            if vj > 0:
                self._resyncs += 1
                self._garbage_bytes += vj
                del self._buf[:vj]
            if len(self._buf) < 8:
                break
            (length,) = struct.unpack_from("<I", self._buf, 4)
            if length == 0 or length > MAX_PAYLOAD or length > MAX_REASONABLE_PAYLOAD:
                # Bad length at this magic — slide one byte and recount as bad header.
                del self._buf[0]
                self._bad_length += 1
                self._resyncs += 1
                continue
            self._frame_need = 8 + length
            # Loop again into IN_VIDEO path (may complete immediately if enough data).
            continue

        return completed

    def _trim_idle_tail(self) -> None:
        """When IDLE with no full magic in buf.

        - Prefer keeping a split-magic prefix (55 / 55 CC / …).
        - Strip trailing USB zero-padding (common on short video URBs) without panic.
        - Only then discard residual orphan bytes.
        """
        if not self._buf:
            return
        # Strip trailing NULs (short frame + padded 4 KiB URB) before other logic.
        end = len(self._buf)
        while end > 0 and self._buf[end - 1] == 0:
            end -= 1
        if end < len(self._buf):
            # Count pad separately from true loss; still free the zeros.
            self._garbage_bytes += len(self._buf) - end
            del self._buf[end:]
        if not self._buf:
            return
        keep = 0
        for mag in (VIDEO_MAGIC, AOA_MAGIC):
            for n in range(min(3, len(self._buf)), 0, -1):
                if self._buf[-n:] == mag[:n]:
                    keep = max(keep, n)
                    break
        if keep:
            if len(self._buf) > keep:
                self._garbage_bytes += len(self._buf) - keep
                del self._buf[:-keep]
            return
        # Hold a little — next feed may complete a split header. Only discard if large.
        if len(self._buf) <= 7:
            return
        self._garbage_bytes += len(self._buf)
        self._buf.clear()

    def _peel_aoa_frame(self) -> bool:
        """Consume one AOA frame at buf[0]. True if peeled or discarded; False if need more data."""
        if len(self._buf) < 8:
            return False
        if not self._buf.startswith(AOA_MAGIC):
            # Should not happen; slide.
            del self._buf[0]
            self._resyncs += 1
            return True
        length = int.from_bytes(self._buf[4:8], "little")
        # AOA carries DUML (~13..few KB). Reject absurd lengths.
        if length < 13 or length > 0x10000:
            del self._buf[0]
            self._bad_length += 1
            self._resyncs += 1
            return True
        need = 8 + length
        if len(self._buf) < need:
            return False
        del self._buf[:need]
        self._aoa_peeled += 1
        return True

    def _emit_payload(self, payload: bytes, completed: list[bytes]) -> None:
        self._chunk_count += 1
        self._total_payload += len(payload)
        if _payload_has_sps(payload):
            self._has_sps = True
        if self._retain:
            self._chunks.append(payload)
            if self._sps_index is None and _payload_has_sps(payload):
                self._sps_index = len(self._chunks) - 1
        completed.append(payload)

    def elementary_stream(self) -> bytes:
        if not self._retain:
            raise RuntimeError("elementary_stream() requires retain_payloads=True")
        return b"".join(self._chunks)

    def elementary_from_sps(self) -> bytes:
        es = self.elementary_stream()
        pos = _find_sps(es)
        return es[pos:] if pos is not None else es

    @property
    def stats(self) -> dict:
        return {
            "chunks": self._chunk_count if not self._retain else len(self._chunks),
            "payload_bytes": self._total_payload,
            "buffered": len(self._buf),
            "resyncs": self._resyncs,
            "aoa_peeled": self._aoa_peeled,
            "garbage_bytes": self._garbage_bytes,
            "bad_length": self._bad_length,
            "has_sps": self._has_sps if not self._retain else self._sps_index is not None,
            "mid_frame": self._frame_need is not None,
        }


def _payload_has_sps(p: bytes) -> bool:
    if p.startswith(b"\x00\x00\x00\x01") and len(p) > 4:
        return (p[4] & 0x1F) == 7
    if p.startswith(b"\x00\x00\x01") and len(p) > 3:
        return (p[3] & 0x1F) == 7
    return False


def _find_sps(es: bytes) -> Optional[int]:
    for pat in (b"\x00\x00\x00\x01\x67", b"\x00\x00\x01\x67"):
        j = es.find(pat)
        if j >= 0:
            return j
    return None


def chunks_from_payload_log(path: Path, dirs: Iterable[str] = ("OUT",)) -> list[VideoChunk]:
    """Parse a USBPROXY_PAYLOAD_LOG into ordered video chunks."""
    dirs_set = set(dirs)
    pieces: list[bytes] = []
    with path.open() as f:
        for line in f:
            parts = line.split()
            if len(parts) < 5 or parts[1] not in dirs_set:
                continue
            try:
                pieces.append(bytes.fromhex(parts[4] if len(parts) == 5 else parts[-1]))
            except ValueError:
                continue
    stream = b"".join(pieces)
    return list(parse_chunks(stream))


def extract_elementary(
    path: Path,
    dirs: Iterable[str] = ("OUT",),
    from_sps: bool = True,
) -> tuple[bytes, dict]:
    """Offline extract: return (es_bytes, stats)."""
    chunks = chunks_from_payload_log(path, dirs)
    lengths = Counter(len(c.payload) for c in chunks)
    blob = b"".join(c.payload for c in chunks)
    sps_pos = _find_sps(blob)
    es = blob[sps_pos:] if (from_sps and sps_pos is not None) else blob
    stats = {
        "chunks": len(chunks),
        "payload_bytes": len(blob),
        "es_bytes": len(es),
        "sps_offset": sps_pos,
        "length_hist": lengths.most_common(12),
        "param_set_chunks": sum(1 for c in chunks if c.is_param_set),
    }
    return es, stats


def remux_h264_to_mp4(h264_path: Path, mp4_path: Path, reencode: bool = False) -> None:
    """
    Produce a playable MP4.
    reencode=False tries bitstream copy (may fail if stream is dirty).
    reencode=True uses libx264 (slower, more tolerant of errors).
    """
    if reencode:
        cmd = [
            "ffmpeg", "-y", "-err_detect", "ignore_err",
            "-f", "h264", "-i", str(h264_path),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-an",
            str(mp4_path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-err_detect", "ignore_err",
            "-f", "h264", "-i", str(h264_path),
            "-c", "copy", "-an",
            str(mp4_path),
        ]
    subprocess.run(cmd, check=False, capture_output=True)


def write_extract(
    payload_log: Path,
    out_prefix: Path,
    dirs: Iterable[str] = ("OUT",),
    make_mp4: bool = True,
) -> dict:
    """Full offline extract used by CLI. Writes .from_sps.h264 (+ optional .mp4)."""
    es, stats = extract_elementary(payload_log, dirs=dirs, from_sps=True)
    out_prefix = Path(out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    h264 = Path(str(out_prefix) + ".from_sps.h264")
    h264.write_bytes(es)
    stats["h264_path"] = str(h264)
    # also full concat for debugging
    full, _ = extract_elementary(payload_log, dirs=dirs, from_sps=False)
    Path(str(out_prefix) + ".all.bin").write_bytes(full)
    if make_mp4:
        mp4 = Path(str(out_prefix) + ".mp4")
        remux_h264_to_mp4(h264, mp4, reencode=True)
        stats["mp4_path"] = str(mp4) if mp4.exists() else None
    stats_path = Path(str(out_prefix) + ".stats.txt")
    stats_path.write_text(
        "\n".join(f"{k}: {v}" for k, v in stats.items()) + "\n"
    )
    stats["stats_path"] = str(stats_path)
    return stats
