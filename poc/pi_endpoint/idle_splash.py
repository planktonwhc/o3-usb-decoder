#!/usr/bin/env python3
"""Idle DRM splash — large "waiting for live FPV feed" on HDMI until video takes over.

Used by HdmiPlayer when O3_IDLE_SPLASH=1 (default). Holds DRM master with a static
XRGB8888 frame so the Linux text console is never the idle screen. The video
presenter (atomic_drm_present) replaces it: the endpoint SIGTERMs this process
before spawning the decoder, so master is free for the live path.

Seamless feed→splash handoff (2026-07-16):
  The endpoint may spawn us *while the video presenter still holds DRM master*,
  then kill the presenter. We spin tightly on drmSetMaster so we grab the card
  the instant the player drops it — no console flash. After a successful modeset
  we print a single line "READY" on stdout so the parent can wait for paint.

Rules (compatible with the 2026-07-14 atomic presenter discipline):
  - Modeset once, hold the FB; do not thrash.
  - Exit cleanly on SIGTERM/SIGINT and DropMaster so the next DRM client can grab.
  - If no connector yet, poll (do not exit) until one appears or we are signalled.
  - Never decode video; zero GStreamer deps.

Env / CLI:
  DRM_DEVICE          default /dev/dri/card1
  DRM_CONNECTOR       default HDMI-A-2 (or 'auto' = first connected HDMI-A-*)
  O3_IDLE_SPLASH_TEXT default "waiting for live FPV feed"
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import mmap
import os
import signal
import sys
import time

import dual_drm_mirror as ddm  # noqa: E402

# fourcc XRGB8888
DRM_FORMAT_XRGB8888 = ddm.DRM_FORMAT_XRGB8888
DRM_MODE_CONNECTOR_HDMIA = 11

# --- tiny 5×7 bitmap font (MSB = left) for printable ASCII subset ------------
# Each glyph is 7 rows of 5-bit masks. Missing chars fall back to a hollow box.
_FONT: dict[str, tuple[int, ...]] = {
    " ": (0, 0, 0, 0, 0, 0, 0),
    "!": (0x04, 0x04, 0x04, 0x04, 0x00, 0x04, 0x00),
    '"': (0x0A, 0x0A, 0x00, 0x00, 0x00, 0x00, 0x00),
    "#": (0x0A, 0x1F, 0x0A, 0x1F, 0x0A, 0x00, 0x00),
    "%": (0x19, 0x1A, 0x04, 0x0B, 0x13, 0x00, 0x00),
    "'": (0x04, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00),
    "(": (0x02, 0x04, 0x04, 0x04, 0x02, 0x00, 0x00),
    ")": (0x08, 0x04, 0x04, 0x04, 0x08, 0x00, 0x00),
    "+": (0x00, 0x04, 0x1F, 0x04, 0x00, 0x00, 0x00),
    ",": (0x00, 0x00, 0x00, 0x00, 0x04, 0x08, 0x00),
    "-": (0x00, 0x00, 0x1F, 0x00, 0x00, 0x00, 0x00),
    ".": (0x00, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00),
    "/": (0x01, 0x02, 0x04, 0x08, 0x10, 0x00, 0x00),
    "0": (0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E),
    "1": (0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "2": (0x0E, 0x11, 0x01, 0x06, 0x08, 0x10, 0x1F),
    "3": (0x1F, 0x01, 0x02, 0x06, 0x01, 0x11, 0x0E),
    "4": (0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02),
    "5": (0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E),
    "6": (0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E),
    "7": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08),
    "8": (0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E),
    "9": (0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C),
    ":": (0x00, 0x04, 0x00, 0x00, 0x04, 0x00, 0x00),
    ";": (0x00, 0x04, 0x00, 0x00, 0x04, 0x08, 0x00),
    "=": (0x00, 0x1F, 0x00, 0x1F, 0x00, 0x00, 0x00),
    "?": (0x0E, 0x11, 0x01, 0x02, 0x04, 0x00, 0x04),
    "A": (0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "B": (0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E),
    "C": (0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E),
    "D": (0x1E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x1E),
    "E": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F),
    "F": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10),
    "G": (0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F),
    "H": (0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "I": (0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "J": (0x01, 0x01, 0x01, 0x01, 0x11, 0x11, 0x0E),
    "K": (0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11),
    "L": (0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F),
    "M": (0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11),
    "N": (0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11),
    "O": (0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "P": (0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10),
    "Q": (0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D),
    "R": (0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11),
    "S": (0x0E, 0x11, 0x10, 0x0E, 0x01, 0x11, 0x0E),
    "T": (0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04),
    "U": (0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "V": (0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04),
    "W": (0x11, 0x11, 0x11, 0x15, 0x15, 0x1B, 0x11),
    "X": (0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11),
    "Y": (0x11, 0x11, 0x0A, 0x04, 0x04, 0x04, 0x04),
    "Z": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F),
    "_": (0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x1F),
    "a": (0x00, 0x00, 0x0E, 0x01, 0x0F, 0x11, 0x0F),
    "b": (0x10, 0x10, 0x1E, 0x11, 0x11, 0x11, 0x1E),
    "c": (0x00, 0x00, 0x0E, 0x11, 0x10, 0x11, 0x0E),
    "d": (0x01, 0x01, 0x0F, 0x11, 0x11, 0x11, 0x0F),
    "e": (0x00, 0x00, 0x0E, 0x11, 0x1F, 0x10, 0x0E),
    "f": (0x06, 0x08, 0x1C, 0x08, 0x08, 0x08, 0x08),
    "g": (0x00, 0x00, 0x0F, 0x11, 0x0F, 0x01, 0x0E),
    "h": (0x10, 0x10, 0x1E, 0x11, 0x11, 0x11, 0x11),
    "i": (0x04, 0x00, 0x0C, 0x04, 0x04, 0x04, 0x0E),
    "j": (0x02, 0x00, 0x06, 0x02, 0x02, 0x12, 0x0C),
    "k": (0x10, 0x10, 0x12, 0x14, 0x18, 0x14, 0x12),
    "l": (0x0C, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "m": (0x00, 0x00, 0x1A, 0x15, 0x15, 0x15, 0x15),
    "n": (0x00, 0x00, 0x1E, 0x11, 0x11, 0x11, 0x11),
    "o": (0x00, 0x00, 0x0E, 0x11, 0x11, 0x11, 0x0E),
    "p": (0x00, 0x00, 0x1E, 0x11, 0x1E, 0x10, 0x10),
    "q": (0x00, 0x00, 0x0F, 0x11, 0x0F, 0x01, 0x01),
    "r": (0x00, 0x00, 0x16, 0x19, 0x10, 0x10, 0x10),
    "s": (0x00, 0x00, 0x0F, 0x10, 0x0E, 0x01, 0x1E),
    "t": (0x08, 0x08, 0x1C, 0x08, 0x08, 0x08, 0x06),
    "u": (0x00, 0x00, 0x11, 0x11, 0x11, 0x11, 0x0F),
    "v": (0x00, 0x00, 0x11, 0x11, 0x11, 0x0A, 0x04),
    "w": (0x00, 0x00, 0x11, 0x15, 0x15, 0x15, 0x0A),
    "x": (0x00, 0x00, 0x11, 0x0A, 0x04, 0x0A, 0x11),
    "y": (0x00, 0x00, 0x11, 0x11, 0x0F, 0x01, 0x0E),
    "z": (0x00, 0x00, 0x1F, 0x02, 0x04, 0x08, 0x1F),
}
_BOX = (0x1F, 0x11, 0x11, 0x11, 0x11, 0x11, 0x1F)
_GLYPH_W, _GLYPH_H = 5, 7
_GLYPH_GAP = 1  # blank column between glyphs


def log(msg: str) -> None:
    print(f"[idle-splash] {msg}", file=sys.stderr, flush=True)


def _bind_master(lib) -> None:
    lib.drmSetMaster.restype = ctypes.c_int
    lib.drmSetMaster.argtypes = [ctypes.c_int]
    lib.drmDropMaster.restype = ctypes.c_int
    lib.drmDropMaster.argtypes = [ctypes.c_int]


def _unbind_vtconsole() -> None:
    """Stop fbcon from fighting the plane while we hold DRM master."""
    for vt in ("vtcon0", "vtcon1", "vtcon2"):
        path = f"/sys/class/vtconsole/{vt}/bind"
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("0\n")
        except OSError:
            pass


def _wrap_words(text: str, max_chars: int) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    cur = words[0]
    for w in words[1:]:
        if len(cur) + 1 + len(w) <= max_chars:
            cur = f"{cur} {w}"
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def _pick_layout(text: str, width: int, height: int) -> tuple[list[str], int, int, int]:
    """Return (lines, scale, total_text_w, total_text_h) sized to fit ~70% of the frame."""
    # Prefer a large single line when it fits; else wrap to 2–3 lines.
    for scale in range(18, 3, -1):
        cell_w = (_GLYPH_W + _GLYPH_GAP) * scale
        cell_h = _GLYPH_H * scale
        max_chars = max(8, (width * 85 // 100) // cell_w)
        lines = _wrap_words(text, max_chars)
        if len(lines) > 3:
            continue
        tw = max(len(ln) for ln in lines) * cell_w
        th = len(lines) * cell_h + (len(lines) - 1) * (scale * 2)
        if tw <= width * 90 // 100 and th <= height * 55 // 100:
            return lines, scale, tw, th
    # Fallback: small but always fits.
    scale = 4
    cell_w = (_GLYPH_W + _GLYPH_GAP) * scale
    max_chars = max(8, (width * 90 // 100) // cell_w)
    lines = _wrap_words(text, max_chars)
    cell_h = _GLYPH_H * scale
    tw = max(len(ln) for ln in lines) * cell_w
    th = len(lines) * cell_h + (len(lines) - 1) * (scale * 2)
    return lines, scale, tw, th


def _draw_glyph(
    mv: memoryview,
    pitch: int,
    x0: int,
    y0: int,
    ch: str,
    scale: int,
    color: int,
    width: int,
    height: int,
) -> None:
    rows = _FONT.get(ch, _BOX)
    # color is 0x00BBGGRR little-endian for XRGB8888 memory on LE (B,G,R,X)
    b = color & 0xFF
    g = (color >> 8) & 0xFF
    r = (color >> 16) & 0xFF
    px = bytes((b, g, r, 0x00))
    for row_i, bits in enumerate(rows):
        for col in range(_GLYPH_W):
            if not (bits & (1 << (_GLYPH_W - 1 - col))):
                continue
            for dy in range(scale):
                yy = y0 + row_i * scale + dy
                if yy < 0 or yy >= height:
                    continue
                row_off = yy * pitch
                for dx in range(scale):
                    xx = x0 + col * scale + dx
                    if xx < 0 or xx >= width:
                        continue
                    off = row_off + xx * 4
                    mv[off : off + 4] = px


def paint_splash(
    mp: mmap.mmap,
    pitch: int,
    width: int,
    height: int,
    text: str,
    bg: int = 0x000000,
    fg: int = 0xE8E8E8,
) -> None:
    """Fill black and draw centered multi-line text."""
    # Background fill (fast path for pitch == width*4).
    bg_b = bytes((bg & 0xFF, (bg >> 8) & 0xFF, (bg >> 16) & 0xFF, 0x00))
    if pitch == width * 4:
        mp[:] = bg_b * (width * height)
    else:
        row = bg_b * width
        for y in range(height):
            mp[y * pitch : y * pitch + width * 4] = row

    lines, scale, tw, th = _pick_layout(text, width, height)
    cell_w = (_GLYPH_W + _GLYPH_GAP) * scale
    cell_h = _GLYPH_H * scale
    line_gap = scale * 2
    x_base = max(0, (width - tw) // 2)
    y = max(0, (height - th) // 2)
    mv = memoryview(mp)
    for ln in lines:
        lw = len(ln) * cell_w
        x = x_base + max(0, (tw - lw) // 2)
        for ch in ln:
            _draw_glyph(mv, pitch, x, y, ch, scale, fg, width, height)
            x += cell_w
        y += cell_h + line_gap


class IdleSplash:
    def __init__(self, device: str, connector_name: str | None, text: str):
        self.device = device
        self.connector_name = connector_name  # None = first connected HDMI-A-*
        self.text = text
        self.fd = -1
        self.lib = ddm.LibDrm().lib
        _bind_master(self.lib)
        self._stop = False
        self._fb_id = 0
        self._handle = 0
        self._map: mmap.mmap | None = None

    def request_stop(self, *_a) -> None:
        self._stop = True

    def open(self) -> None:
        self.fd = os.open(self.device, os.O_RDWR | os.O_CLOEXEC)
        self.lib.drmSetClientCap(self.fd, ddm.DRM_CLIENT_CAP_UNIVERSAL_PLANES, 1)
        # Master may still be held by the live presenter — that is expected during
        # the seamless handoff; ensure_master() will spin until it frees.
        if self.lib.drmSetMaster(self.fd) != 0:
            log("drmSetMaster deferred — waiting for previous master to release")

    def ensure_master(self, timeout: float = 2.0) -> bool:
        """Poll drmSetMaster until we own the card (or timeout / stop).

        Used for feed→splash handoff: parent spawns us, then kills the video
        presenter; we grab master within ~one 50ms tick of the player dropping it.
        """
        if self.fd < 0:
            return False
        # Fast path: already master (or free).
        if self.lib.drmSetMaster(self.fd) == 0:
            return True
        t0 = time.monotonic()
        while not self._stop and (time.monotonic() - t0) < max(0.05, timeout):
            time.sleep(0.05)
            if self.lib.drmSetMaster(self.fd) == 0:
                return True
        return self.lib.drmSetMaster(self.fd) == 0

    def close(self) -> None:
        self._destroy_fb()
        if self.fd >= 0:
            try:
                self.lib.drmDropMaster(self.fd)
            except Exception:
                pass
            os.close(self.fd)
            self.fd = -1

    def _destroy_fb(self) -> None:
        if self._map is not None:
            try:
                self._map.close()
            except Exception:
                pass
            self._map = None
        if self._fb_id:
            try:
                self.lib.drmModeRmFB(self.fd, self._fb_id)
            except Exception:
                pass
            self._fb_id = 0
        if self._handle:
            try:
                d = ddm.drm_mode_destroy_dumb(handle=self._handle)
                fcntl.ioctl(self.fd, ddm.DRM_IOCTL_MODE_DESTROY_DUMB, d)
            except OSError:
                pass
            self._handle = 0

    def _pick_target(self):
        res_p = self.lib.drmModeGetResources(self.fd)
        if not res_p:
            raise OSError("drmModeGetResources failed")
        res = ctypes.cast(res_p, ctypes.POINTER(ddm.drmModeRes)).contents
        crtc_ids = [res.crtcs[i] for i in range(res.count_crtcs)]
        chosen = None
        for i in range(res.count_connectors):
            conn_id = res.connectors[i]
            conn_p = self.lib.drmModeGetConnector(self.fd, conn_id)
            if not conn_p:
                continue
            conn = ctypes.cast(conn_p, ctypes.POINTER(ddm.drmModeConnector)).contents
            name = f"conn-{conn_id}"
            if conn.connector_type == DRM_MODE_CONNECTOR_HDMIA:
                name = f"HDMI-A-{conn.connector_type_id}"
            connected = conn.connection == ddm.DRM_MODE_CONNECTED and conn.count_modes > 0
            want = (self.connector_name is None) or (name == self.connector_name)
            if connected and want and chosen is None:
                modes = [conn.modes[j] for j in range(conn.count_modes)]
                mode = ddm.pick_mode(modes, 1920, 1080)
                crtc_id = self._crtc_for(conn, crtc_ids)
                if crtc_id and mode is not None:
                    chosen = (conn_id, crtc_id, mode, name)
            self.lib.drmModeFreeConnector(conn_p)
        self.lib.drmModeFreeResources(res_p)
        return chosen

    def _crtc_for(self, conn, crtc_ids: list[int]) -> int:
        """Pick a free CRTC for this connector. Capture fields *before* FreeEncoder
        (use-after-free on enc.crtc_id produced garbage ids → SetCrtc -ENOENT)."""
        if conn.encoder_id:
            enc_p = self.lib.drmModeGetEncoder(self.fd, conn.encoder_id)
            if enc_p:
                enc = ctypes.cast(enc_p, ctypes.POINTER(ddm.drmModeEncoder)).contents
                crtc_id = int(enc.crtc_id)
                possible = int(enc.possible_crtcs)
                self.lib.drmModeFreeEncoder(enc_p)
                if crtc_id:
                    return crtc_id
                for bit, cid in enumerate(crtc_ids):
                    if possible & (1 << bit):
                        return int(cid)
        for ei in range(conn.count_encoders):
            e2 = conn.encoders[ei]
            enc_p = self.lib.drmModeGetEncoder(self.fd, e2)
            if not enc_p:
                continue
            enc = ctypes.cast(enc_p, ctypes.POINTER(ddm.drmModeEncoder)).contents
            possible = int(enc.possible_crtcs)
            self.lib.drmModeFreeEncoder(enc_p)
            for bit, cid in enumerate(crtc_ids):
                if possible & (1 << bit):
                    return int(cid)
        return int(crtc_ids[0]) if crtc_ids else 0

    def show(self) -> bool:
        """Modeset splash once. Returns True if the frame is up."""
        target = self._pick_target()
        if target is None:
            return False
        conn_id, crtc_id, mode, name = target
        w, h = int(mode.hdisplay), int(mode.vdisplay)
        creq = ddm.drm_mode_create_dumb(height=h, width=w, bpp=32, flags=0)
        fcntl.ioctl(self.fd, ddm.DRM_IOCTL_MODE_CREATE_DUMB, creq)
        self._handle = creq.handle
        pitch = int(creq.pitch)
        handles = (ctypes.c_uint32 * 4)(creq.handle, 0, 0, 0)
        pitches = (ctypes.c_uint32 * 4)(pitch, 0, 0, 0)
        offsets = (ctypes.c_uint32 * 4)(0, 0, 0, 0)
        fb_id = ctypes.c_uint32(0)
        r = self.lib.drmModeAddFB2(
            self.fd, w, h, DRM_FORMAT_XRGB8888,
            handles, pitches, offsets, ctypes.byref(fb_id), 0,
        )
        if r != 0:
            raise OSError(f"drmModeAddFB2 failed ({r})")
        self._fb_id = fb_id.value
        mreq = ddm.drm_mode_map_dumb(handle=creq.handle)
        fcntl.ioctl(self.fd, ddm.DRM_IOCTL_MODE_MAP_DUMB, mreq)
        self._map = mmap.mmap(
            self.fd, creq.size, flags=mmap.MAP_SHARED,
            prot=mmap.PROT_READ | mmap.PROT_WRITE, offset=mreq.offset,
        )
        paint_splash(self._map, pitch, w, h, self.text)
        # Modeset while still master; steal the CRTC from fbcon *before* unbinding
        # vtconsole (unbinding first left SetCrtc returning -ENOENT in field).
        conn_arr = (ctypes.c_uint32 * 1)(conn_id)
        # Ensure mode is a durable copy (pick_mode already copies, but be explicit).
        mode_copy = ddm.copy_mode(mode)
        r = self.lib.drmModeSetCrtc(
            self.fd, crtc_id, self._fb_id, 0, 0,
            conn_arr, 1, ctypes.byref(mode_copy),
        )
        if r != 0:
            # Retry once after DropMaster/SetMaster — another client may have raced.
            try:
                self.lib.drmDropMaster(self.fd)
            except Exception:
                pass
            time.sleep(0.05)
            self.lib.drmSetMaster(self.fd)
            r = self.lib.drmModeSetCrtc(
                self.fd, crtc_id, self._fb_id, 0, 0,
                conn_arr, 1, ctypes.byref(mode_copy),
            )
        if r != 0:
            raise OSError(f"drmModeSetCrtc failed ({r})")
        _unbind_vtconsole()
        log(f"showing on {name}: {w}x{h} text={self.text!r}")
        # Parent (HdmiPlayer) waits on stdout for this token before considering
        # the handoff complete — keep it exact, single line, no prefix.
        print("READY", flush=True)
        return True

    def hold(self) -> None:
        """Stay alive until SIGTERM so the FB keeps scanning out."""
        while not self._stop:
            time.sleep(0.25)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", default=os.environ.get("DRM_DEVICE", "/dev/dri/card1"))
    ap.add_argument(
        "--connector",
        default=os.environ.get("DRM_CONNECTOR", "HDMI-A-2"),
        help="HDMI-A-N or 'auto' for first connected HDMI-A-*",
    )
    ap.add_argument(
        "--text",
        default=os.environ.get("O3_IDLE_SPLASH_TEXT", "waiting for live FPV feed"),
    )
    ap.add_argument(
        "--retry-s",
        type=float,
        default=float(os.environ.get("O3_IDLE_SPLASH_RETRY_S", "2")),
        help="seconds between connector-not-ready retries",
    )
    ap.add_argument(
        "--master-wait-s",
        type=float,
        default=float(os.environ.get("O3_IDLE_SPLASH_MASTER_WAIT_S", "3")),
        help="how long to spin on drmSetMaster per attempt (handoff window)",
    )
    args = ap.parse_args()
    conn = args.connector.strip()
    if conn.lower() in ("", "auto", "any", "*"):
        conn_name: str | None = None
    else:
        conn_name = conn

    splash = IdleSplash(args.device, conn_name, args.text.strip() or "waiting for live FPV feed")
    signal.signal(signal.SIGTERM, splash.request_stop)
    signal.signal(signal.SIGINT, splash.request_stop)

    try:
        splash.open()
    except OSError as e:
        log(f"open {args.device} failed: {e}")
        return 1

    try:
        while not splash._stop:
            # Spin for master first so a parent that kills the video presenter
            # mid-window sees us paint within ~50ms of the player dropping DRM.
            if not splash.ensure_master(timeout=max(0.2, args.master_wait_s)):
                if splash._stop:
                    break
                log(f"still waiting for DRM master — retry in {min(0.2, args.retry_s)}s")
                t0 = time.monotonic()
                while not splash._stop and (time.monotonic() - t0) < 0.2:
                    time.sleep(0.05)
                continue
            try:
                if splash.show():
                    splash.hold()
                    break
                log(f"no usable connector ({conn_name or 'auto'}) — retry in {args.retry_s}s")
            except OSError as e:
                log(f"modeset failed: {e} — retry in {args.retry_s}s")
                splash._destroy_fb()
            # Wait, but wake promptly on stop. Short tick while connector missing.
            t0 = time.monotonic()
            while not splash._stop and (time.monotonic() - t0) < max(0.5, args.retry_s):
                time.sleep(0.2)
    finally:
        splash.close()
        log("exit (DRM master released)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
