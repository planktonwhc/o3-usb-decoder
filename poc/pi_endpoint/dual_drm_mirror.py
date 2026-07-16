#!/usr/bin/env python3
"""Dual-HDMI DRM mirror for Pi 4 vc4 — one buffer, both CRTCs, scale-to-fit.

Reads raw BGRx frames (default 1920×1080) from stdin and presents them on every
connected HDMI connector via a single DRM master open. Each CRTC gets the full
source frame scaled to fit (letterbox / pillarbox, aspect preserved).

Why this exists (2026-07-13 offline prove):
  - dual tee×kmssink → modeset / SetPlane Permission denied
  - fbdevsink shared FB → both paint, but A-1 often stuck at 1024×768 cropping
    the top-left of a 1920×1080 FB
  - this tool modesets each CRTC independently and uses plane scaling so a
    lower-mode port still shows the whole FPV image

Usage (H.264 ES on stdin → decode → mirror):
  gst-launch-1.0 -q fdsrc fd=0 ! h264parse ! openh264dec ! videoconvert !
      video/x-raw,format=BGRx,width=1920,height=1080 ! fdsink sync=false
    | dual_drm_mirror.py --width 1920 --height 1080

Or as a one-shot player (stdin = H.264 ES):
  dual_drm_mirror.py --from-es
"""

from __future__ import annotations

import argparse
import atexit
import ctypes
import ctypes.util
import fcntl
import os
import signal
import struct
import subprocess
import sys
import time
from typing import Any


# --- libdrm minimal bindings -------------------------------------------------

DRM_IOCTL_BASE = ord("d")


def _IOWR(nr: int, size: int) -> int:
    # Linux ioctl: dir(2) type(8) nr(8) size(14) — simplified for our structs
    return (3 << 30) | (size << 16) | (DRM_IOCTL_BASE << 8) | nr


class drmModeModeInfo(ctypes.Structure):
    _fields_ = [
        ("clock", ctypes.c_uint32),
        ("hdisplay", ctypes.c_uint16),
        ("hsync_start", ctypes.c_uint16),
        ("hsync_end", ctypes.c_uint16),
        ("htotal", ctypes.c_uint16),
        ("hskew", ctypes.c_uint16),
        ("vdisplay", ctypes.c_uint16),
        ("vsync_start", ctypes.c_uint16),
        ("vsync_end", ctypes.c_uint16),
        ("vtotal", ctypes.c_uint16),
        ("vscan", ctypes.c_uint16),
        ("vrefresh", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("name", ctypes.c_char * 32),
    ]


class drm_mode_create_dumb(ctypes.Structure):
    _fields_ = [
        ("height", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("bpp", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("handle", ctypes.c_uint32),
        ("pitch", ctypes.c_uint32),
        ("size", ctypes.c_uint64),
    ]


class drm_mode_map_dumb(ctypes.Structure):
    _fields_ = [
        ("handle", ctypes.c_uint32),
        ("pad", ctypes.c_uint32),
        ("offset", ctypes.c_uint64),
    ]


class drm_mode_destroy_dumb(ctypes.Structure):
    _fields_ = [
        ("handle", ctypes.c_uint32),
    ]


class drm_mode_fb_cmd2(ctypes.Structure):
    _fields_ = [
        ("fb_id", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("pixel_format", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("handles", ctypes.c_uint32 * 4),
        ("pitches", ctypes.c_uint32 * 4),
        ("offsets", ctypes.c_uint32 * 4),
        ("modifier", ctypes.c_uint64 * 4),
    ]


# fourcc
DRM_FORMAT_XRGB8888 = 0x34325258  # 'XR24'


class LibDrm:
    def __init__(self):
        path = ctypes.util.find_library("drm") or "libdrm.so.2"
        self.lib = ctypes.CDLL(path)
        L = self.lib

        # drmModeGetResources → pointer
        L.drmModeGetResources.restype = ctypes.c_void_p
        L.drmModeGetResources.argtypes = [ctypes.c_int]

        L.drmModeFreeResources.argtypes = [ctypes.c_void_p]

        L.drmModeGetConnector.restype = ctypes.c_void_p
        L.drmModeGetConnector.argtypes = [ctypes.c_int, ctypes.c_uint32]
        L.drmModeFreeConnector.argtypes = [ctypes.c_void_p]

        L.drmModeGetEncoder.restype = ctypes.c_void_p
        L.drmModeGetEncoder.argtypes = [ctypes.c_int, ctypes.c_uint32]
        L.drmModeFreeEncoder.argtypes = [ctypes.c_void_p]

        L.drmModeGetCrtc.restype = ctypes.c_void_p
        L.drmModeGetCrtc.argtypes = [ctypes.c_int, ctypes.c_uint32]
        L.drmModeFreeCrtc.argtypes = [ctypes.c_void_p]

        L.drmModeGetPlaneResources.restype = ctypes.c_void_p
        L.drmModeGetPlaneResources.argtypes = [ctypes.c_int]
        L.drmModeFreePlaneResources.argtypes = [ctypes.c_void_p]

        L.drmModeGetPlane.restype = ctypes.c_void_p
        L.drmModeGetPlane.argtypes = [ctypes.c_int, ctypes.c_uint32]
        L.drmModeFreePlane.argtypes = [ctypes.c_void_p]

        L.drmModeSetCrtc.restype = ctypes.c_int
        L.drmModeSetCrtc.argtypes = [
            ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int,
            ctypes.POINTER(drmModeModeInfo),
        ]

        L.drmModeSetPlane.restype = ctypes.c_int
        L.drmModeSetPlane.argtypes = [
            ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_int, ctypes.c_int, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32,
        ]

        L.drmSetClientCap.restype = ctypes.c_int
        L.drmSetClientCap.argtypes = [ctypes.c_int, ctypes.c_uint64, ctypes.c_uint64]

        L.drmModeAddFB2.restype = ctypes.c_int
        L.drmModeAddFB2.argtypes = [
            ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_uint32,
        ]

        L.drmModeRmFB.restype = ctypes.c_int
        L.drmModeRmFB.argtypes = [ctypes.c_int, ctypes.c_uint32]

        # opaque struct field access via manual offsets — use helper readers
        self._setup_struct_helpers()

    def _setup_struct_helpers(self):
        # drmModeRes layout (libdrm):
        # int count_fbs; uint32_t *fbs;
        # int count_crtcs; uint32_t *crtcs;
        # int count_connectors; uint32_t *connectors;
        # int count_encoders; uint32_t *encoders;
        # ...
        pass


# C struct layouts matching libdrm headers (LP64)
class drmModeRes(ctypes.Structure):
    _fields_ = [
        ("count_fbs", ctypes.c_int),
        ("fbs", ctypes.POINTER(ctypes.c_uint32)),
        ("count_crtcs", ctypes.c_int),
        ("crtcs", ctypes.POINTER(ctypes.c_uint32)),
        ("count_connectors", ctypes.c_int),
        ("connectors", ctypes.POINTER(ctypes.c_uint32)),
        ("count_encoders", ctypes.c_int),
        ("encoders", ctypes.POINTER(ctypes.c_uint32)),
        ("min_width", ctypes.c_uint32),
        ("max_width", ctypes.c_uint32),
        ("min_height", ctypes.c_uint32),
        ("max_height", ctypes.c_uint32),
    ]


class drmModeConnector(ctypes.Structure):
    _fields_ = [
        ("connector_id", ctypes.c_uint32),
        ("encoder_id", ctypes.c_uint32),
        ("connector_type", ctypes.c_uint32),
        ("connector_type_id", ctypes.c_uint32),
        ("connection", ctypes.c_uint32),
        ("mmWidth", ctypes.c_uint32),
        ("mmHeight", ctypes.c_uint32),
        ("subpixel", ctypes.c_uint32),
        ("count_modes", ctypes.c_int),
        ("modes", ctypes.POINTER(drmModeModeInfo)),
        ("count_props", ctypes.c_int),
        ("props", ctypes.POINTER(ctypes.c_uint32)),
        ("prop_values", ctypes.POINTER(ctypes.c_uint64)),
        ("count_encoders", ctypes.c_int),
        ("encoders", ctypes.POINTER(ctypes.c_uint32)),
    ]


class drmModeEncoder(ctypes.Structure):
    _fields_ = [
        ("encoder_id", ctypes.c_uint32),
        ("encoder_type", ctypes.c_uint32),
        ("crtc_id", ctypes.c_uint32),
        ("possible_crtcs", ctypes.c_uint32),
        ("possible_clones", ctypes.c_uint32),
    ]


class drmModeCrtc(ctypes.Structure):
    _fields_ = [
        ("crtc_id", ctypes.c_uint32),
        ("buffer_id", ctypes.c_uint32),
        ("x", ctypes.c_uint32),
        ("y", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("mode_valid", ctypes.c_int),
        ("mode", drmModeModeInfo),
        ("gamma_size", ctypes.c_int),
    ]


class drmModePlaneRes(ctypes.Structure):
    _fields_ = [
        ("count_planes", ctypes.c_uint32),
        ("planes", ctypes.POINTER(ctypes.c_uint32)),
    ]


class drmModePlane(ctypes.Structure):
    _fields_ = [
        ("count_formats", ctypes.c_uint32),
        ("formats", ctypes.POINTER(ctypes.c_uint32)),
        ("plane_id", ctypes.c_uint32),
        ("crtc_id", ctypes.c_uint32),
        ("fb_id", ctypes.c_uint32),
        ("crtc_x", ctypes.c_uint32),
        ("crtc_y", ctypes.c_uint32),
        ("x", ctypes.c_uint32),
        ("y", ctypes.c_uint32),
        ("possible_crtcs", ctypes.c_uint32),
        ("gamma_size", ctypes.c_uint32),
    ]


DRM_MODE_CONNECTED = 1
DRM_CLIENT_CAP_UNIVERSAL_PLANES = 2
DRM_IOCTL_MODE_CREATE_DUMB = _IOWR(0xB2, ctypes.sizeof(drm_mode_create_dumb))
DRM_IOCTL_MODE_MAP_DUMB = _IOWR(0xB3, ctypes.sizeof(drm_mode_map_dumb))
DRM_IOCTL_MODE_DESTROY_DUMB = _IOWR(0xB4, ctypes.sizeof(drm_mode_destroy_dumb))


def log(msg: str) -> None:
    print(f"[dual-drm] {msg}", file=sys.stderr, flush=True)


def scale_to_fit(src_w: int, src_h: int, dst_w: int, dst_h: int) -> tuple[int, int, int, int]:
    """Return (x, y, w, h) letterboxed inside dst, preserving aspect."""
    if src_w <= 0 or src_h <= 0 or dst_w <= 0 or dst_h <= 0:
        return 0, 0, dst_w, dst_h
    scale = min(dst_w / src_w, dst_h / src_h)
    w = max(1, int(src_w * scale))
    h = max(1, int(src_h * scale))
    x = (dst_w - w) // 2
    y = (dst_h - h) // 2
    return x, y, w, h


def copy_mode(src: drmModeModeInfo) -> drmModeModeInfo:
    """Deep-copy mode info so it survives FreeConnector."""
    m = drmModeModeInfo()
    for f, _t in drmModeModeInfo._fields_:
        setattr(m, f, getattr(src, f))
    return m


def pick_mode(
    modes: list[drmModeModeInfo],
    prefer_w: int = 1920,
    prefer_h: int = 1080,
    active: drmModeModeInfo | None = None,
    policy: str = "scale",
) -> drmModeModeInfo | None:
    """Choose CRTC mode from the connector's EDID mode list only.

    Field bug: A-1 often reports active 1024×768 from fbcon clone, but that
    mode is **not** in HDMI-A-1's EDID list (only 1080p/720p/…). Modesetting
    the bogus active mode left A-1 stuck looking cropped forever. Never use a
    mode that is not in `modes`.
    """
    if not modes:
        return None

    def in_list(w: int, h: int) -> drmModeModeInfo | None:
        hits = [m for m in modes if m.hdisplay == w and m.vdisplay == h]
        if not hits:
            return None
        hits.sort(key=lambda m: abs(int(m.vrefresh) - 60))
        return copy_mode(hits[0])

    # Always prefer 1080p when the EDID lists it (both Pi ports do for our gear).
    m1080 = in_list(prefer_w, prefer_h)
    if m1080 is not None:
        return m1080

    # Active mode only if it is a real EDID mode for this connector.
    if active is not None and active.hdisplay and active.vdisplay:
        m_act = in_list(int(active.hdisplay), int(active.vdisplay))
        if m_act is not None:
            return m_act

    # DRM preferred = first listed mode
    return copy_mode(modes[0])


def letterbox_scale_bgrx(
    src: bytes, sw: int, sh: int,
    dw: int, dh: int, dst_pitch: int,
) -> bytearray:
    """Software scale-to-fit with letterbox bars — full image visible.

    Per-CRTC FB (not HW plane scale): field tests showed A-1 still cropped with
    drmModeSetPlane src→dst scaling even when kmsprint looked correct.
    Uses Pillow (present on the Pi image) for decent speed/quality.
    """
    out = bytearray(dst_pitch * dh)  # black
    if sw <= 0 or sh <= 0 or dw <= 0 or dh <= 0:
        return out
    dx, dy, fw, fh = scale_to_fit(sw, sh, dw, dh)
    try:
        from PIL import Image
        # Treat BGRx as BGRA (X ignored); resize keeps channel order for DRM XRGB/BGRx.
        img = Image.frombytes("RGBA", (sw, sh), src, "raw", "BGRA")
        scaled_b = img.resize((fw, fh), Image.BILINEAR).tobytes("raw", "BGRA")
    except Exception:
        # nearest-neighbor fallback (no PIL)
        src_mv = memoryview(src)
        src_row = sw * 4
        xmap = [min((x * sw) // fw, sw - 1) for x in range(fw)]
        line = bytearray(fw * 4)
        for y in range(fh):
            sy = min((y * sh) // fh, sh - 1)
            src_row_off = sy * src_row
            for i, sx in enumerate(xmap):
                si = src_row_off + sx * 4
                line[i * 4:(i + 1) * 4] = src_mv[si:si + 4]
            dst_row_off = (dy + y) * dst_pitch + dx * 4
            out[dst_row_off:dst_row_off + fw * 4] = line
        return out

    # blit letterboxed scaled row into out (BGRX)
    row = fw * 4
    for y in range(fh):
        dst_row_off = (dy + y) * dst_pitch + dx * 4
        out[dst_row_off:dst_row_off + row] = scaled_b[y * row:(y + 1) * row]
    return out


class DualMirror:
    def __init__(self, device: str, src_w: int, src_h: int, mode_policy: str = "scale"):
        self.device = device
        self.src_w = src_w
        self.src_h = src_h
        self.mode_policy = mode_policy  # "scale" | "prefer1080"
        self.fd = -1
        self.lib = LibDrm().lib
        self.outputs: list[dict[str, Any]] = []
        self._saved_crtcs: list[tuple] = []

    def open(self) -> None:
        self.fd = os.open(self.device, os.O_RDWR | os.O_CLOEXEC)
        # universal planes so we can find primary planes
        self.lib.drmSetClientCap(self.fd, DRM_CLIENT_CAP_UNIVERSAL_PLANES, 1)

    def close(self) -> None:
        self._restore()
        for o in self.outputs:
            self._destroy_output_fb(o)
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def _destroy_output_fb(self, o: dict[str, Any]) -> None:
        mp = o.get("map")
        if mp is not None:
            try:
                mp.close()
            except Exception:
                pass
            o["map"] = None
        if o.get("fb_id"):
            try:
                self.lib.drmModeRmFB(self.fd, o["fb_id"])
            except Exception:
                pass
            o["fb_id"] = 0
        if o.get("handle"):
            d = drm_mode_destroy_dumb(handle=o["handle"])
            try:
                fcntl.ioctl(self.fd, DRM_IOCTL_MODE_DESTROY_DUMB, d)
            except OSError:
                pass
            o["handle"] = 0

    def _create_output_fb(self, o: dict[str, Any]) -> None:
        """One dumb FB sized to the CRTC mode (1:1 scanout, no HW plane scale)."""
        w = int(o["mode"].hdisplay)
        h = int(o["mode"].vdisplay)
        creq = drm_mode_create_dumb(height=h, width=w, bpp=32, flags=0)
        fcntl.ioctl(self.fd, DRM_IOCTL_MODE_CREATE_DUMB, creq)
        o["handle"] = creq.handle
        o["pitch"] = creq.pitch
        o["buf_size"] = creq.size
        o["fb_w"] = w
        o["fb_h"] = h

        handles = (ctypes.c_uint32 * 4)(creq.handle, 0, 0, 0)
        pitches = (ctypes.c_uint32 * 4)(creq.pitch, 0, 0, 0)
        offsets = (ctypes.c_uint32 * 4)(0, 0, 0, 0)
        fb_id = ctypes.c_uint32(0)
        ret = self.lib.drmModeAddFB2(
            self.fd, w, h, DRM_FORMAT_XRGB8888,
            handles, pitches, offsets, ctypes.byref(fb_id), 0,
        )
        if ret != 0:
            raise OSError(ret, f"drmModeAddFB2 failed ({ret}) for {o['name']}")
        o["fb_id"] = fb_id.value

        mreq = drm_mode_map_dumb(handle=creq.handle)
        fcntl.ioctl(self.fd, DRM_IOCTL_MODE_MAP_DUMB, mreq)
        import mmap
        o["map"] = mmap.mmap(
            self.fd, creq.size, flags=mmap.MAP_SHARED,
            prot=mmap.PROT_READ | mmap.PROT_WRITE, offset=mreq.offset,
        )
        o["map"][:] = b"\x00" * creq.size
        # same-size outputs can direct-copy; others software letterbox
        o["needs_scale"] = not (w == self.src_w and h == self.src_h)

    def _find_plane_for_crtc(self, crtc_index: int, crtc_id: int) -> int | None:
        pres_p = self.lib.drmModeGetPlaneResources(self.fd)
        if not pres_p:
            return None
        pres = ctypes.cast(pres_p, ctypes.POINTER(drmModePlaneRes)).contents
        found = None
        for i in range(pres.count_planes):
            pid = pres.planes[i]
            pl_p = self.lib.drmModeGetPlane(self.fd, pid)
            if not pl_p:
                continue
            pl = ctypes.cast(pl_p, ctypes.POINTER(drmModePlane)).contents
            if pl.possible_crtcs & (1 << crtc_index):
                # Prefer a plane already bound to this CRTC, else first possible
                if pl.crtc_id == crtc_id or found is None:
                    found = pid
                    if pl.crtc_id == crtc_id:
                        self.lib.drmModeFreePlane(pl_p)
                        break
            self.lib.drmModeFreePlane(pl_p)
        self.lib.drmModeFreePlaneResources(pres_p)
        return found

    def setup_outputs(self) -> None:
        res_p = self.lib.drmModeGetResources(self.fd)
        if not res_p:
            raise OSError("drmModeGetResources failed")
        res = ctypes.cast(res_p, ctypes.POINTER(drmModeRes)).contents

        # map crtc_id → index for possible_crtcs bitmasks
        crtc_ids = [res.crtcs[i] for i in range(res.count_crtcs)]
        crtc_index = {cid: i for i, cid in enumerate(crtc_ids)}

        used_crtcs: set[int] = set()
        for i in range(res.count_connectors):
            conn_id = res.connectors[i]
            conn_p = self.lib.drmModeGetConnector(self.fd, conn_id)
            if not conn_p:
                continue
            conn = ctypes.cast(conn_p, ctypes.POINTER(drmModeConnector)).contents
            if conn.connection != DRM_MODE_CONNECTED or conn.count_modes == 0:
                self.lib.drmModeFreeConnector(conn_p)
                continue

            modes = [conn.modes[j] for j in range(conn.count_modes)]

            # pick encoder/crtc
            enc_id = conn.encoder_id
            crtc_id = 0
            if enc_id:
                enc_p = self.lib.drmModeGetEncoder(self.fd, enc_id)
                if enc_p:
                    enc = ctypes.cast(enc_p, ctypes.POINTER(drmModeEncoder)).contents
                    if enc.crtc_id and enc.crtc_id not in used_crtcs:
                        crtc_id = enc.crtc_id
                    possible = enc.possible_crtcs
                    self.lib.drmModeFreeEncoder(enc_p)
                    if not crtc_id:
                        for bit, cid in enumerate(crtc_ids):
                            if (possible & (1 << bit)) and cid not in used_crtcs:
                                crtc_id = cid
                                break
            if not crtc_id:
                # try all encoders on connector
                for ei in range(conn.count_encoders):
                    e2 = conn.encoders[ei]
                    enc_p = self.lib.drmModeGetEncoder(self.fd, e2)
                    if not enc_p:
                        continue
                    enc = ctypes.cast(enc_p, ctypes.POINTER(drmModeEncoder)).contents
                    for bit, cid in enumerate(crtc_ids):
                        if (enc.possible_crtcs & (1 << bit)) and cid not in used_crtcs:
                            crtc_id = cid
                            break
                    self.lib.drmModeFreeEncoder(enc_p)
                    if crtc_id:
                        break

            if not crtc_id:
                log(f"connector {conn_id}: no free CRTC — skip")
                self.lib.drmModeFreeConnector(conn_p)
                continue

            # Active mode before we touch anything (A-1 field: often 1024×768).
            active = self._active_mode(crtc_id)
            mode = pick_mode(modes, active=active, policy=self.mode_policy)
            if mode is None:
                self.lib.drmModeFreeConnector(conn_p)
                continue

            # save old crtc for restore
            old_p = self.lib.drmModeGetCrtc(self.fd, crtc_id)
            if old_p:
                self._saved_crtcs.append((crtc_id, old_p))  # free later on restore

            cidx = crtc_index[crtc_id]
            plane_id = self._find_plane_for_crtc(cidx, crtc_id)
            if plane_id is None:
                log(f"connector {conn_id}: no plane for crtc {crtc_id} — skip")
                self.lib.drmModeFreeConnector(conn_p)
                continue

            used_crtcs.add(crtc_id)
            ctype = conn.connector_type
            ctype_id = conn.connector_type_id
            name = f"conn-{conn_id}"
            # HDMI-A types: DRM_MODE_CONNECTOR_HDMIA = 11
            if ctype == 11:
                name = f"HDMI-A-{ctype_id}"

            dx, dy, dw, dh = scale_to_fit(
                self.src_w, self.src_h, mode.hdisplay, mode.vdisplay
            )
            self.outputs.append({
                "name": name,
                "connector_id": conn_id,
                "crtc_id": crtc_id,
                "plane_id": plane_id,
                "mode": mode,
                "dst": (dx, dy, dw, dh),
            })
            log(
                f"{name}: mode={mode.hdisplay}x{mode.vdisplay}@{mode.vrefresh} "
                f"crtc={crtc_id} plane={plane_id} fit={dw}x{dh}+{dx}+{dy}"
            )
            # keep connector pointer? free — we copied mode by value
            self.lib.drmModeFreeConnector(conn_p)

        self.lib.drmModeFreeResources(res_p)
        if not self.outputs:
            raise RuntimeError("no connected HDMI outputs found")

    def _crtc_display_size(self, crtc_id: int, fallback: drmModeModeInfo) -> tuple[int, int]:
        """Actual active CRTC mode size (after modeset), else preferred mode size."""
        old_p = self.lib.drmModeGetCrtc(self.fd, crtc_id)
        if old_p:
            crtc = ctypes.cast(old_p, ctypes.POINTER(drmModeCrtc)).contents
            if crtc.mode_valid and crtc.mode.hdisplay and crtc.mode.vdisplay:
                w, h = int(crtc.mode.hdisplay), int(crtc.mode.vdisplay)
                self.lib.drmModeFreeCrtc(old_p)
                return w, h
            if crtc.width and crtc.height:
                w, h = int(crtc.width), int(crtc.height)
                self.lib.drmModeFreeCrtc(old_p)
                return w, h
            self.lib.drmModeFreeCrtc(old_p)
        return int(fallback.hdisplay), int(fallback.vdisplay)

    def _active_mode(self, crtc_id: int) -> drmModeModeInfo | None:
        old_p = self.lib.drmModeGetCrtc(self.fd, crtc_id)
        if not old_p:
            return None
        crtc = ctypes.cast(old_p, ctypes.POINTER(drmModeCrtc)).contents
        m = None
        if crtc.mode_valid and crtc.mode.hdisplay:
            m = copy_mode(crtc.mode)
        self.lib.drmModeFreeCrtc(old_p)
        return m

    def modeset(self) -> None:
        """Per-output FB at CRTC resolution + SetCrtc 1:1 (software scale on present)."""
        for o in self.outputs:
            self._create_output_fb(o)
            conn_arr = (ctypes.c_uint32 * 1)(o["connector_id"])
            mode = o["mode"]
            ret = self.lib.drmModeSetCrtc(
                self.fd, o["crtc_id"], o["fb_id"], 0, 0,
                conn_arr, 1, ctypes.byref(mode),
            )
            if ret != 0:
                errno = -ret if ret < 0 else ret
                try:
                    errname = os.strerror(errno)
                except Exception:
                    errname = str(errno)
                log(f"{o['name']}: SetCrtc {mode.hdisplay}x{mode.vdisplay} "
                    f"failed ({errname})")
                # try active mode with matching FB rebuild
                active = self._active_mode(o["crtc_id"])
                if active is not None:
                    self._destroy_output_fb(o)
                    o["mode"] = active
                    self._create_output_fb(o)
                    ret = self.lib.drmModeSetCrtc(
                        self.fd, o["crtc_id"], o["fb_id"], 0, 0,
                        conn_arr, 1, ctypes.byref(active),
                    )
            if ret != 0:
                log(f"{o['name']}: SetCrtc still failed ret={ret}")
            else:
                how = "sw-scale-to-fit" if o.get("needs_scale") else "1:1"
                log(f"{o['name']}: SetCrtc {o['fb_w']}x{o['fb_h']} OK ({how})")

    def present_frame(self, bgrx: bytes) -> None:
        """Blit one BGRx source frame into every output FB (scale-to-fit if needed)."""
        need = self.src_w * self.src_h * 4
        if len(bgrx) < need:
            return
        src = bgrx[:need]
        for o in self.outputs:
            mp = o.get("map")
            if mp is None:
                continue
            pitch = o["pitch"]
            w, h = o["fb_w"], o["fb_h"]
            if not o.get("needs_scale") and pitch == w * 4:
                mp[:need] = src
                continue
            if not o.get("needs_scale"):
                # same size, padded pitch
                row = w * 4
                mv = memoryview(src)
                for y in range(h):
                    mp[y * pitch:y * pitch + row] = mv[y * row:(y + 1) * row]
                continue
            # Software letterbox scale into this CRTC's native FB
            scaled = letterbox_scale_bgrx(
                src, self.src_w, self.src_h, w, h, pitch,
            )
            mp[: len(scaled)] = scaled

    def _restore(self) -> None:
        # Free saved crtc structs; skip full restore (fbcon reclaims on process exit)
        for crtc_id, old_p in self._saved_crtcs:
            try:
                self.lib.drmModeFreeCrtc(old_p)
            except Exception:
                pass
        self._saved_crtcs.clear()


def run_from_bgrx_stdin(mirror: DualMirror) -> int:
    frame_bytes = mirror.src_w * mirror.src_h * 4
    log(f"reading BGRx {mirror.src_w}x{mirror.src_h} ({frame_bytes} bytes/frame) from stdin")
    # Use buffered read
    stdin = sys.stdin.buffer
    n = 0
    t0 = time.monotonic()
    while True:
        buf = bytearray()
        while len(buf) < frame_bytes:
            chunk = stdin.read(frame_bytes - len(buf))
            if not chunk:
                log(f"EOF after {n} frames ({time.monotonic() - t0:.1f}s)")
                return 0
            buf.extend(chunk)
        mirror.present_frame(bytes(buf))
        n += 1
        if n == 1 or n % 120 == 0:
            log(f"frames={n}")


def patch_h264_level(es: bytes, level_idc: int = 42) -> tuple[bytes, bool]:
    """Rewrite SPS level_idc in Annex-B ES.

    Returns (data, saw_sps). data is es unchanged if no SPS or already at target level.

    O3 live/DVR tags High@L5.2 but 1080p60 only needs L4.2. Pi v4l2h264dec
    (bcm2835) only advertises levels ≤5.1, so raw L5.2 fails caps negotiation and
    dual falls back to openh264 — too slow → field slow-mo.

    SPS after start code: NAL hdr (0x67) | profile_idc | constraints | level_idc
    Fast path: find() on common SPS start-code patterns (not byte-scan).
    """
    if not es or len(es) < 8:
        return es, False
    # Common DJI SPS NAL header is 0x67 (ref_idc=3, type=7). Also ref_idc=1 (0x27).
    patterns = (b"\x00\x00\x00\x01\x67", b"\x00\x00\x01\x67",
                b"\x00\x00\x00\x01\x27", b"\x00\x00\x01\x27")
    out: bytearray | None = None
    saw_sps = False
    for pat in patterns:
        start = 0
        sc = 4 if pat.startswith(b"\x00\x00\x00\x01") else 3
        base = es if out is None else out
        while True:
            j = base.find(pat, start)
            if j < 0:
                break
            saw_sps = True
            nal = j + sc
            if nal + 3 < len(base):
                lvl_pos = nal + 3
                if base[lvl_pos] != level_idc:
                    if out is None:
                        out = bytearray(es)
                        base = out
                    out[lvl_pos] = level_idc
            start = nal + 4
    if out is not None:
        return bytes(out), True
    return es, saw_sps


def _pick_decoder(preferred: str) -> str:
    """Resolve decoder element name. 'auto' → v4l2h264dec if present else openh264dec."""
    pref = (preferred or "auto").strip().lower()
    if pref not in ("", "auto"):
        return preferred
    # Prefer HW: openh264 cannot sustain 1080p60 on Pi 4 (field slow-mo).
    try:
        r = subprocess.run(
            ["gst-inspect-1.0", "v4l2h264dec"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if r.returncode == 0:
            return "v4l2h264dec"
    except FileNotFoundError:
        pass
    return "openh264dec"


def run_from_es_stdin(mirror: DualMirror, decoder: str, es_path: str | None = None) -> int:
    """H.264 ES → gst decode → BGRx on gst stdout → present.

    ES source: --es-file PATH, or stdin (live HdmiPlayer / shell redirect).
    Parent pumps ES into gst's stdin pipe so fdsrc never races Python's stdin.

    Live pacing:
      - Patch SPS level 5.2 → 4.2 so v4l2h264dec accepts the stream.
      - Prefer v4l2h264dec (HW); openh264 is fallback only.
      - Drop intermediate frames if present falls behind (never slow-mo catch-up).
    """
    import threading

    decoder = _pick_decoder(decoder)
    # Always patch L5.2→L4.2 for v4l2; for openh264 leave stream alone unless forced.
    env_patch = os.environ.get("O3_H264_LEVEL_PATCH", "").strip().lower()
    if env_patch in ("0", "false", "no"):
        do_level_patch = False
    elif env_patch in ("1", "true", "yes", "on"):
        do_level_patch = True
    else:
        do_level_patch = decoder.startswith("v4l2")

    # Always pump via fdsrc so we can level-patch (and for live stdin).
    # Offline --es-file without patch could use filesrc, but one path is simpler.
    log(f"ES source: {es_path if es_path else 'stdin'}  decoder={decoder}  "
        f"level_patch={do_level_patch}")

    # NOTE: do NOT put a small leaky queue before raw BGRx — one 1080p frame is
    # ~8 MiB. Live ES pacing is drop-oldest in HdmiPlayer + drop-to-latest below.
    gst_cmd = [
        "gst-launch-1.0", "-q",
        "fdsrc", "fd=0",
        "!", "h264parse", "config-interval=-1",
        "!", decoder,
        "!", "videoconvert", "n-threads=2",
        "!", "videoscale", "method=0",
        "!", f"video/x-raw,format=BGRx,width={mirror.src_w},height={mirror.src_h}",
        "!", "fdsink", "fd=1", "sync=false",
    ]
    log("pipeline: " + " ".join(gst_cmd))

    gst = subprocess.Popen(
        gst_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    def _pump_es():
        assert gst.stdin is not None
        src = open(es_path, "rb") if es_path else sys.stdin.buffer
        close_src = bool(es_path)
        # Patch only until we've seen an SPS (level is constant for the session).
        still_patch = do_level_patch
        try:
            while True:
                chunk = src.read(65536)
                if not chunk:
                    break
                if still_patch:
                    chunk, saw = patch_h264_level(chunk)
                    if saw:
                        still_patch = False
                try:
                    gst.stdin.write(chunk)
                except BrokenPipeError:
                    break
        finally:
            if close_src:
                try:
                    src.close()
                except Exception:
                    pass
            try:
                gst.stdin.close()
            except Exception:
                pass

    threading.Thread(target=_pump_es, name="es-pump", daemon=True).start()

    assert gst.stdout is not None
    # Non-blocking stdout so we can drain-to-latest when present is slow.
    try:
        os.set_blocking(gst.stdout.fileno(), False)
    except Exception:
        pass

    frame_bytes = mirror.src_w * mirror.src_h * 4
    n = 0
    dropped = 0
    t0 = time.monotonic()
    partial = bytearray()
    try:
        while True:
            # Read at least one full frame (block via poll if needed).
            while len(partial) < frame_bytes:
                try:
                    chunk = gst.stdout.read(min(65536, frame_bytes - len(partial)))
                except BlockingIOError:
                    chunk = None
                if chunk:
                    partial.extend(chunk)
                    continue
                if chunk == b"":
                    # EOF
                    err = b""
                    if gst.stderr:
                        try:
                            # stderr may also be full; best-effort
                            os.set_blocking(gst.stderr.fileno(), False)
                            err = gst.stderr.read() or b""
                        except Exception:
                            pass
                    dt = time.monotonic() - t0
                    fps = (n / dt) if dt > 0 else 0.0
                    log(f"decode EOF after {n} frames ({dt:.1f}s, {fps:.1f} fps, "
                        f"dropped={dropped}) gst_rc={gst.poll()} "
                        f"err={err.decode('utf-8', 'replace')[-600:]}")
                    return 0 if n else 1
                # No data yet — brief wait
                import select
                r, _, _ = select.select([gst.stdout], [], [], 0.5)
                if not r and gst.poll() is not None:
                    # process died with empty pipe
                    break
            if len(partial) < frame_bytes:
                break

            # First full frame
            frame = bytes(partial[:frame_bytes])
            del partial[:frame_bytes]

            # Drain any additional complete frames already in the pipe — keep only
            # the newest (stay live; never play catch-up slow-mo).
            while True:
                # Fill partial from non-blocking reads
                while len(partial) < frame_bytes:
                    try:
                        chunk = gst.stdout.read(min(65536, frame_bytes - len(partial)))
                    except BlockingIOError:
                        chunk = None
                    if chunk:
                        partial.extend(chunk)
                        continue
                    break
                if len(partial) < frame_bytes:
                    break
                frame = bytes(partial[:frame_bytes])
                del partial[:frame_bytes]
                dropped += 1

            mirror.present_frame(frame)
            n += 1
            if n == 1 or n % 120 == 0:
                dt = time.monotonic() - t0
                fps = (n / dt) if dt > 0 else 0.0
                log(f"frames={n} drop={dropped} ~{fps:.1f} fps")
    finally:
        if gst.poll() is None:
            gst.terminate()
            try:
                gst.wait(timeout=2)
            except subprocess.TimeoutExpired:
                gst.kill()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default="/dev/dri/card1")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--from-es", action="store_true",
                    help="stdin is H.264 ES (spawn gst decode); default: stdin is raw BGRx")
    ap.add_argument("--es-file", default=None,
                    help="read H.264 ES from PATH instead of stdin (offline prove)")
    ap.add_argument(
        "--decoder",
        default=os.environ.get("O3_GST_DECODER", "auto"),
        help="gst decoder element, or 'auto' (v4l2h264dec if available else openh264dec)",
    )
    ap.add_argument(
        "--mode-policy",
        choices=("scale", "prefer1080"),
        default=os.environ.get("O3_DUAL_MODE_POLICY", "scale"),
        help="scale=keep native CRTC mode + plane letterbox (default); "
             "prefer1080=force 1920x1080 when EDID lists it",
    )
    args = ap.parse_args()

    mirror = DualMirror(args.device, args.width, args.height, mode_policy=args.mode_policy)
    atexit.register(mirror.close)

    def _sig(_s, _f):
        sys.exit(0)
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    try:
        mirror.open()
        mirror.setup_outputs()
        mirror.modeset()
    except Exception as e:
        log(f"setup failed: {e}")
        return 1

    if args.from_es or args.es_file:
        return run_from_es_stdin(mirror, args.decoder, es_path=args.es_file)
    return run_from_bgrx_stdin(mirror)


if __name__ == "__main__":
    sys.exit(main() or 0)
