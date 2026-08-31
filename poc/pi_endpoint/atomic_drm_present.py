#!/usr/bin/env python3
"""Present-per-frame KMS presenter for Pi 4 vc4 — single or dual HDMI, SW decode.

Why this exists (2026-07-14 gst atomic pivot — see
docs/SESSION_CLOSE_2026-07-14_gst_atomic_pivot.md):
  - mpv presents on a *guessed* container-fps clock → 50-vs-60 mismatch ate 2/3 of
    frames (the ~14 fps "chop"). Raw USB is clean ~43-50 fps; the reader is clean.
  - `v4l2h264dec ! kmssink` is smooth BUT kmssink re-modesets **every spawn** with
    force-modesetting; under the systemd service that crash-looped and the modeset
    churn dropped the Atomos Shinobi's HDMI sync (needed a monitor power-cycle).
  - mpv coexists fine because it opens the DRM device explicitly and **modesets
    exactly once**, then holds it.

This presenter keeps mpv's DRM discipline but presents per frame:
  DECODE  (GStreamer appsink, in-process, SW): fdsrc ! queue(leaky) ! h264parse !
          avdec_h264 ! video/x-raw,format=I420 ! appsink.
  PRESENT (custom DRM, ctypes reused from dual_drm_mirror): keep a small pool of
          DRM **YU12 (YUV420) dumb buffers** — the vc4 display plane scans YU12
          out natively (verified: card1 lists YU12/NV12 plane formats) — memcpy the
          decoded frame into a free buffer and **page-flip** it onto each connector's
          CRTC. Modeset ONCE per CRTC at start (SetCrtc), then only page-flips.
          Dual HDMI (Stage 2): same FB flipped onto both CRTCs when modes match.

A page-flip is NOT a modeset — it swaps the scanout address at vblank — so it can
never drop the monitor's HDMI sync the way kmssink's re-modeset did.

**HW decode (v4l2h264dec, /dev/video10, bcm2835-codec) was dropped 2026-07-15.**
Field-test found a solid green OSD bar + cyan/blue ghosting on high-contrast content
(a TV/monitor in frame). Root-caused by a byte-level A/B: fed the *identical* tee'd
ES to (a) the HW decoder and (b) an independent software decoder (ffmpeg, then
GStreamer's avdec_h264) and diffed chroma planes. The HW decoder reconstructs chroma
wrong at sharp/high-contrast edges (e.g. V clipped to 0 at a static OSD region, on the
very first keyframe, regardless of declared SPS level) — a genuine bcm2835-codec
firmware bug, not a colorspace/DRM/presentation issue (ruled that out first: the vc4
plane's COLOR_ENCODING/COLOR_RANGE already defaulted correctly). avdec_h264 decodes
the exact same bytes with correct chroma throughout, confirmed pixel-for-pixel, AND
measured 120-125 fps on this Pi 4 (vs the ~50-60 fps needed) — more than 2x headroom,
so this is not a speed tradeoff. Zero-copy dmabuf was never actually achieved with
v4l2h264dec either (see below) — so switching decoders costs nothing we still had.

**Retested 2026-08-22** (layout-aware pixel dump, five bench files, firmware
check negative): the chroma bug is real, content-general (flat near-black luma),
and `hw-nv12` does not reliably fix it. **HW decode was removed from this file
on 2026-08-30** — `avdec_h264` is the only path. `O3_HDMI_DECODE` is ignored.
Evidence: `docs/SESSION_CLOSE_2026-08-22_hw_nv12_built_falsified.md`.

Zero-copy dmabuf was the original idea, but on this Pi appsink does not drive the
allocation query that makes a V4L2 M2M decoder export dmabuf fds (it hands back
allocatorsysmem0). Rather than depend on that, we decode and do one ~3 MB/frame
memcpy into a YU12 dumb buffer — negligible on a Pi 4, and the whole frame is
verifiable offline (a decoded I420 frame renders correctly to PNG). The chop fix is
"present-per-frame + modeset once", not zero-copy specifically.

The goggles' own cadence paces the display (capped at the panel's refresh via
tear-free page-flips): no fps guess, no framedrop on a wrong clock. That kills the
chop.

avdec_h264 has no level ceiling (unlike v4l2h264dec's L5.1 limit that forced the
endpoint's SPS L5.2->L5.1 patch) — it decodes the native O3 L5.2 stream directly.
The endpoint's O3_H264_LEVEL_PATCH is harmless left on (proven not to affect chroma
correctness either way) but is no longer required by this presenter.

Modes:
  --probe        Decode + report frame size / derived plane layout / fps. NO DRM,
                 NO modeset, NO monitor. Safe headless while the service is idle.
  (default)      Full presenter: modeset once + page-flip per frame. Needs the
                 monitor connected and DRM master (run as root / via the service).

Connector (--connector / DRM_CONNECTOR):
  HDMI-A-2                 single port (field default)
  HDMI-A-1,HDMI-A-2        dual: same YU12 FB page-flipped onto both CRTCs
  auto                     all connected usable HDMI-A ports (modes must match)

Dual v1 requires matching hdisplay×vdisplay (prefer both EDID 1920×1080). Mixed
modes fall back to best single (prefer HDMI-A-2) with a journal line.

Input: H.264 Annex-B ES on stdin (live from the endpoint) or --es-file PATH.
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import mmap
import os
import select
import signal
import sys
import time
from dataclasses import dataclass

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstApp, GstVideo  # noqa: E402,F401  (GstApp arms appsink overrides)

# Reuse the DRM ctypes foundation + helpers from the dual mirror (same dir).
import dual_drm_mirror as ddm  # noqa: E402
# Shared splash paint (same font/layout as idle_splash.py) for in-process handoff.
from idle_splash import paint_splash, _unbind_vtconsole  # noqa: E402


def log(msg: str) -> None:
    print(f"[atomic-drm] {msg}", file=sys.stderr, flush=True)


# Process-global flags (set from signal handlers).
_STOP = False
_IDLE_SPLASH_REQ = False
_RESUME_REQ = False

# run_present return: rebuild GStreamer pipeline but keep DRM master + splash FB.
RC_RESUME_VIDEO = 10


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        n = default
    else:
        try:
            n = int(raw, 0)
        except ValueError:
            n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _es_queue_elem() -> str:
    """Non-leaky pre-decoder queue. Size is a lag cap, not a smoothness cushion.

    4 MiB (old default) banks ~4s of ES when avdec_h264 is CPU-starved
    (2026-08-31, ARM 600 MHz under-voltage). appsink drop=true only drops
    *decoded* frames — it cannot skip compressed NALs already sitting in this
    queue, so a slow decoder plays old video. 256 KiB ≈ 0.3s at 7 Mbit/s.
    Backpressure goes to stdin → endpoint GOP-aligned drop. Never leaky.
    """
    n = _env_int("O3_ATOMIC_ES_QUEUE_BYTES", 256 * 1024, 64 * 1024, 4 * 1024 * 1024)
    return f"queue max-size-buffers=0 max-size-bytes={n} max-size-time=0"


DRM_MODE_PAGE_FLIP_EVENT = 0x01
DRM_MODE_CONNECTOR_HDMIA = 11
DRM_MODE_OBJECT_PLANE = 0xEEEEEEEE
DRM_PROP_NAME_LEN = 32
# fourcc('Y','U','1','2') == DRM_FORMAT_YUV420 (3-plane I420, U before V).
DRM_FORMAT_YU12 = ord("Y") | (ord("U") << 8) | (ord("1") << 16) | (ord("2") << 24)

# Measured from the live O3 SPS VUI (2026-07-15, this session): colour_primaries=1,
# transfer_characteristics=1, matrix_coefficients=1, video_full_range_flag=0 ->
# BT.709, limited range. vc4's YUV plane defaults to something else (BT.601-ish)
# when these plane properties are left unset, which is invisible on desaturated
# everyday scenes but blows out anything with real chroma: a near-black translucent
# OSD bar renders as solid saturated green, and a TV's saturated content ghosts
# cyan/blue. mpv/kmssink set this correctly from the decoder's negotiated
# colorimetry; our from-scratch presenter never did — this is the fix.
PLANE_COLOR_ENCODING = "ITU-R BT.709 YCbCr"
PLANE_COLOR_RANGE = "YCbCr limited range"


class drmModeObjectProperties(ctypes.Structure):
    _fields_ = [
        ("count_props", ctypes.c_uint32),
        ("props", ctypes.POINTER(ctypes.c_uint32)),
        ("prop_values", ctypes.POINTER(ctypes.c_uint64)),
    ]


class drmModePropertyEnum(ctypes.Structure):
    _fields_ = [
        ("value", ctypes.c_uint64),
        ("name", ctypes.c_char * DRM_PROP_NAME_LEN),
    ]


class drmModePropertyRes(ctypes.Structure):
    _fields_ = [
        ("prop_id", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("name", ctypes.c_char * DRM_PROP_NAME_LEN),
        ("count_values", ctypes.c_int),
        ("values", ctypes.POINTER(ctypes.c_uint64)),
        ("count_enums", ctypes.c_int),
        ("enums", ctypes.POINTER(drmModePropertyEnum)),
        ("count_blobs", ctypes.c_int),
        ("blob_ids", ctypes.POINTER(ctypes.c_uint32)),
    ]


# --- plane layout ------------------------------------------------------------


class Layout:
    """I420 plane geometry for the decoder's (padded) output buffer."""

    __slots__ = ("width", "height", "stride", "padded_h", "size",
                 "pitches", "offsets")

    def __init__(self, width: int, height: int, size: int):
        self.width = width
        self.height = height
        self.size = size
        stride, padded_h = _solve_i420(size, width)
        self.stride = stride
        self.padded_h = padded_h
        cw, ch = stride // 2, padded_h // 2
        y_off = 0
        u_off = stride * padded_h
        v_off = u_off + cw * ch
        self.pitches = [stride, cw, cw, 0]
        self.offsets = [y_off, u_off, v_off, 0]

    @classmethod
    def from_video_meta(cls, width: int, height: int, size: int, meta) -> "Layout":
        """Build a Layout from GStreamer's own per-plane stride/offset (GstVideoMeta).

        Bypasses the alignment guess in `_solve_i420` entirely — meta.stride[]/
        offset[] are exactly what the decode/scale chain actually produced,
        whatever internal padding a given decoder or GStreamer build happens to
        use (e.g. V4L2 macroblock-row height alignment), rather than a guess that
        only happens to be right for alignments the guess tries.
        """
        lay = cls.__new__(cls)
        lay.width = width
        lay.height = height
        lay.size = size
        lay.stride = int(meta.stride[0])
        lay.padded_h = int(meta.offset[1] // lay.stride) if lay.stride else height
        lay.pitches = [int(meta.stride[0]), int(meta.stride[1]), int(meta.stride[2]), 0]
        lay.offsets = [int(meta.offset[0]), int(meta.offset[1]), int(meta.offset[2]), 0]
        return lay

    def __repr__(self):
        return (f"Layout({self.width}x{self.height} stride={self.stride} "
                f"padded_h={self.padded_h} size={self.size} "
                f"pitches={self.pitches[:3]} offsets={self.offsets[:3]})")


def _build_layout(sample, width: int, height: int, size: int) -> "Layout":
    """Prefer the sample's own GstVideoMeta over `_solve_i420`'s alignment guess.

    Why this matters: the 2026-07-15 "HW decode has a chroma bug" finding (see
    module docstring) A/B'd HW vs SW decode by dumping raw decoded bytes and
    re-interpreting them with THIS guessed layout on both sides. If the guess
    happened to match one decoder's real packing but not the other's, that A/B
    would look exactly like a decoder bug even if both decoders were correct —
    the guess, not the decoder, would be misplacing the chroma planes. That was
    a real gap worth closing regardless of the outcome. Meta-based layout
    removes the guess so extraction is correct regardless of which decoder is
    live; falls back to the old guess (unchanged behavior) if meta is
    unavailable — which, per a 2026-08-22 retest (see `_decode_element`), is
    the case for both decoders on this pipeline. The retest used this function
    to confirm the guess itself is correct for both (verified via a direct
    pixel dump, not just self-consistent size math) and, with layout no longer
    in question, reproduced the original chroma defect on HW and a clean frame
    on SW — the 2026-07-15 finding was a real bcm2835-codec bug, not a layout
    artifact. This function stays: it's a strictly safer way to extract a
    decoded frame than guessing, independent of that result.
    """
    try:
        buf = sample.get_buffer()
        meta = GstVideo.buffer_get_video_meta(buf) if buf is not None else None
    except Exception as e:
        log(f"layout: video-meta lookup failed ({e}) — falling back to size-based guess")
        meta = None
    if meta is not None and getattr(meta, "n_planes", 0) >= 3:
        try:
            lay = Layout.from_video_meta(width, height, size, meta)
            guess_stride, guess_padded_h = _solve_i420(size, width)
            if lay.stride != guess_stride:
                log(f"layout: video-meta DIFFERS from guess "
                    f"(meta stride={lay.stride} offsets={lay.offsets[:3]} vs "
                    f"guessed stride={guess_stride} padded_h={guess_padded_h}) "
                    f"— using meta")
            else:
                log(f"layout: video-meta matches guess ({lay!r})")
            return lay
        except Exception as e:
            log(f"layout: video-meta present but unusable ({e}) — falling back to guess")
    else:
        log("layout: no GstVideoMeta on sample — falling back to size-based guess")
    return Layout(width, height, size)


def _solve_i420(size: int, width: int) -> tuple[int, int]:
    """Derive (luma stride, padded height) for a contiguous I420 buffer of `size`.

    Some decoders pad: stride = width aligned up (usually 32/64) and height aligned
    up (usually 16). size == stride*padded_h*3//2. Try common alignments; verify.
    avdec_h264 normally hands back unpadded I420 (stride==width, no row padding),
    which the fallback branch below already covers.
    """
    for align in (64, 32, 256, 128, 16):
        stride = ((width + align - 1) // align) * align
        if size % stride:
            continue
        rows = size // stride  # == padded_h * 3/2
        if rows % 3:
            continue
        padded_h = rows * 2 // 3
        if stride * padded_h * 3 // 2 == size and padded_h >= 1:
            return stride, padded_h
    # Fallback: assume no padding.
    stride = width
    padded_h = (size * 2 // 3) // stride if stride else 0
    log(f"WARN could not solve I420 layout for size={size} width={width}; "
        f"assuming stride={stride} padded_h={padded_h}")
    return stride, padded_h


# --- extra libdrm bindings on top of dual_drm_mirror's LibDrm ----------------


def _bind_extra_drm(lib) -> None:
    if getattr(lib, "_atomic_bound", False):
        return
    lib.drmModePageFlip.restype = ctypes.c_int
    lib.drmModePageFlip.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32,
                                    ctypes.c_uint32, ctypes.c_void_p]
    lib.drmSetMaster.restype = ctypes.c_int
    lib.drmSetMaster.argtypes = [ctypes.c_int]
    lib.drmDropMaster.restype = ctypes.c_int
    lib.drmDropMaster.argtypes = [ctypes.c_int]
    lib.drmModeObjectGetProperties.restype = ctypes.c_void_p
    lib.drmModeObjectGetProperties.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32]
    lib.drmModeFreeObjectProperties.argtypes = [ctypes.c_void_p]
    lib.drmModeGetProperty.restype = ctypes.c_void_p
    lib.drmModeGetProperty.argtypes = [ctypes.c_int, ctypes.c_uint32]
    lib.drmModeFreeProperty.argtypes = [ctypes.c_void_p]
    lib.drmModeObjectSetProperty.restype = ctypes.c_int
    lib.drmModeObjectSetProperty.argtypes = [
        ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint64,
    ]
    lib._atomic_bound = True


def _set_plane_enum_prop(lib, fd: int, plane_id: int, prop_name: str, enum_name: str) -> bool:
    """Set a plane's enum-valued property (e.g. COLOR_ENCODING) by enum name.

    Returns True if the property existed, had a matching enum value, and was set.
    Fails soft (logs + returns False) otherwise — older/different vc4 kernels may
    name these differently, and we must never crash the presenter over cosmetics.
    """
    props_p = lib.drmModeObjectGetProperties(fd, plane_id, DRM_MODE_OBJECT_PLANE)
    if not props_p:
        log(f"colorspace: drmModeObjectGetProperties(plane={plane_id}) failed")
        return False
    try:
        props = ctypes.cast(props_p, ctypes.POINTER(drmModeObjectProperties)).contents
        for i in range(props.count_props):
            prop_id = props.props[i]
            prop_p = lib.drmModeGetProperty(fd, prop_id)
            if not prop_p:
                continue
            try:
                prop = ctypes.cast(prop_p, ctypes.POINTER(drmModePropertyRes)).contents
                if prop.name.decode("ascii", "ignore") != prop_name:
                    continue
                for j in range(prop.count_enums):
                    e = prop.enums[j]
                    if e.name.decode("ascii", "ignore") == enum_name:
                        r = lib.drmModeObjectSetProperty(
                            fd, plane_id, DRM_MODE_OBJECT_PLANE, prop_id, e.value)
                        if r == 0:
                            log(f"colorspace: plane {plane_id} {prop_name}={enum_name!r} set")
                            return True
                        log(f"colorspace: setProperty {prop_name}={enum_name!r} failed ({r})")
                        return False
                log(f"colorspace: {prop_name} has no enum {enum_name!r} on plane {plane_id}")
                return False
            finally:
                lib.drmModeFreeProperty(prop_p)
    finally:
        lib.drmModeFreeObjectProperties(props_p)
    log(f"colorspace: plane {plane_id} has no property {prop_name!r}")
    return False


# --- GStreamer decode half --------------------------------------------------


def _aspect_mode() -> str:
    """How non-16:9 coded frames fill the fixed DRM canvas.

    O3_HDMI_ASPECT:
      crop      (default) true center-crop to canvas AR, then scale. 4:3
                (e.g. 1440×1080) becomes full-bleed 16:9 matching native
                goggles 16:9 FOV — better for crowd viewing; pilots still
                see native 4:3 in the goggles.
      letterbox scale-to-fit + black bars (full FOV, "square box" on 16:9 TV).

    True 16:9 sources are identical under either mode (no crop, no bars).
    PAR is always forced 1/1 so SPS sample_aspect_ratio cannot re-crop us.

    IMPORTANT: do NOT use `videoscale add-borders=false` for crop — that
    *stretches* anamorphically (proven 2026-07-17: circle ratio 1.33 on
    1440→1920) and warps corners vs native 16:9. Crop path uses
    `aspectratiocrop` then uniform `videoscale`.
    """
    raw = (os.environ.get("O3_HDMI_ASPECT", "crop") or "crop").strip().lower()
    if raw in ("letterbox", "fit", "pad", "borders", "pillarbox"):
        return "letterbox"
    if raw in ("crop", "fill", "cover", "zoom"):
        return "crop"
    log(f"O3_HDMI_ASPECT={raw!r} unknown — using crop")
    return "crop"


def _aspect_scale_chain(aspect: str, target_w: int, target_h: int) -> str:
    """GStreamer chain: post-decode PAR lock → aspect policy → fixed canvas.

    Returns the fragment between avdec and appsink (no trailing/leading !).
    """
    caps = (
        f"video/x-raw, format=I420, width={target_w}, height={target_h}, "
        "pixel-aspect-ratio=1/1"
    )
    # Force square pixels (and I420, harmless when the decoder already emits it)
    # first so crop/scale use coded width/height only. format=I420 is included
    # here rather than as a separate capsfilter right after the decoder element:
    # this GStreamer build's gst-launch parser rejects two bare-caps fragments
    # back to back ("no element 'video'") — confirmed with a minimal repro
    # (`videotestsrc ! video/x-raw,format=I420 ! video/x-raw,pixel-aspect-ratio=1/1
    # ! fakesink` fails the same way). Folding format into this one filter keeps
    # a single caps fragment after the decoder element.
    par = "video/x-raw, format=I420, pixel-aspect-ratio=1/1"
    if aspect == "crop":
        # Center-crop to canvas AR (top/bottom off a 4:3 1440×1080 → 1440×810),
        # then uniform scale to the DRM canvas. Matches native goggles 16:9 FOV.
        return (
            f"{par} ! aspectratiocrop aspect-ratio={target_w}/{target_h} ! "
            f"videoscale ! {caps}"
        )
    # letterbox: fit inside canvas, pad bars (add-borders=true preserves AR).
    return (
        f"{par} ! videoscale add-borders=true ! {caps}"
    )


def _decode_element() -> str:
    """Always avdec_h264. HW decode (v4l2h264dec) is gone.

    2026-07-15 field + 2026-08-22 pixel retest: bcm2835-codec reconstructs chroma
    wrong on flat near-black luma (OSD bar / ghosting). hw-nv12 did not fix it.
    `O3_HDMI_DECODE` is ignored if set — log once so old unit files don't confuse.
    """
    raw = (os.environ.get("O3_HDMI_DECODE", "") or "").strip().lower()
    if raw and raw not in ("sw", "avdec", "avdec_h264"):
        log(f"O3_HDMI_DECODE={raw!r} ignored — HW decode removed, using avdec_h264")
    return "avdec_h264 ! "


def build_pipeline(es_fd: int, target_w: int = 1920, target_h: int = 1080) -> tuple:
    """fdsrc -> queue -> h264parse -> avdec_h264 (SW) -> scale -> I420 -> appsink.

    SW decode (avdec_h264), not HW (v4l2h264dec) — see module docstring: the Pi's
    bcm2835-codec HW decode block reconstructs chroma wrong at high-contrast edges
    (proven via byte-identical A/B against software decode, 2026-07-15). avdec_h264
    measured 120-125 fps on this Pi 4, well above the ~50-60 fps needed.

    Fixed-size canvas: the O3 stream's coded resolution is NOT guaranteed to match
    the DRM mode we scan out to (field-observed 2026-07-15: goggles sent 1440x1080
    (4:3) while the CRTC was fixed at 1920x1080 — the mismatched dumb-buffer size
    made drmModeSetCrtc fail with ENOSPC on every single frame, permanent black
    screen). Scaling to the presenter's target size here means the appsink always
    hands over exactly target_w x target_h, so the DRM buffer/mode never mismatch
    regardless of what resolution the goggles are actually sending.

    Aspect (O3_HDMI_ASPECT, default crop):
      crop      — aspectratiocrop to canvas AR + uniform videoscale. A 4:3 feed
                  (1440×1080) center-crops top/bottom to 16:9 then upscales —
                  same FOV geometry as when the goggles send native 16:9.
                  NEVER videoscale add-borders=false (that stretches 4:3→16:9
                  anamorphically and warps corners; fixed 2026-07-17).
      letterbox — videoscale add-borders=true: full source FOV with black bars.

    Forced 1/1 PAR before crop/scale: without it, elements negotiate against the
    SPS's own (possibly non-square) sample_aspect_ratio and can crop or pad
    unexpectedly. Pixel-only geometry is intentional.
    """
    Gst.init(None)
    aspect = _aspect_mode()
    scale_chain = _aspect_scale_chain(aspect, target_w, target_h)
    decode_elem = _decode_element()
    # Debug capture tap (no restart needed — endpoint re-execs us each spawn): if the
    # flag file /tmp/o3_tee_es exists, tee the raw stdin ES to a per-pid file so the
    # exact bytes the decoder chokes on can be analysed offline.
    tee_es = os.path.exists("/tmp/o3_tee_es") and es_fd == 0
    if tee_es:
        cap = f"/tmp/o3_live_es_{os.getpid()}.h264"
        desc = (
            f"fdsrc fd={es_fd} do-timestamp=false ! tee name=estee "
            f"estee. ! queue ! filesink location={cap} async=false "
            f"estee. ! {_es_queue_elem()} ! "
            f"h264parse config-interval=-1 ! {decode_elem}"
            f"{scale_chain} ! "
            "appsink name=sink emit-signals=false sync=false max-buffers=4 drop=true"
        )
        log(f"pipeline aspect={aspect} (ES capture → {cap}): {desc}")
        pipeline = Gst.parse_launch(desc)
        return pipeline, pipeline.get_by_name("sink")
    # CRITICAL: the pre-decoder queue must NOT be leaky. Dropping *compressed ES*
    # here loses reference NALs → the decoder emits accumulating macroblock garbage
    # until the next keyframe ("picture broken" while motion is smooth). We stay live
    # by dropping *decoded* frames at the appsink (drop=true), never the ES. If present
    # falls behind, backpressure propagates to stdin and the endpoint drops upstream at
    # GOP/SPS boundaries (clean), which is the right place to drop.
    desc = (
        f"fdsrc fd={es_fd} do-timestamp=false ! "
        f"{_es_queue_elem()} ! "
        "h264parse config-interval=-1 ! "
        f"{decode_elem}"
        f"{scale_chain} ! "
        "appsink name=sink emit-signals=false sync=false max-buffers=4 drop=true"
    )
    log(f"pipeline aspect={aspect}: {desc}")
    pipeline = Gst.parse_launch(desc)
    return pipeline, pipeline.get_by_name("sink")


def _sample_dims(sample) -> tuple[int, int]:
    import re
    s = sample.get_caps().to_string()
    w = re.search(r"width=\(int\)(\d+)", s)
    h = re.search(r"height=\(int\)(\d+)", s)
    return (int(w.group(1)) if w else 0, int(h.group(1)) if h else 0)


def _map_sample(sample):
    """Return (bytes, size) of the sample's single memory, or (None, 0)."""
    buf = sample.get_buffer()
    if buf is None:
        return None, 0
    mem = buf.peek_memory(0)
    ok, info = mem.map(Gst.MapFlags.READ)
    if not ok:
        return None, 0
    try:
        # info.data is a memoryview/bytes-like; keep as a view to avoid a copy where
        # possible. We hand it straight to the DRM buffer memcpy.
        data = info.data
        size = info.size
        return (data, size, mem, info)
    except Exception:
        mem.unmap(info)
        return None, 0


# --- probe mode -------------------------------------------------------------


def run_probe(appsink, pipeline, limit: int) -> int:
    pipeline.set_state(Gst.State.PLAYING)
    n = 0
    lay = None
    t0 = time.monotonic()
    try:
        while True:
            sample = appsink.try_pull_sample(200 * Gst.MSECOND)
            if sample is None:
                if appsink.is_eos():
                    break
                if pipeline.get_state(0)[1] == Gst.State.NULL:
                    break
                continue
            buf = sample.get_buffer()
            mem = buf.peek_memory(0)
            size = mem.get_sizes()[0]
            if lay is None:
                w, h = _sample_dims(sample)
                lay = _build_layout(sample, w, h, size)
                log(f"caps={sample.get_caps().to_string()[:100]}")
                log(f"n_memory={buf.n_memory()} {lay!r}")
            n += 1
            if n >= limit:
                break
    finally:
        pipeline.set_state(Gst.State.NULL)
    dt = time.monotonic() - t0
    log(f"probe done: {n} frames in {dt:.1f}s = {n / dt if dt else 0:.1f} fps")
    return 0 if n else 1


# --- DRM present half -------------------------------------------------------


@dataclass
class Output:
    """One connected HDMI port + its CRTC (shared YU12 FB for multi)."""

    name: str
    connector_id: int
    crtc_id: int
    mode: object  # ddm.drmModeModeInfo
    plane_id: int | None = None
    flip_pending: bool = False


def _parse_connector_spec(spec: str | None) -> list[str] | None:
    """Return wanted connector names, or None for 'all connected HDMI-A'.

    Accepts: None / 'auto' → all; 'HDMI-A-2' → one; 'HDMI-A-1,HDMI-A-2' → multi.
    """
    if spec is None:
        return None
    s = spec.strip()
    if not s or s.lower() in ("auto", "any", "*"):
        return None
    parts = [p.strip() for p in s.split(",") if p.strip()]
    return parts or None


def _prefer_single(cands: list[Output]) -> Output:
    """Best single output when dual cannot run (prefer far micro-HDMI A-2)."""
    for o in cands:
        if o.name == "HDMI-A-2":
            return o
    return cands[0]


class AtomicPresenter:
    """Open card, become master, modeset ONCE per CRTC, page-flip a YU12 pool.

    Single or dual HDMI: one decode → one memcpy → page-flip the **same** FB onto
    every active CRTC. Dual requires matching hdisplay×vdisplay (same-buffer rule).
    """

    N_BUFFERS = 2  # double-buffer with wait-per-flip (tear-free, paces to refresh)

    def __init__(self, device: str, connector_name: str | None,
                 prefer_w: int = 1920, prefer_h: int = 1080):
        self.device = device
        # Raw CLI/env string: None/auto → all HDMI; "A-2" or "A-1,A-2" → list.
        self.connector_name = connector_name
        self.prefer_w = prefer_w
        self.prefer_h = prefer_h
        self.fd = -1
        self.lib = ddm.LibDrm().lib
        _bind_extra_drm(self.lib)
        self.outputs: list[Output] = []
        # Scalar aliases (first/primary output) for single-path log compatibility.
        self.connector_id = 0
        self.crtc_id = 0
        self.mode: ddm.drmModeModeInfo | None = None
        self.layout: Layout | None = None
        self.opened = False
        self._bufs: list[dict] = []
        self._did_modeset = False
        self._flips_pending = 0  # outstanding page-flip events across all CRTCs
        self._front = 0
        self._idle: dict | None = None  # XRGB idle-splash dumb buffer (in-process)

    @property
    def _flip_pending(self) -> bool:
        """True while any CRTC still has an outstanding page-flip event."""
        return self._flips_pending > 0

    def open(self) -> None:
        self.fd = os.open(self.device, os.O_RDWR | os.O_CLOEXEC)
        self.lib.drmSetClientCap(self.fd, ddm.DRM_CLIENT_CAP_UNIVERSAL_PLANES, 1)
        # May fail if external idle_splash still holds master — expected during the
        # seamless splash→video handoff (we spin ensure_master after NEED_MASTER).
        if self.lib.drmSetMaster(self.fd) != 0:
            log("drmSetMaster deferred — waiting for splash to release")
        self.opened = True

    def ensure_master(self, timeout: float = 3.0) -> bool:
        """Poll drmSetMaster until we own the card (handoff from external splash)."""
        if self.fd < 0:
            return False
        if self.lib.drmSetMaster(self.fd) == 0:
            return True
        t0 = time.monotonic()
        while not _STOP and (time.monotonic() - t0) < max(0.05, timeout):
            time.sleep(0.05)
            if self.lib.drmSetMaster(self.fd) == 0:
                return True
        return self.lib.drmSetMaster(self.fd) == 0

    def close(self) -> None:
        self._destroy_idle_fb()
        for b in self._bufs:
            self._destroy_buf(b)
        self._bufs = []
        if self.fd >= 0:
            try:
                self.lib.drmDropMaster(self.fd)
            except Exception:
                pass
            os.close(self.fd)
            self.fd = -1

    def _destroy_idle_fb(self) -> None:
        """Tear down the XRGB idle-splash FB (if any)."""
        idle = getattr(self, "_idle", None)
        if not idle:
            return
        self._idle = None
        try:
            if idle.get("map") is not None:
                idle["map"].close()
        except Exception:
            pass
        try:
            if idle.get("fb"):
                self.lib.drmModeRmFB(self.fd, idle["fb"])
        except Exception:
            pass
        try:
            if idle.get("handle"):
                d = ddm.drm_mode_destroy_dumb(handle=idle["handle"])
                fcntl.ioctl(self.fd, ddm.DRM_IOCTL_MODE_DESTROY_DUMB, d)
        except Exception:
            pass

    def present_idle_splash(self, text: str | None = None) -> bool:
        """Paint the idle message while STILL holding DRM master (seamless feed→splash).

        Process-swap handoffs always flash the console: the presenter destroys its FBs
        on exit, then idle_splash cold-starts. This path never drops master — SetCrtc
        swaps the scanout FB from live YU12 to a static XRGB text frame in-process.
        On dual, the same XRGB FB is SetCrtc'd onto every output.
        Returns True if the splash is on screen on at least one CRTC.
        """
        if self.fd < 0 or not self.opened or not self.outputs or self.mode is None:
            return False
        text = (text or os.environ.get("O3_IDLE_SPLASH_TEXT", "waiting for live FPV feed")
                or "waiting for live FPV feed").strip()
        w, h = int(self.mode.hdisplay), int(self.mode.vdisplay)
        # Drain any pending page-flip so CRTCs are free for SetCrtc.
        try:
            self._drain_flip(block=False)
            if self._flips_pending > 0:
                self._drain_flip(block=True)
        except Exception:
            pass
        try:
            creq = ddm.drm_mode_create_dumb(height=h, width=w, bpp=32, flags=0)
            fcntl.ioctl(self.fd, ddm.DRM_IOCTL_MODE_CREATE_DUMB, creq)
            pitch = int(creq.pitch)
            handles = (ctypes.c_uint32 * 4)(creq.handle, 0, 0, 0)
            pitches = (ctypes.c_uint32 * 4)(pitch, 0, 0, 0)
            offsets = (ctypes.c_uint32 * 4)(0, 0, 0, 0)
            fb_id = ctypes.c_uint32(0)
            r = self.lib.drmModeAddFB2(
                self.fd, w, h, ddm.DRM_FORMAT_XRGB8888,
                handles, pitches, offsets, ctypes.byref(fb_id), 0,
            )
            if r != 0:
                raise OSError(f"AddFB2(XRGB idle) failed ({r})")
            mreq = ddm.drm_mode_map_dumb(handle=creq.handle)
            fcntl.ioctl(self.fd, ddm.DRM_IOCTL_MODE_MAP_DUMB, mreq)
            mp = mmap.mmap(
                self.fd, creq.size, flags=mmap.MAP_SHARED,
                prot=mmap.PROT_READ | mmap.PROT_WRITE, offset=mreq.offset,
            )
            paint_splash(mp, pitch, w, h, text)
            ok = 0
            for o in self.outputs:
                conn_arr = (ctypes.c_uint32 * 1)(o.connector_id)
                mode_copy = ddm.copy_mode(o.mode)
                rr = self.lib.drmModeSetCrtc(
                    self.fd, o.crtc_id, fb_id.value, 0, 0,
                    conn_arr, 1, ctypes.byref(mode_copy),
                )
                if rr != 0:
                    log(f"SetCrtc(idle splash) failed on {o.name} ({rr})")
                else:
                    ok += 1
            if ok == 0:
                mp.close()
                try:
                    self.lib.drmModeRmFB(self.fd, fb_id.value)
                except Exception:
                    pass
                raise OSError("SetCrtc(idle splash) failed on all outputs")
            self._destroy_idle_fb()
            self._idle = {
                "handle": creq.handle, "fb": fb_id.value, "map": mp, "size": creq.size,
            }
            self._flips_pending = 0
            for o in self.outputs:
                o.flip_pending = False
            _unbind_vtconsole()
            self._did_modeset = True
            names = ", ".join(o.name for o in self.outputs)
            log(f"IDLE_SPLASH showing {w}x{h} on {names} text={text!r}")
            return True
        except Exception as e:
            log(f"present_idle_splash failed: {e}")
            return False

    def setup(self) -> None:
        """Discover connectors, assign distinct CRTCs, enforce same-buffer mode rule."""
        want = _parse_connector_spec(self.connector_name)
        res_p = self.lib.drmModeGetResources(self.fd)
        if not res_p:
            raise OSError("drmModeGetResources failed")
        res = ctypes.cast(res_p, ctypes.POINTER(ddm.drmModeRes)).contents
        crtc_ids = [res.crtcs[i] for i in range(res.count_crtcs)]
        crtc_index = {cid: i for i, cid in enumerate(crtc_ids)}

        found: list[Output] = []
        used_crtcs: set[int] = set()

        candidates: list[tuple] = []
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
            if want is None:
                match = connected and conn.connector_type == DRM_MODE_CONNECTOR_HDMIA
            else:
                match = connected and name in want
            if not match:
                self.lib.drmModeFreeConnector(conn_p)
                continue
            modes = [conn.modes[j] for j in range(conn.count_modes)]
            mode = ddm.pick_mode(modes, self.prefer_w, self.prefer_h)
            crtc_id = self._crtc_for(conn, crtc_ids, used_crtcs)
            if not crtc_id or mode is None:
                log(f"{name}: skip (no free CRTC or no EDID mode)")
                self.lib.drmModeFreeConnector(conn_p)
                continue
            used_crtcs.add(crtc_id)
            cidx = crtc_index.get(crtc_id, 0)
            plane_id = self._find_plane_for_crtc(cidx, crtc_id)
            candidates.append((name, conn_id, crtc_id, mode, plane_id))
            self.lib.drmModeFreeConnector(conn_p)

        self.lib.drmModeFreeResources(res_p)

        if not candidates:
            raise RuntimeError(
                f"no usable connector (name={self.connector_name!r}) on {self.device}")

        if want is not None:
            by_name = {c[0]: c for c in candidates}
            ordered = [by_name[n] for n in want if n in by_name]
            missing = [n for n in want if n not in by_name]
            if missing:
                log(f"requested connectors not usable: {missing}")
            candidates = ordered if ordered else candidates
        else:
            candidates.sort(key=lambda c: (0 if c[0] == "HDMI-A-1" else
                                           1 if c[0] == "HDMI-A-2" else 2, c[0]))

        for name, conn_id, crtc_id, mode, plane_id in candidates:
            found.append(Output(
                name=name, connector_id=conn_id, crtc_id=crtc_id,
                mode=mode, plane_id=plane_id,
            ))

        # Same-buffer rule: multi requires matching hdisplay×vdisplay.
        if len(found) > 1:
            dims = {(int(o.mode.hdisplay), int(o.mode.vdisplay)) for o in found}
            if len(dims) > 1:
                detail = ", ".join(
                    f"{o.name}={o.mode.hdisplay}x{o.mode.vdisplay}" for o in found)
                log(f"dual refused: mismatched modes ({detail}) — falling back to single")
                found = [_prefer_single(found)]

        self.outputs = found
        primary = self.outputs[0]
        self.connector_id = primary.connector_id
        self.crtc_id = primary.crtc_id
        self.mode = primary.mode

        if len(self.outputs) > 1:
            parts = [
                f"{o.name} (conn={o.connector_id} crtc={o.crtc_id})"
                for o in self.outputs
            ]
            m = self.mode
            log(f"dual atomic targets: {', '.join(parts)} "
                f"mode={m.hdisplay}x{m.vdisplay}@{m.vrefresh} (same-buffer page-flip)")
        else:
            o = self.outputs[0]
            log(f"target {o.name}: connector={o.connector_id} crtc={o.crtc_id} "
                f"mode={o.mode.hdisplay}x{o.mode.vdisplay}@{o.mode.vrefresh}")

        for o in self.outputs:
            if o.plane_id:
                _set_plane_enum_prop(self.lib, self.fd, o.plane_id,
                                      "COLOR_ENCODING", PLANE_COLOR_ENCODING)
                _set_plane_enum_prop(self.lib, self.fd, o.plane_id,
                                      "COLOR_RANGE", PLANE_COLOR_RANGE)
            else:
                log(f"colorspace: no primary plane for {o.name} crtc={o.crtc_id} "
                    f"— skipping COLOR_ENCODING/COLOR_RANGE fixup")

    def _find_plane_for_crtc(self, crtc_index: int, crtc_id: int) -> int | None:
        pres_p = self.lib.drmModeGetPlaneResources(self.fd)
        if not pres_p:
            return None
        pres = ctypes.cast(pres_p, ctypes.POINTER(ddm.drmModePlaneRes)).contents
        found = None
        for i in range(pres.count_planes):
            pid = pres.planes[i]
            pl_p = self.lib.drmModeGetPlane(self.fd, pid)
            if not pl_p:
                continue
            pl = ctypes.cast(pl_p, ctypes.POINTER(ddm.drmModePlane)).contents
            if pl.possible_crtcs & (1 << crtc_index):
                if pl.crtc_id == crtc_id or found is None:
                    found = pid
                    if pl.crtc_id == crtc_id:
                        self.lib.drmModeFreePlane(pl_p)
                        break
            self.lib.drmModeFreePlane(pl_p)
        self.lib.drmModeFreePlaneResources(pres_p)
        return found

    def _crtc_for(self, conn, crtc_ids: list[int], used: set[int] | None = None) -> int:
        """Pick a free CRTC for this connector (skip CRTCs already assigned)."""
        used = used or set()

        def _pick_from_possible(possible: int, preferred: int = 0) -> int:
            if preferred and preferred not in used and preferred in crtc_ids:
                return preferred
            for bit, c in enumerate(crtc_ids):
                if (possible & (1 << bit)) and c not in used:
                    return c
            return 0

        enc_id = conn.encoder_id
        if enc_id:
            enc_p = self.lib.drmModeGetEncoder(self.fd, enc_id)
            if enc_p:
                enc = ctypes.cast(enc_p, ctypes.POINTER(ddm.drmModeEncoder)).contents
                cid = int(enc.crtc_id) if enc.crtc_id else 0
                possible = int(enc.possible_crtcs)
                self.lib.drmModeFreeEncoder(enc_p)
                picked = _pick_from_possible(possible, cid)
                if picked:
                    return picked
        for ei in range(conn.count_encoders):
            enc_p = self.lib.drmModeGetEncoder(self.fd, conn.encoders[ei])
            if not enc_p:
                continue
            enc = ctypes.cast(enc_p, ctypes.POINTER(ddm.drmModeEncoder)).contents
            possible = int(enc.possible_crtcs)
            self.lib.drmModeFreeEncoder(enc_p)
            picked = _pick_from_possible(possible)
            if picked:
                return picked
        return 0

    # --- dumb YU12 buffers ---

    def _alloc_bufs(self, layout: Layout) -> None:
        self.layout = layout
        for _ in range(self.N_BUFFERS):
            self._bufs.append(self._create_buf(layout))
        log(f"allocated {len(self._bufs)} YU12 dumb buffers "
            f"({layout.size} bytes each), fb_ids={[b['fb'] for b in self._bufs]}")

    def _create_buf(self, layout: Layout) -> dict:
        # One linear dumb allocation holding all 3 planes: width=stride, bpp=8 so
        # pitch==stride; height chosen so size covers the padded I420 frame.
        rows = (layout.size + layout.stride - 1) // layout.stride
        creq = ddm.drm_mode_create_dumb(height=rows, width=layout.stride, bpp=8, flags=0)
        fcntl.ioctl(self.fd, ddm.DRM_IOCTL_MODE_CREATE_DUMB, creq)
        handle, size = creq.handle, creq.size
        handles = (ctypes.c_uint32 * 4)(handle, handle, handle, 0)
        pitches = (ctypes.c_uint32 * 4)(*layout.pitches)
        offsets = (ctypes.c_uint32 * 4)(*layout.offsets)
        fb_id = ctypes.c_uint32(0)
        r = self.lib.drmModeAddFB2(
            self.fd, layout.width, layout.height, DRM_FORMAT_YU12,
            handles, pitches, offsets, ctypes.byref(fb_id), 0,
        )
        if r != 0:
            raise OSError(f"drmModeAddFB2(YU12) failed ({r}) "
                          f"pitches={layout.pitches[:3]} offsets={layout.offsets[:3]}")
        mreq = ddm.drm_mode_map_dumb(handle=handle)
        fcntl.ioctl(self.fd, ddm.DRM_IOCTL_MODE_MAP_DUMB, mreq)
        mp = mmap.mmap(self.fd, size, flags=mmap.MAP_SHARED,
                       prot=mmap.PROT_READ | mmap.PROT_WRITE, offset=mreq.offset)
        return {"handle": handle, "fb": fb_id.value, "map": mp, "size": size}

    def _destroy_buf(self, b: dict) -> None:
        try:
            if b.get("map") is not None:
                b["map"].close()
            if b.get("fb"):
                self.lib.drmModeRmFB(self.fd, b["fb"])
            if b.get("handle"):
                d = ddm.drm_mode_destroy_dumb(handle=b["handle"])
                fcntl.ioctl(self.fd, ddm.DRM_IOCTL_MODE_DESTROY_DUMB, d)
        except Exception:
            pass

    def _drain_flip(self, block: bool) -> None:
        """Consume page-flip events until no CRTC is pending (or non-block empty)."""
        while self._flips_pending > 0:
            r, _, _ = select.select([self.fd], [], [], None if block else 0)
            if self.fd not in r:
                return
            try:
                data = os.read(self.fd, 4096)
            except OSError:
                self._flips_pending = 0
                for o in self.outputs:
                    o.flip_pending = False
                return
            if not data:
                return
            # Parse drm_event stream: each event has type+length header (8 bytes).
            off = 0
            while off + 8 <= len(data) and self._flips_pending > 0:
                length = int.from_bytes(data[off + 4:off + 8], "little")
                if length < 8 or off + length > len(data):
                    self._flips_pending = max(0, self._flips_pending - 1)
                    break
                self._flips_pending -= 1
                off += length
            if self._flips_pending <= 0:
                self._flips_pending = 0
                for o in self.outputs:
                    o.flip_pending = False

    def _modeset_all(self, fb_id: int) -> None:
        """drmModeSetCrtc once per output with the same FB (initial or idle-resume)."""
        for o in self.outputs:
            conn_arr = (ctypes.c_uint32 * 1)(o.connector_id)
            mode_copy = ddm.copy_mode(o.mode)
            r = self.lib.drmModeSetCrtc(
                self.fd, o.crtc_id, fb_id, 0, 0, conn_arr, 1,
                ctypes.byref(mode_copy),
            )
            if r != 0:
                raise OSError(
                    f"drmModeSetCrtc failed on {o.name} crtc={o.crtc_id} ({r})")

    def present(self, data, size: int) -> None:
        """Copy one decoded I420 frame into a free buffer and page-flip all CRTCs.

        Modeset once per CRTC on first frame (or resume-from-idle); thereafter only
        page-flips of the **same** YU12 FB onto every active output.
        """
        if self.layout is None or not self.outputs:
            return
        # Resume from in-process idle splash: SetCrtc back to YU12 without DropMaster.
        if self._idle is not None:
            self._destroy_idle_fb()
            b = self._bufs[0]
            n = min(size, b["size"])
            b["map"][:n] = data[:n]
            self._modeset_all(b["fb"])
            self._did_modeset = True
            self._front = 0
            self._flips_pending = 0
            for o in self.outputs:
                o.flip_pending = False
            n_out = len(self.outputs)
            log(f"resumed video from idle splash (YU12 modeset x{n_out}, master held)")
            return
        if not self._did_modeset:
            b = self._bufs[0]
            n = min(size, b["size"])
            b["map"][:n] = data[:n]
            self._modeset_all(b["fb"])
            self._did_modeset = True
            self._front = 0
            n_out = len(self.outputs)
            log(f"initial modeset done on {n_out} output(s) — page-flips only from here")
            return
        # Wait for prior flips on all CRTCs so the back buffer is free to draw.
        self._drain_flip(block=True)
        back = 1 - self._front
        b = self._bufs[back]
        n = min(size, b["size"])
        b["map"][:n] = data[:n]
        ok = 0
        for o in self.outputs:
            r = self.lib.drmModePageFlip(
                self.fd, o.crtc_id, b["fb"], DRM_MODE_PAGE_FLIP_EVENT, None,
            )
            if r != 0:
                o.flip_pending = False
                continue  # transient EBUSY; other CRTCs may still flip
            o.flip_pending = True
            ok += 1
        if ok == 0:
            return  # all CRTCs busy; next frame retries without advancing front
        self._flips_pending = ok
        self._front = back



def _idle_first_enabled() -> bool:
    raw = (os.environ.get("O3_ATOMIC_IDLE_FIRST", "1") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _boot_idle_first(presenter: AtomicPresenter) -> int:
    """Take DRM master and paint splash before any ES exists.

    Boot pays GStreamer/PyGObject import + plugin load. USB plug then only needs
    SIGUSR2 + the first keyframe (same path as drone re-power idle-hold).
    Returns RC_RESUME_VIDEO to build the pipeline, 0 on SIGTERM, or
    RC_RESUME_VIDEO even on setup failure so the old first-frame NEED_MASTER
    path still runs.
    """
    global _STOP, _RESUME_REQ
    Gst.init(None)
    try:
        factory = Gst.ElementFactory.find("avdec_h264")
        log("decode plugin avdec_h264 ready" if factory is not None
            else "WARN avdec_h264 not in GStreamer registry")
    except Exception as e:
        log(f"decode plugin warmup skipped: {e}")
    try:
        presenter.open()
        presenter.setup()
    except Exception as e:
        log(f"idle-first DRM open/setup failed: {e} — falling through to first-frame handoff")
        return RC_RESUME_VIDEO
    log("NEED_MASTER")
    if not presenter.ensure_master(timeout=4.0):
        log("idle-first ensure_master failed — falling through to first-frame handoff")
        return RC_RESUME_VIDEO
    if not presenter.present_idle_splash():
        log("idle-first splash paint failed — falling through to first-frame handoff")
        return RC_RESUME_VIDEO
    log("idle-hold (boot) — holding DRM master until SIGTERM/SIGUSR2")
    while not _STOP:
        if _RESUME_REQ:
            _RESUME_REQ = False
            log("RESUME_VIDEO — build pipeline, keep DRM master + splash FB")
            return RC_RESUME_VIDEO
        time.sleep(0.1)
    log("idle-first released (SIGTERM)")
    return 0


def _enter_idle_hold(pipeline, presenter: AtomicPresenter, reason: str) -> int:
    """Stop decode, paint splash while still master, hold until SIGTERM or SIGUSR2.

    SIGUSR2 (RC_RESUME_VIDEO): rebuild decoder pipeline without DropMaster — seamless
    splash→live on drone re-power. SIGTERM: exit for process replacement.
    """
    global _STOP, _RESUME_REQ
    try:
        pipeline.set_state(Gst.State.NULL)
    except Exception:
        pass
    if presenter.opened and presenter._did_modeset:
        if presenter.present_idle_splash():
            log(f"idle-hold ({reason}) — holding DRM master until SIGTERM/SIGUSR2")
            while not _STOP:
                if _RESUME_REQ:
                    _RESUME_REQ = False
                    log("RESUME_VIDEO — rebuild pipeline, keep DRM master + splash FB")
                    return RC_RESUME_VIDEO
                time.sleep(0.1)
            log("idle-hold released (SIGTERM)")
            return 0
        log(f"idle-hold ({reason}): splash paint failed — fall through to exit")
    else:
        log(f"idle-hold ({reason}): no modeset yet — exit for external splash")
    return 2


def run_present(appsink, pipeline, presenter: AtomicPresenter) -> int:
    """Decode + present loop. Returns RC_RESUME_VIDEO to rebuild pipeline w/ same presenter."""
    global _STOP, _IDLE_SPLASH_REQ
    pipeline.set_state(Gst.State.PLAYING)
    n = 0
    t0 = time.monotonic()
    last_log = t0
    last_progress = t0  # time of last pulled sample (stall watchdog)
    # After this many seconds with no decoded frame, paint idle splash IN-PROCESS
    # (keep DRM master) instead of exiting — exit always flashed the console.
    # 0 disables. Default 8s: video-silence re-arm is 6s; a brief USB gap should
    # recover onto the last frame rather than flash the wait screen (2026-08-31).
    # Drone-off still gets splash, just ~5s later than the old 3s default.
    stall_secs = _env_float("O3_ATOMIC_STALL_SECS", 8.0)
    # Watch the GStreamer bus for decoder/pipeline ERROR + EOS. Without this a
    # decode error is silent and we loop forever.
    bus = pipeline.get_bus()
    # Debug: dump raw decoded I420 frames (appsink output, what we're about to show)
    # to O3_ATOMIC_DUMP_DIR so decode-stage corruption can be checked headlessly.
    dump_dir = os.environ.get("O3_ATOMIC_DUMP_DIR", "").strip()
    dumped = 0
    modeset_fails = 0
    close_presenter = True  # False when resuming (keep master + splash FB)
    rc = 0
    try:
        while not _STOP:
            # Endpoint asked us to show splash now (drone off / stream restart) —
            # do it without releasing DRM master.
            if _IDLE_SPLASH_REQ:
                _IDLE_SPLASH_REQ = False
                hold_rc = _enter_idle_hold(pipeline, presenter, "sigusr1")
                if hold_rc == RC_RESUME_VIDEO:
                    close_presenter = False
                return hold_rc
            # Drain pipeline bus (non-blocking): surface + act on ERROR/EOS.
            msg = bus.pop_filtered(
                Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.WARNING
            )
            while msg is not None:
                if msg.type == Gst.MessageType.ERROR:
                    err, dbg = msg.parse_error()
                    log(f"pipeline ERROR from {msg.src.get_name()}: {err.message} | {dbg}")
                    # Prefer seamless splash over hard exit when we already own the plane.
                    if presenter.opened and presenter._did_modeset:
                        hold_rc = _enter_idle_hold(pipeline, presenter, "pipeline-error")
                        if hold_rc == RC_RESUME_VIDEO:
                            close_presenter = False
                        return hold_rc
                    return 3
                if msg.type == Gst.MessageType.EOS:
                    log("pipeline EOS")
                    if presenter.opened and presenter._did_modeset:
                        hold_rc = _enter_idle_hold(pipeline, presenter, "eos")
                        if hold_rc == RC_RESUME_VIDEO:
                            close_presenter = False
                        return hold_rc
                    return 0 if n else 1
                if msg.type == Gst.MessageType.WARNING:
                    werr, wdbg = msg.parse_warning()
                    log(f"pipeline WARN from {msg.src.get_name()}: {werr.message}")
                msg = bus.pop_filtered(
                    Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.WARNING
                )
            sample = appsink.try_pull_sample(200 * Gst.MSECOND)
            if sample is None:
                if appsink.is_eos():
                    log("EOS")
                    if presenter.opened and presenter._did_modeset:
                        hold_rc = _enter_idle_hold(pipeline, presenter, "appsink-eos")
                        if hold_rc == RC_RESUME_VIDEO:
                            close_presenter = False
                        return hold_rc
                    break
                if stall_secs > 0 and (time.monotonic() - last_progress) > stall_secs:
                    log(f"STALL: no decoded frame for {stall_secs:.0f}s "
                        f"(frames={n}) — in-process idle splash (keep master)")
                    hold_rc = _enter_idle_hold(pipeline, presenter, "stall")
                    if hold_rc == RC_RESUME_VIDEO:
                        close_presenter = False
                    return hold_rc
                if presenter.opened:
                    presenter._drain_flip(block=False)
                continue
            last_progress = time.monotonic()
            mapped = _map_sample(sample)
            if not mapped or mapped[0] is None:
                continue
            data, size, mem, info = mapped
            try:
                # First frame of a *new* DRM session (external splash still up):
                # 1) open+setup (master may still be held by idle_splash)
                # 2) copy frame out of GStreamer
                # 3) log NEED_MASTER → endpoint DropMaster's the splash
                # 4) spin SetMaster + alloc + modeset — gap is ~one 50ms tick
                if presenter.layout is None and presenter._idle is None:
                    if not presenter.opened:
                        try:
                            presenter.open()
                            presenter.setup()
                        except Exception as e:
                            log(f"DRM open/setup failed at first frame: {e} — exit for respawn")
                            mem.unmap(info)
                            return 4
                    w, h = _sample_dims(sample)
                    lay = _build_layout(sample, w, h, size)
                    log(f"first frame {lay!r}")
                    # Copy out before unmap so we can hand off master without holding the map.
                    frame = bytes(data[:size])
                    mem.unmap(info)
                    # Ask endpoint to release external idle_splash *now*.
                    log("NEED_MASTER")
                    if not presenter.ensure_master(timeout=4.0):
                        log("ensure_master failed after NEED_MASTER — exit for respawn")
                        return 4
                    try:
                        presenter._alloc_bufs(lay)
                        presenter.present(frame, size)
                    except OSError as e:
                        log(f"first present after handoff failed: {e}")
                        return 5
                    modeset_fails = 0
                    n += 1
                    now = time.monotonic()
                    last_log = now
                    log(f"frames={n} ~handoff ok front={presenter._front}")
                    continue
                # Resume path: layout exists, idle FB on screen — present() swaps to YU12.
                if presenter.layout is None:
                    w, h = _sample_dims(sample)
                    lay = _build_layout(sample, w, h, size)
                    log(f"first frame (resume alloc) {lay!r}")
                    presenter._alloc_bufs(lay)
                if dump_dir and dumped < 6 and n % 90 == 0:
                    try:
                        with open(os.path.join(dump_dir, f"dec_{n:05d}.yuv"), "wb") as f:
                            f.write(bytes(data[:size]))
                        dumped += 1
                    except OSError:
                        pass
                presenter.present(data, size)
                modeset_fails = 0
            except OSError as e:
                log(f"present error: {e}")
                if not presenter._did_modeset or presenter._idle is not None:
                    modeset_fails += 1
                    if modeset_fails > 40:
                        log("modeset kept failing (DRM master unavailable?) "
                            "— exit for endpoint respawn")
                        try:
                            mem.unmap(info)
                        except Exception:
                            pass
                        return 5
            finally:
                try:
                    mem.unmap(info)
                except Exception:
                    pass
            n += 1
            now = time.monotonic()
            if n == 1 or now - last_log >= 5.0:
                dt = now - t0
                log(f"frames={n} ~{n / dt:.1f} fps front={presenter._front} "
                    f"flip_pending={presenter._flip_pending}")
                last_log = now
        rc = 0 if n else 1
    finally:
        try:
            pipeline.set_state(Gst.State.NULL)
        except Exception:
            pass
        if close_presenter:
            presenter.close()
    dt = time.monotonic() - t0
    log(f"present done: {n} frames in {dt:.1f}s = {n / dt if dt else 0:.1f} fps")
    return rc


# --- offline ES feeder (level-patch + real-time pacing) ---------------------


def feed_es_file(path: str, wfd: int, level_patch: bool, pace_fps: float) -> None:
    """Thread: feed a raw ES file into the pipeline write end, optionally paced.

    Level-patches L5.2->L4.2 (constant per session). pace_fps>0 throttles to ~that
    many access units/sec (no `pv` on the Pi); <=0 feeds as fast as the pipe takes.
    """
    import threading

    def _run():
        try:
            with open(path, "rb") as f:
                data = f.read()
            if level_patch:
                data, _ = ddm.patch_h264_level(data)
            if pace_fps <= 0:
                os.write(wfd, data)
            else:
                interval = 1.0 / pace_fps
                idx = _split_access_units(data)
                if len(idx) < 2:
                    os.write(wfd, data)
                else:
                    next_t = time.monotonic()
                    bounds = idx + [len(data)]
                    for a, b in zip(bounds, bounds[1:]):
                        os.write(wfd, data[a:b])
                        next_t += interval
                        s = next_t - time.monotonic()
                        if s > 0:
                            time.sleep(s)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                os.close(wfd)
            except OSError:
                pass

    threading.Thread(target=_run, name="es-feed", daemon=True).start()


def _split_access_units(data: bytes) -> list[int]:
    """Byte offsets of access-unit starts (AUD type 9, else SPS type 7)."""
    starts: list[int] = []
    i, n = 0, len(data)
    while True:
        j = data.find(b"\x00\x00\x01", i)
        if j < 0:
            break
        nal = data[j + 3] & 0x1F if j + 3 < n else 0
        if nal in (9, 7):
            s = j - 1 if (j >= 1 and data[j - 1] == 0) else j
            if not starts or s > starts[-1]:
                starts.append(s)
        i = j + 3
    return starts


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default=os.environ.get("DRM_DEVICE", "/dev/dri/card1"))
    ap.add_argument("--connector", default=os.environ.get("DRM_CONNECTOR", "HDMI-A-2"),
                    help="HDMI-A-N, comma list (HDMI-A-1,HDMI-A-2), or 'auto' = all usable")
    ap.add_argument("--es-file", default=None, help="read H.264 ES from PATH (offline)")
    ap.add_argument("--probe", action="store_true",
                    help="decode + report layout/fps; NO DRM, NO monitor")
    ap.add_argument("--probe-frames", type=int, default=300)
    ap.add_argument("--pace-fps", type=float,
                    default=float(os.environ.get("O3_ATOMIC_PACE_FPS", "0") or 0),
                    help="offline: throttle ES feed to ~N AUs/s (0 = as fast as possible)")
    ap.add_argument("--level-patch", dest="level_patch", action="store_true", default=True)
    ap.add_argument("--no-level-patch", dest="level_patch", action="store_false")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    args = ap.parse_args()

    conn_raw = args.connector.strip()
    if conn_raw.lower() in ("", "auto", "any", "*"):
        connector: str | None = None  # all usable HDMI-A
    else:
        connector = conn_raw  # single name or comma list

    # Debug: simulate slow cold-boot startup (gi import from cold disk cache ~3s) to
    # reproduce the startup back-pressure livelock. 0 = off.
    _sd = _env_float("O3_ATOMIC_STARTUP_DELAY", 0.0)
    if _sd > 0:
        log(f"debug startup delay {_sd:.1f}s")
        time.sleep(_sd)

    def _sig_term(_s, _f):
        # Cooperative stop: idle-hold loop exits, finally DropMaster. Avoid sys.exit
        # from the handler (that skipped cleanup and raced the external splash).
        global _STOP
        _STOP = True

    def _sig_idle(_s, _f):
        # Endpoint wants seamless splash now (drone power-cycle / stream restart).
        global _IDLE_SPLASH_REQ
        _IDLE_SPLASH_REQ = True

    def _sig_resume(_s, _f):
        # Endpoint has a fresh SPS — rebuild decoder, keep DRM master (splash→video).
        global _RESUME_REQ
        _RESUME_REQ = True

    signal.signal(signal.SIGINT, _sig_term)
    signal.signal(signal.SIGTERM, _sig_term)
    signal.signal(signal.SIGUSR1, _sig_idle)
    signal.signal(signal.SIGUSR2, _sig_resume)

    if args.probe:
        if args.es_file:
            rfd, wfd = os.pipe()
            pipeline, appsink = build_pipeline(rfd, args.width, args.height)
            feed_es_file(args.es_file, wfd, args.level_patch, args.pace_fps)
        else:
            pipeline, appsink = build_pipeline(0, args.width, args.height)
        return run_probe(appsink, pipeline, args.probe_frames)

    # Live: on boot, take master and paint splash immediately so USB plug does
    # not wait on GStreamer import (idle-first). External idle_splash may still
    # hold master until we emit NEED_MASTER. On drone re-power / first SPS we
    # loop with RC_RESUME_VIDEO and never DropMaster (in-process splash stays).
    presenter = AtomicPresenter(args.device, connector, args.width, args.height)
    rc = 0
    if (not args.es_file) and _idle_first_enabled():
        rc = _boot_idle_first(presenter)
        if rc != RC_RESUME_VIDEO:
            if presenter.fd >= 0:
                presenter.close()
            return rc
    while not _STOP:
        if args.es_file:
            rfd, wfd = os.pipe()
            pipeline, appsink = build_pipeline(rfd, args.width, args.height)
            feed_es_file(args.es_file, wfd, args.level_patch, args.pace_fps)
        else:
            pipeline, appsink = build_pipeline(0, args.width, args.height)
        rc = run_present(appsink, pipeline, presenter)
        if rc == RC_RESUME_VIDEO:
            log("pipeline rebuild for resume (splash FB still scanning out)")
            continue
        break
    # If run_present already closed on non-resume paths, close() is idempotent enough
    # (fd may be -1). Resume path left presenter open until final exit.
    if presenter.fd >= 0:
        presenter.close()
    return rc


if __name__ == "__main__":
    sys.exit(main() or 0)
