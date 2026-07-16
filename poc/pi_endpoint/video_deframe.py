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
slice NALs follow in larger chunks (often 4 KiB USB-sized).

This module:
  - parses chunk framing with resync
  - rebuilds ES (optionally from first SPS)
  - provides a streaming deframer for live Phase B use
  - optional ffmpeg remux helpers

Used by analysis/extract_video_stream.py and poc/pi_endpoint/.
"""
from __future__ import annotations

import struct
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional

VIDEO_MAGIC = bytes([0x55, 0xCC, 0x4A, 0x57])
MAX_PAYLOAD = 2_000_000


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

    feed(data) → list of newly completed payload chunks
    elementary_stream() → concat of all payloads so far (only if retain_payloads)
    elementary_from_sps() → ES starting at first SPS (playable; retain mode only)

    retain_payloads=False (live HDMI path): do not keep every ES chunk in RAM —
    multi-minute sessions at ~7 Mbit/s otherwise grow unbounded and GC-stall the
    bulk reader (→ URB drops / choppy picture). Stats still track counts/bytes.
    """

    def __init__(self, retain_payloads: bool = True) -> None:
        self._buf = bytearray()
        self._retain = retain_payloads
        self._chunks: list[bytes] = []
        self._chunk_count = 0
        self._total_payload = 0
        self._resyncs = 0
        self._has_sps = False
        self._sps_index: Optional[int] = None  # index into _chunks (retain mode)

    def feed(self, data: bytes) -> list[bytes]:
        """Append bulk bytes; return list of newly completed chunk payloads."""
        if not data:
            return []
        self._buf.extend(data)
        completed: list[bytes] = []
        while True:
            j = self._buf.find(VIDEO_MAGIC)
            if j < 0:
                # keep a short tail in case magic is split across feeds
                if len(self._buf) > 3:
                    del self._buf[:-3]
                break
            if j > 0:
                self._resyncs += 1
                del self._buf[:j]
            if len(self._buf) < 8:
                break
            (length,) = struct.unpack_from("<I", self._buf, 4)
            if length == 0 or length > MAX_PAYLOAD:
                # bad length — skip one magic byte and resync
                del self._buf[0]
                self._resyncs += 1
                continue
            need = 8 + length
            if len(self._buf) < need:
                break
            payload = bytes(self._buf[8:need])
            del self._buf[:need]
            self._chunk_count += 1
            self._total_payload += len(payload)
            if _payload_has_sps(payload):
                self._has_sps = True
            if self._retain:
                self._chunks.append(payload)
                if self._sps_index is None and _payload_has_sps(payload):
                    self._sps_index = len(self._chunks) - 1
            completed.append(payload)
        return completed

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
            "has_sps": self._has_sps if not self._retain else self._sps_index is not None,
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
                pieces.append(bytes.fromhex(parts[4]))
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
