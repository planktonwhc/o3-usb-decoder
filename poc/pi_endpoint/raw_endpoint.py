#!/usr/bin/env python3
"""
Phase B accessory emulator via raw-gadget (works when FunctionFS is unavailable).

  sudo python3 poc/pi_endpoint/raw_endpoint.py --out /tmp/o3live.h264 -v
  # plug goggles into Pi USB-C (VBUS isolated)

Emulates the phone side of the goggles' AOA link so the goggles converse + stream:
  1. Start in NORMAL mode (PID_NORMAL). The goggles, as AOA USB host, drive
     GET_PROTOCOL(0x33) → SEND_STRING(0x34)×6 → ACCESSORY_START(0x35) — exactly as they
     bring up the phone in the gold capture.
  2. On ACCESSORY_START, re-enumerate as the accessory (18d1:2d01) with bulk 0x01 OUT +
     0x81 IN. (Presenting 2d01 cold — old behavior, --cold-2d01 — got the goggles to
     enumerate but they suspended without sending bulk: they only converse with an
     accessory they themselves brought up. That was the standalone-video blocker.)
  3. Answer DUML identity probes (cs=0 / 0x81 / 0x82 / 0x88) with the exact bytes DJI Fly
     sent in the gold capture; deframe 55 CC 4A 57 video → H.264 ES.
"""
from __future__ import annotations

import argparse
import errno
import os
import select
import signal
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

# Prefer analysis/ over this directory so a stale local video_deframe.py on the Pi
# (left by older deploys; rsync without --delete) cannot shadow the real module.
# __file__ = .../o3test/poc/pi_endpoint/raw_endpoint.py
# parents[0]=pi_endpoint, [1]=poc, [2]=o3test
_FILE = Path(__file__).resolve()
_HERE = _FILE.parent
_ANALYSIS = _FILE.parents[2] / "analysis"
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ANALYSIS))  # inserted last → searched first

from video_deframe import StreamingVideoDeframer, _find_sps  # noqa: E402
from aoa_transport import (  # noqa: E402
    AOA_MAGIC,
    VIDEO_MAGIC,
    aoa_wrap,
    aoa_unwrap_stream,
    maybe_handle_duml,
)
from raw_gadget import (  # noqa: E402
    RawGadget,
    parse_ctrl,
    device_descriptor,
    config_descriptor,
    string_descriptor,
    qualifier_descriptor,
    USB_RAW_EVENT_CONNECT,
    USB_RAW_EVENT_CONTROL,
    USB_RAW_EVENT_SUSPEND,
    USB_RAW_EVENT_RESUME,
    USB_RAW_EVENT_RESET,
    USB_RAW_EVENT_DISCONNECT,
    USB_DIR_IN,
    USB_TYPE_MASK,
    USB_TYPE_STANDARD,
    USB_TYPE_VENDOR,
    USB_REQ_GET_DESCRIPTOR,
    USB_REQ_SET_CONFIGURATION,
    USB_REQ_GET_CONFIGURATION,
    USB_REQ_SET_INTERFACE,
    USB_REQ_GET_INTERFACE,
    USB_REQ_SET_ADDRESS,
    USB_REQ_GET_STATUS,
    USB_DT_DEVICE,
    USB_DT_CONFIG,
    USB_DT_STRING,
    USB_DT_DEVICE_QUALIFIER,
    USB_DT_OTHER_SPEED_CONFIG,
    USB_DT_BOS,
    AOA_GET_PROTOCOL,
    AOA_SEND_STRING,
    AOA_START,
    AOA_PROTOCOL_VERSION,
    PID_NORMAL,
    PID_ACCESSORY,
)

STRINGS = {
    1: "DJI",
    2: "com.dji.logiclink",
    3: "0123456789ABCDEF",
}


def _drm_card_name(drm_device: str) -> str:
    """`/dev/dri/card1` → `card1`."""
    return os.path.basename(drm_device.rstrip("/"))


def _connector_edid_len(sysfs_path: str) -> int:
    """Bytes of EDID blob (0 if missing / unreadable). Real monitors are ≥128."""
    try:
        with open(os.path.join(sysfs_path, "edid"), "rb") as f:
            return len(f.read())
    except OSError:
        return 0


def _connector_has_modes(sysfs_path: str) -> bool:
    try:
        with open(os.path.join(sysfs_path, "modes"), encoding="utf-8") as f:
            return bool(f.read().strip())
    except OSError:
        return False


def connector_is_usable(c: dict) -> bool:
    """True if the port looks like a real monitor (not a phantom 'connected' stub).

    Field gotcha (Pi 4, 2026-07-13):
      - HDMI can report status=connected with **0-byte EDID** and no modes.
      - Worse: unplugged / half-attached ports often keep **VESA fallback modes**
        (1024×768, 800×600, …) with still-empty EDID. Treating those as usable
        forced dual_drm_mirror (openh264 software) even with one real monitor →
        lag / choppy / slow-mo on the live path.

    Auto mode requires a real EDID blob (≥128 B). Modes alone are not enough.
    Explicit connector list still allows override in resolve_hdmi_targets().
    """
    if c.get("status") != "connected":
        return False
    # Real monitors always expose at least one 128-byte EDID block.
    return int(c.get("edid_len") or 0) >= 128


def list_hdmi_connectors(drm_device: str) -> list[dict]:
    """Enumerate HDMI connectors for a DRM card via sysfs.

    Returns dicts: {name, id, status, sysfs, edid_len, has_modes} where name is
    e.g. `HDMI-A-1` and id is the numeric DRM connector object id (kmssink).
    """
    card = _drm_card_name(drm_device)
    base = "/sys/class/drm"
    out: list[dict] = []
    if not os.path.isdir(base):
        return out
    prefix = card + "-HDMI-"
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for ent in names:
        if not ent.startswith(prefix):
            continue
        path = os.path.join(base, ent)
        conn_name = ent[len(card) + 1 :]  # HDMI-A-1
        try:
            status = open(os.path.join(path, "status"), encoding="utf-8").read().strip()
        except OSError:
            continue
        cid = None
        try:
            cid = int(open(os.path.join(path, "connector_id"), encoding="utf-8").read().strip())
        except (OSError, ValueError):
            pass
        edid_len = _connector_edid_len(path)
        has_modes = _connector_has_modes(path)
        out.append({
            "name": conn_name,
            "id": cid,
            "status": status,
            "sysfs": path,
            "edid_len": edid_len,
            "has_modes": has_modes,
        })
    return out


# Last phantom-connector description we logged (avoid 2s hotplug spam).
_last_phantom_log: str | None = None


def resolve_hdmi_targets(drm_device: str, connector_spec: str,
                         *, require_usable: bool = True) -> list[dict]:
    """Pick target connectors from a spec.

    connector_spec:
      - `auto` — every *connected + usable* HDMI-A-* on the card. Usable means
        real EDID ≥128 B (0-byte EDID + VESA fallback modes = phantom, ignored).
      - `HDMI-A-1` or `HDMI-A-1,HDMI-A-2` — explicit list; still only starts when
        status=connected. Explicit picks may force a flaky EDID port.
    """
    global _last_phantom_log
    all_c = list_hdmi_connectors(drm_device)
    by_name = {c["name"]: c for c in all_c}
    spec = (connector_spec or "auto").strip()
    if spec.lower() in ("auto", "*", "all"):
        connected = [c for c in all_c if c["status"] == "connected"]
        if not require_usable:
            return connected
        usable = [c for c in connected if connector_is_usable(c)]
        phantoms = [c for c in connected if not connector_is_usable(c)]
        if phantoms:
            desc = ", ".join(
                f"{c['name']}(edid={c.get('edid_len', 0)}B,modes={c.get('has_modes')})"
                for c in phantoms
            )
            # Log once per set change — hotplug polls every 2s.
            if desc != _last_phantom_log:
                print(f"[*] HDMI: ignoring phantom connector(s): {desc}", flush=True)
                print(
                    "[*] (phantom = connected but no real EDID — unplug/replug that "
                    "HDMI cable, or use the port that reports edid≥128B)",
                    flush=True,
                )
                _last_phantom_log = desc
        else:
            _last_phantom_log = None
        return usable
    # Explicit list: honour the name even if EDID is empty (field override). Still
    # requires status=connected at spawn time; hotplug will pick it up later.
    wanted = [p.strip() for p in spec.split(",") if p.strip()]
    out = []
    for name in wanted:
        c = by_name.get(name)
        if c is None:
            print(f"[!] DRM connector {name} not found on {drm_device} "
                  f"(have: {', '.join(by_name) or 'none'})", flush=True)
            continue
        if c["status"] != "connected":
            continue
        if not connector_is_usable(c):
            # Rate-limit: hotplug polls ~2s; do not spam journal every tick.
            desc = f"{name}(edid={c.get('edid_len', 0)}B)"
            if desc != _last_phantom_log:
                print(f"[!] DRM connector {name} connected but looks phantom "
                      f"(edid={c.get('edid_len', 0)}B, modes={c.get('has_modes')}) "
                      f"— still using (explicit override)",
                      flush=True)
                _last_phantom_log = desc
        out.append(c)
    return out


def _udc_state(device: str = "fe980000.usb") -> str:
    """Read UDC link state. NOTE: 'configured' does NOT mean a host is present —
    after goggles unplug, dwc2/raw-gadget often leaves this stuck until reinit."""
    path = f"/sys/class/udc/{device}/state"
    try:
        return open(path, encoding="utf-8").read().strip()
    except OSError:
        return "?"


def selected_player() -> str:
    """Which single-HDMI player the field wants: 'atomic' (default) | 'mpv' | 'gst'.

    - atomic  : atomic_drm_present.py — SW decode (avdec_h264) → YU12 dumb buffer →
                page-flip, modeset ONCE. The committed field default and owner
                directive ("never mpv again" — memory o3-never-mpv-atomic-only).
    - mpv     : zero-copy DRM, modesets once. Retired fallback (debug only).
    - gst     : v4l2h264dec ! kmssink (present-per-frame, but kmssink re-modesets
                every spawn — crash-looped + killed the Atomos in the field; debug).
    """
    v = os.environ.get("O3_HDMI_PLAYER", "atomic").strip().lower()
    if v in ("mpv",):
        return "mpv"
    if v in ("gst", "gstreamer", "live"):
        return "gst"
    return "atomic"


def prefer_mpv_player() -> bool:
    """True only when mpv is explicitly opted into. See selected_player() for kinds.

    2026-07-13 field: gst started on empty stdin stayed prerolling forever (fbcon kept
    the plane → console only) even while ES flowed. mpv at least modesets and shows
    video; demuxer-max-bytes integer cap limits multi-minute lag. Retired in favour of
    the atomic presenter (2026-07-14 owner directive) — kept as debug-only fallback.
    """
    return selected_player() == "mpv"


def patch_h264_level(es: bytes, level_idc: int = 42) -> tuple[bytes, bool]:
    """Rewrite SPS level_idc in Annex-B ES (same as dual_drm_mirror).

    O3 live tags High@L5.2; Pi bcm2835 v4l2 only advertises levels ≤5.1 (menu max
    5.1). 1080p60 only needs L4.2. Patching avoids borderline HW decode that can
    hitch under load. Returns (data, saw_sps).
    """
    if not es or len(es) < 8:
        return es, False
    patterns = (
        b"\x00\x00\x00\x01\x67",
        b"\x00\x00\x01\x67",
        b"\x00\x00\x00\x01\x27",
        b"\x00\x00\x01\x27",
    )
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


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw, 0)
    except ValueError:
        return default


def parse_sps_vui_fps(es: bytes) -> float | None:
    """Return fps from first H.264 SPS VUI timing_info, or None.

    Formula: fps = time_scale / (2 * num_units_in_tick) when present.
    O3 gold SPS advertised 60 with fixed_frame_rate_flag=0; field AU rate was
    ~50 — treat VUI as a hint, not absolute truth. Prefer measured AU feed.
    """
    if not es or len(es) < 16:
        return None
    # Locate SPS NAL (type 7).
    sps_nal: bytes | None = None
    for pat, sc in (
        (b"\x00\x00\x00\x01\x67", 5),
        (b"\x00\x00\x01\x67", 4),
        (b"\x00\x00\x00\x01\x27", 5),
        (b"\x00\x00\x01\x27", 4),
    ):
        j = es.find(pat)
        if j < 0:
            continue
        start = j + sc - 1  # include NAL header byte
        # End at next start code.
        rest = es[start + 1 :]
        k4 = rest.find(b"\x00\x00\x00\x01")
        k3 = rest.find(b"\x00\x00\x01")
        end_rel = len(rest)
        if k4 >= 0:
            end_rel = min(end_rel, k4)
        if k3 >= 0:
            end_rel = min(end_rel, k3)
        sps_nal = es[start : start + 1 + end_rel]
        break
    if not sps_nal or len(sps_nal) < 8:
        return None

    # EBSP → RBSP (remove 0x000003 emulation prevention).
    rbsp = bytearray()
    i = 1  # skip NAL header
    while i < len(sps_nal):
        if (
            i + 2 < len(sps_nal)
            and sps_nal[i] == 0
            and sps_nal[i + 1] == 0
            and sps_nal[i + 2] == 3
        ):
            rbsp.extend(sps_nal[i : i + 2])
            i += 3
        else:
            rbsp.append(sps_nal[i])
            i += 1

    class _BR:
        def __init__(self, b: bytes):
            self.b = b
            self.i = 0
            self.bit = 0

        def u(self, n: int) -> int:
            v = 0
            for _ in range(n):
                if self.i >= len(self.b):
                    return 0
                v = (v << 1) | ((self.b[self.i] >> (7 - self.bit)) & 1)
                self.bit += 1
                if self.bit == 8:
                    self.bit = 0
                    self.i += 1
            return v

        def ue(self) -> int:
            z = 0
            while self.u(1) == 0 and z < 32:
                z += 1
            return ((1 << z) - 1 + self.u(z)) if z else 0

        def se(self) -> int:
            v = self.ue()
            return ((-1) ** (v + 1)) * ((v + 1) // 2)

    try:
        br = _BR(bytes(rbsp))
        profile_idc = br.u(8)
        br.u(8)  # constraint flags
        br.u(8)  # level_idc
        br.ue()  # sps id
        if profile_idc in (
            100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135,
        ):
            chroma = br.ue()
            if chroma == 3:
                br.u(1)
            br.ue()
            br.ue()
            br.u(1)
            if br.u(1):  # scaling matrix present — skip best-effort
                n = 12 if chroma == 3 else 8
                for idx in range(n):
                    if not br.u(1):
                        continue
                    last = 8
                    nextv = 8
                    size = 16 if idx < 6 else 64
                    for _ in range(size):
                        if nextv != 0:
                            delta = br.se()
                            nextv = (last + delta + 256) % 256
                        last = nextv if nextv else last
        br.ue()  # log2_max_frame_num_minus4
        poc = br.ue()
        if poc == 0:
            br.ue()
        elif poc == 1:
            br.u(1)
            br.se()
            br.se()
            for _ in range(br.ue()):
                br.se()
        br.ue()  # max_num_ref_frames
        br.u(1)  # gaps
        br.ue()
        br.ue()  # pic size in mbs
        frame_mbs = br.u(1)
        if not frame_mbs:
            br.u(1)
        br.u(1)
        if br.u(1):
            br.ue()
            br.ue()
            br.ue()
            br.ue()
        if not br.u(1):  # vui_parameters_present
            return None
        if br.u(1):  # aspect_ratio
            af = br.u(8)
            if af == 255:
                br.u(32)
        if br.u(1):  # overscan
            br.u(1)
        if br.u(1):  # video_signal
            br.u(3)
            br.u(1)
            if br.u(1):
                br.u(24)
        if br.u(1):  # chroma_loc
            br.ue()
            br.ue()
        if not br.u(1):  # timing_info_present
            return None
        num_units = br.u(32)
        time_scale = br.u(32)
        # fixed_frame_rate_flag = br.u(1)  # often 0 on O3
        if num_units <= 0 or time_scale <= 0:
            return None
        fps = time_scale / (2.0 * num_units)
        if 10.0 <= fps <= 240.0:
            return fps
    except Exception:
        return None
    return None


def default_player_cmd(
    drm_device: str,
    drm_connector: str,
    container_fps: float | None = None,
) -> list[str]:
    """mpv zero-copy live path (field default).

    ZERO-COPY: --vo=gpu --gpu-context=drm --hwdec=v4l2m2m.
    demuxer-max-bytes MUST be integer bytes (mpv 0.40 default 150MiB = minutes lag).
    No GLSL quality stack here — keeps VO light for live FPV.
    Never --untimed + desync + lavf nobuffer together.
    Never --video-sync=display-desync (field: slow-mo + lag stacking).
    Never --video-sync=display-vdrop here either — field 2026-07-13 evening saw
    mpv exit rc=2 crash-loop + orphan DRM masters → “really bad” broken video.

    Stream is raw Annex-B H.264 without PTS; lavf invents 25 fps unless we force
    the real rate + --no-correct-pts.

    Content rate is **adaptive** (goggles can change FPS mid-session):
      1. Explicit O3_MPV_CONTAINER_FPS env (if set) wins.
      2. Else measured AU feed rate (field truth — was ~50 while SPS VUI said 60).
      3. Else SPS VUI timing_info.
      4. Else 50.
    Display override tracks the panel (default 60). Content at 100–120 on a 60 Hz
    panel is still *live* via framedrop=vo (drops surplus frames); seeing full
    100–120 needs a high-refresh monitor + working DRM mode.

    Shallow jitter cushion only — never multi-second lag. Env knobs below.
    """
    # Tight live cushion: enough for USB micro-jitter, not a delay line.
    cache_s = _env_float("O3_MPV_CACHE_SECS", 0.12)
    cache_s = max(0.06, min(cache_s, 0.28))  # hard clamp — never bank multi-second lag
    # Scale demuxer byte cap lightly with fps (more frames/s → more bytes/s).
    c_fps = float(container_fps) if container_fps is not None else _env_float(
        "O3_MPV_CONTAINER_FPS", 50.0
    )
    c_fps = max(20.0, min(c_fps, 120.0))
    # ~0.8–1.0s of ES at ~6–10 Mbit: base 768KiB @50fps, scale with rate.
    default_bytes = int(786432 * max(1.0, c_fps / 50.0))
    default_bytes = min(default_bytes, 1572864)  # hard cap 1.5 MiB
    max_bytes = _env_int("O3_MPV_MAX_BYTES", default_bytes)
    max_bytes = max(262144, min(max_bytes, 2097152))  # 0.25–2 MiB
    hw_extra = _env_int("O3_MPV_HWDEC_EXTRA_FRAMES", 5)
    hw_extra = max(2, min(hw_extra, 12))
    # vo drop keeps output live when content fps > display or decode lags.
    framedrop = os.environ.get("O3_MPV_FRAMEDROP", "vo").strip().lower() or "vo"
    if framedrop not in ("no", "vo", "decoder", "decoder+vo"):
        framedrop = "vo"
    display_fps = _env_float("O3_MPV_DISPLAY_FPS", 60.0)
    display_fps = max(30.0, min(display_fps, 240.0))
    ipc = os.environ.get("O3_MPV_IPC", "/tmp/o3-mpv.sock").strip() or "/tmp/o3-mpv.sock"
    latency_hacks = os.environ.get("O3_MPV_LATENCY_HACKS", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )
    # Optional; empty = mpv default (audio). Banned: display-desync, display-vdrop.
    video_sync = os.environ.get("O3_MPV_VIDEO_SYNC", "").strip().lower()
    banned_sync = {"display-desync", "display-vdrop", "desync"}
    if video_sync in banned_sync:
        print(f"[!] O3_MPV_VIDEO_SYNC={video_sync} banned (field crash/lag); ignoring",
              flush=True)
        video_sync = ""
    c_fps_s = f"{c_fps:.3f}".rstrip("0").rstrip(".")
    d_fps_s = f"{display_fps:.3f}".rstrip("0").rstrip(".")
    cmd = [
        "mpv",
        "--no-config",
        "--really-quiet",
        "--audio=no",
        f"--drm-device={drm_device}",
        f"--drm-connector={drm_connector}",
        # Prefer progressive 1080p60 panel mode (EDID may list 4K first).
        "--drm-mode=1920x1080",
        "--vo=gpu",
        "--gpu-context=drm",
        "--hwdec=v4l2m2m",
        f"--hwdec-extra-frames={hw_extra}",
        "--cache=yes",
        "--cache-pause=no",
        # Soft readahead absorbs USB micro-jitter; hard byte cap prevents lag stack.
        f"--demuxer-readahead-secs={cache_s:.3f}",
        f"--cache-secs={cache_s:.3f}",
        f"--demuxer-max-bytes={max_bytes}",
        "--demuxer-max-back-bytes=0",
        # PTS-less Annex-B: stamp frames at measured/content rate.
        "--no-correct-pts",
        f"--container-fps-override={c_fps_s}",
        f"--display-fps-override={d_fps_s}",
        "--interpolation=no",
        f"--framedrop={framedrop}",
        f"--input-ipc-server={ipc}",
        "--demuxer=lavf",
        "--demuxer-lavf-format=h264",
        "--demuxer-lavf-probesize=32768",
        "--demuxer-lavf-analyzeduration=0",
        "-",
    ]
    if latency_hacks:
        # Insert before trailing "-" stdin.
        cmd.insert(-1, "--video-latency-hacks=yes")
    if video_sync:
        cmd.insert(-1, f"--video-sync={video_sync}")
    return cmd


def _mpv_ipc_get(sock_path: str, property_name: str, timeout: float = 0.25):
    """One-shot mpv JSON IPC get_property. Returns data or None."""
    import json
    import socket

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(sock_path)
        req = json.dumps({"command": ["get_property", property_name]}) + "\n"
        s.sendall(req.encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace")
        if not line:
            return None
        resp = json.loads(line)
        if resp.get("error") != "success":
            return None
        return resp.get("data")
    except (OSError, ValueError, json.JSONDecodeError, TimeoutError):
        return None


def default_gst_live_cmd(target: dict) -> list[str]:
    """Single HDMI live path: HW zero-copy decode → present each frame as it arrives.

    kmssink sync=false = display frames on arrival (no mpv-style PTS clock, no
    fps-override to guess, no slow-mo lag). This is the present-per-frame model:
    the goggles' own cadence paces the display, so there is nothing to drop or
    duplicate on a guessed clock (mpv's 60-vs-50 chop cannot occur here).

    HW ZERO-COPY (validated 2026-07-14 on this Pi, owner eyes-on "the REAL DEAL,
    smooth"): v4l2h264dec emits video/x-raw(memory:DMABuf), format=DMA_DRM
    (drm-format YU12); kmssink imports that DMABuf straight as a DRM framebuffer —
    no videoconvert, no CPU copy (~4% CPU vs software openh264dec pegging a core).
    Requires stream level ≤ 5.1: the endpoint's O3_H264_LEVEL_PATCH (L5.2→L4.2)
    already satisfies this — do NOT disable it on the gst path or v4l2h264dec will
    refuse to negotiate the native L5.2 stream.

    History: the OLD path forced software openh264dec ! videoconvert ! BGRx
    because decodebin→plain I420 (system memory) failed to modeset on this Pi
    ("failed to configure video mode"). The real fix was DMA_DRM DMABuf
    negotiation (newer GStreamer), not software decode.

    Leaky queue keeps ~1 MiB of freshest ES under back-pressure (drops oldest,
    never stalls the USB reader → kernel raw-gadget URB drops).
    """
    if target.get("id") is None:
        raise ValueError(f"connector {target.get('name')} has no connector_id in sysfs")
    return [
        "gst-launch-1.0",
        "-q",
        "fdsrc", "fd=0",
        "!", "queue",
        "max-size-buffers=8",
        "max-size-bytes=1048576",
        "max-size-time=0",
        "leaky=downstream",
        "!", "h264parse", "config-interval=-1",
        # HW decode → DMABuf (DMA_DRM/YU12), handed straight to the display plane.
        "!", "v4l2h264dec",
        "!", "kmssink",
        "driver-name=vc4",
        f"connector-id={target['id']}",
        "sync=false",
        "force-modesetting=true",
        "skip-vsync=true",
    ]


def _dual_drm_mirror_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "dual_drm_mirror.py")


def _atomic_present_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "atomic_drm_present.py")


def default_atomic_cmd(target: dict | list[dict], drm_device: str) -> list[str]:
    """Atomic-KMS present-per-frame path (O3_HDMI_PLAYER=atomic).

    Single or dual: one process, SW decode (avdec_h264) → YU12 dumb buffer pool →
    drmModePageFlip. Modeset ONCE per CRTC; dual uses the **same** FB on both when
    modes match (see atomic_drm_present.py Stage 2).

    `target` is one connector dict, or a list for multi (e.g. A-1+A-2).
    Stdin is the endpoint's already-level-patched H.264 ES.
    """
    if isinstance(target, dict):
        conn = target["name"]
    else:
        if not target:
            raise ValueError("default_atomic_cmd requires at least one target")
        conn = ",".join(t["name"] for t in target)
    return [
        sys.executable,
        _atomic_present_path(),
        f"--device={drm_device}",
        f"--connector={conn}",
    ]


def default_gst_cmd(targets: list[dict]) -> list[str]:
    """Legacy multi-HDMI path when player is NOT atomic (debug only).

    Field dual uses atomic multi (default_atomic_cmd), not this. Kept for:
      O3_DUAL_MODE=drm|mirror   → dual_drm_mirror
      O3_DUAL_MODE=fbdev        → shared /dev/fb0 (crop risk)
      O3_DUAL_MODE=kms          → broken dual kmssink
    """
    if not targets:
        raise ValueError("default_gst_cmd requires at least one target")
    mode = os.environ.get("O3_DUAL_MODE", "drm").strip().lower()
    if mode in ("kms", "kmssink", "tee"):
        return _default_gst_dual_kmssink(targets)
    if mode in ("fbdev", "fb"):
        return _default_gst_dual_fbdev()
    # Default legacy: scale-to-fit dual DRM mirror (stdin = H.264 ES).
    return _default_gst_dual_drm()


def _default_gst_dual_drm() -> list[str]:
    """One process: decode + dual CRTC scale-to-fit (see dual_drm_mirror.py)."""
    return [
        sys.executable,
        _dual_drm_mirror_path(),
        "--from-es",
        f"--device={os.environ.get('DRM_DEVICE', '/dev/dri/card1')}",
        "--width=1920",
        "--height=1080",
    ]


def _default_gst_dual_fbdev() -> list[str]:
    """One sink, both CRTCs via shared /dev/fb0 (vc4drmfb). Crop if modes differ."""
    return [
        "gst-launch-1.0",
        "-q",
        "fdsrc", "fd=0",
        "!", "queue",
        "max-size-buffers=8",
        "max-size-bytes=1048576",
        "max-size-time=0",
        "leaky=downstream",
        "!", "h264parse", "config-interval=-1",
        "!", "openh264dec",
        "!", "videoconvert", "n-threads=2",
        "!", "video/x-raw,format=RGB16",
        "!", "fbdevsink",
        "sync=false",
    ]


def _default_gst_dual_kmssink(targets: list[dict]) -> list[str]:
    """Legacy dual kmssink (known FAIL on this Pi — debug/research only)."""
    for t in targets:
        if t.get("id") is None:
            raise ValueError(f"connector {t.get('name')} has no connector_id in sysfs")
    cmd: list[str] = [
        "gst-launch-1.0",
        "-q",
        "fdsrc", "fd=0",
        "!", "queue",
        "max-size-buffers=8",
        "max-size-bytes=1048576",
        "max-size-time=0",
        "leaky=downstream",
        "!", "h264parse", "config-interval=-1",
        "!", "openh264dec",
        "!", "videoconvert", "n-threads=2",
        "!", "video/x-raw,format=BGRx",
        "!", "tee", "name=t",
    ]
    for t in targets:
        cmd += [
            "t.",
            "!", "queue",
            "max-size-buffers=2",
            "max-size-bytes=0",
            "max-size-time=0",
            "leaky=downstream",
            "!", "kmssink",
            "driver-name=vc4",
            f"connector-id={t['id']}",
            "sync=false",
            "force-modesetting=true",
            "skip-vsync=true",
        ]
    return cmd


def dual_hdmi_enabled() -> bool:
    """True when dual-HDMI path is allowed (atomic multi-CRTC page-flip).

    Requires O3_DUAL_HDMI=1 **and** DRM_CONNECTOR=auto (or HDMI-A-1,HDMI-A-2)
    so resolve_hdmi_targets() returns ≥2 usable ports. With the field pin
    DRM_CONNECTOR=HDMI-A-2, dual stays single even if O3_DUAL_HDMI=1.

    Shipping dual backend is atomic (same YU12 FB → both CRTCs). Legacy
    dual_drm_mirror only when O3_HDMI_PLAYER is not atomic.
    """
    return os.environ.get("O3_DUAL_HDMI", "").strip().lower() in ("1", "true", "yes", "on")


class HdmiPlayer:
    """Feeds the live H.264 ES to decoder subprocess(es) without ever blocking USB.

    Single usable HDMI  → atomic (field) / mpv / gst per O3_HDMI_PLAYER.
    Two+ usable HDMIs + O3_DUAL_HDMI=1 + atomic → dual-atomic page-flip both.
    Phantom connectors (status=connected, 0-byte EDID) are ignored in `auto`.


    `connector_spec="auto"` tracks every usable HDMI-A-* and restarts the player
    when the set changes (plug either port, or both, at any time).

    URB reader must never stall (kernel drops raw-gadget URBs → frame loss), so
    writes go through a bounded queue. On overflow we must NOT drop arbitrary
    mid-GOP chunks (field: unflyable bitty video vs DJI Fly). Instead: clear
    backlog and re-align to the next SPS.

    Smoothness polish (2026-07-14):
      - Patch SPS L5.2→L4.2 so v4l2m2m stays on the HW path (Pi max menu = 5.1).
      - Soft prebuffer a few complete frames before the first write so mpv never
        starts from an empty demuxer (USB burst → underrun hitch).
      - Coalesce Annex-B into AUD access units when possible for steadier feed.

    Chop fix (2026-07-14 night):
      - Count AUs fed/sec vs mpv estimated-vf-fps (IPC) to split feed vs display.
      - Adaptive content fps from au_feed; framedrop=vo; short queue/cache.
    """

    def __init__(
        self,
        drm_device: str,
        connector_spec: str = "auto",
        verbose: bool = False,
        max_chunks: int = 48,
        fixed_cmd: list[str] | None = None,
    ):
        self.drm_device = drm_device
        self.connector_spec = connector_spec
        self.verbose = verbose
        self.fixed_cmd = fixed_cmd  # --player-cmd override: ignore multi/auto logic
        self._q: deque[bytes] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._spawn_lock = threading.Lock()
        # Live FPV: keep the hand-off queue short. 512 AUs @ 50 fps ≈ 10s of banked
        # video (flight 2: back-pressure dumps + play-after-drone-off). Default 48
        # ≈ 1s @ 50 fps — enough jitter, not a DVR. Env: still passed via max_chunks.
        self._max = max_chunks
        self._stop = False
        self._dropped = 0
        self._drop_log_t = 0.0
        self._drop_total = 0
        # Brief warm-up only — long grace + huge queue was banking lag (field 2026-07-14).
        self._spawn_grace_until = 0.0
        self._grace_max = max(max_chunks * 2, 24)
        # Deep warm-up buffer (AUs) used only during a slow player's cold-start window
        # (the atomic presenter's GStreamer/PyGObject import is ~3-5s from cold disk
        # cache). 0 = use the normal short grace. Set per-spawn in _spawn.
        self._warmup_limit = 0
        self._proc: subprocess.Popen | None = None
        self._thr: threading.Thread | None = None
        self._err_thr: threading.Thread | None = None
        self._stats_thr: threading.Thread | None = None
        self._watch: threading.Thread | None = None
        self._cmd: list[str] | None = None
        self._targets_key: tuple | None = None
        # After multi fails, pin single until hotplug set changes.
        self._force_single: bool = False
        self._respawn_failures: int = 0
        self._last_spawn_mono: float = 0.0
        # Last stdin write error (diagnostic).
        self._last_write_err: str = ""
        # Idle splash (owner request 2026-07-16): large "waiting for live FPV feed"
        # instead of the Linux text console whenever there is no live video.
        # O3_IDLE_SPLASH=0 restores the old console-on-idle behaviour.
        _sp = os.environ.get("O3_IDLE_SPLASH", "1").strip().lower()
        self._idle_splash = _sp not in ("0", "false", "no", "off")
        self._idle_splash_text = (
            os.environ.get("O3_IDLE_SPLASH_TEXT", "waiting for live FPV feed").strip()
            or "waiting for live FPV feed"
        )
        self._splash_proc: subprocess.Popen | None = None
        self._splash_ready: bool = False  # True after stdout READY (modeset done)
        # Serialize feed→splash / boot splash so drain-stderr + hotplug can't double-spawn.
        self._idle_lock = threading.Lock()
        # After every (re)spawn, drop ES until an SPS so mid-GOP restarts don't
        # instantly kill mpv (field: crash loop ~1s → "really bad broken video").
        self._need_sps: bool = True
        self._sps_hold: bytearray = bytearray()
        self._multi_log_once: bool = False
        # Lazy start: do not spawn decoder until first ES feed. Starting gst/mpv on an
        # empty pipe left kmssink prerolling forever (console only) in field tests.
        self._ever_spawned: bool = False
        # Min seconds between player spawns (prevents DRM-master thrash / dual orphans).
        # Must exceed the atomic presenter's cold-start (GStreamer/PyGObject import ~3-5s)
        # so a spawn's orphan-reap can't kill a presenter still importing before it runs.
        self._min_spawn_gap: float = 5.0
        # Level-patch once per session (SPS level is constant). Disable: O3_H264_LEVEL_PATCH=0
        _lp = os.environ.get("O3_H264_LEVEL_PATCH", "1").strip().lower()
        self._level_patch = _lp not in ("0", "false", "no", "off")
        self._level_patched_once = False
        # Soft prebuffer: hold ~N complete frames before first stdin write after (re)spawn.
        # ~4 frames @60 ≈ 67ms cushion — kills startup/underrun hitch without deep lag.
        self._prebuffer_frames = max(0, min(_env_int("O3_HDMI_PREBUFFER_FRAMES", 4), 12))
        self._prebuffer_bytes = max(0, min(_env_int("O3_HDMI_PREBUFFER_BYTES", 48 * 1024), 256 * 1024))
        self._need_prebuffer = True
        self._pre_q: deque[bytes] = deque()
        self._pre_bytes = 0
        # AUD coalescer: assemble complete access units before queueing.
        self._au_buf = bytearray()
        self._au_coalesce = os.environ.get("O3_HDMI_AU_COALESCE", "1").strip().lower() not in (
            "0", "false", "no", "off",
        )
        # Player kind (set per-spawn): mpv gets IPC stats + fps-adapt; gst does not.
        self._player_is_mpv = True
        self._player_is_atomic = False
        # True while atomic is holding the plane with in-process idle splash (SIGUSR1
        # or stall). feed() resumes via SIGUSR2 on the next SPS (same process).
        self._player_holding_idle = False
        self._pending_idle_resume = False  # SPS seen; fire SIGUSR2 after enqueue
        # Presentation diagnostics (chop A/B 2026-07-14 night).
        self._ipc_path = os.environ.get("O3_MPV_IPC", "/tmp/o3-mpv.sock").strip() or "/tmp/o3-mpv.sock"
        self._au_fed_total = 0
        self._au_fed_window = 0
        self._au_fed_window_t = time.monotonic()
        self._au_fed_rate = 0.0  # AUs queued to player / sec (instant)
        self._mpv_stats: dict = {}
        self._mpv_stats_lock = threading.Lock()
        # Adaptive content FPS (goggles can switch 50/60/100/120…).
        # Priority: O3_MPV_CONTAINER_FPS env > measured AU rate > SPS VUI > 50.
        self._fps_env_lock = os.environ.get("O3_MPV_CONTAINER_FPS", "").strip() != ""
        self._container_fps = _env_float("O3_MPV_CONTAINER_FPS", 50.0)
        self._container_fps = max(20.0, min(self._container_fps, 120.0))
        self._sps_vui_fps: float | None = None
        self._fps_candidate: float | None = None
        self._fps_candidate_since = 0.0
        self._fps_adapt = os.environ.get("O3_MPV_FPS_ADAPT", "1").strip().lower() not in (
            "0", "false", "no", "off",
        )
        # Scale queue depth to ~1s of content at current fps (min 24, max 120).
        self._apply_fps_queue_depth()

    def start(self):
        """Arm hotplug watcher; decoder starts on first video feed (first SPS)."""
        self._stop = False
        targets = resolve_hdmi_targets(self.drm_device, self.connector_spec)
        self._targets_key = self._targets_fingerprint(targets)
        names = ", ".join(t["name"] for t in targets) or "(none yet)"
        print(f"[*] HDMI armed → {names} "
              f"(atomic idle-first pre-spawn unless O3_ATOMIC_IDLE_FIRST=0)",
              flush=True)
        # Boot / pre-video: own the plane with the idle message (not the text console).
        self._enter_idle_display(reason="boot")
        # Pre-start the atomic presenter now so USB-plug does not pay the 3–5s
        # GStreamer/PyGObject import. Child takes master (NEED_MASTER), paints
        # in-process splash, idle-holds until SIGUSR2 on the first SPS.
        idle_first = os.environ.get("O3_ATOMIC_IDLE_FIRST", "1").strip().lower() not in (
            "0", "false", "no", "off",
        )
        if idle_first and selected_player() == "atomic" and self.fixed_cmd is None:
            print("[*] HDMI: pre-starting atomic presenter (idle-first) — "
                  "plug-in skips GStreamer import", flush=True)
            self._spawn()
        if self.fixed_cmd is None and self._watch is None:
            self._watch = threading.Thread(target=self._hotplug_loop, name="hdmi-hotplug",
                                           daemon=True)
            self._watch.start()

    def _targets_fingerprint(self, targets: list[dict]) -> tuple:
        # Do NOT include edid_len — field monitors often flap 0↔256 B EDID while
        # status stays connected, which used to respawn mpv every few seconds.
        return tuple((t["name"], t.get("id"), t["status"]) for t in targets)

    @staticmethod
    def _best_single(targets: list[dict]) -> list[dict]:
        """Pick one connector for single-port field path.

        Prefer **HDMI-A-2** (far micro-HDMI) when present — that was the
        eyes-on “decent / excellent” port. Else largest EDID, then name.
        """
        if not targets:
            return []
        for t in targets:
            if t.get("name") == "HDMI-A-2":
                return [t]
        ranked = sorted(
            targets,
            key=lambda t: (int(t.get("edid_len") or 0), t.get("has_modes", False), t["name"]),
            reverse=True,
        )
        return [ranked[0]]

    def _build_cmd(self, targets: list[dict]) -> list[str] | None:
        if self.fixed_cmd is not None:
            return list(self.fixed_cmd)
        if not targets:
            return None
        use = list(targets)
        if self._force_single and len(use) > 1:
            use = self._best_single(use)
        # Dual off: multi-port → best single (prefer A-2). Dual on needs
        # DRM_CONNECTOR=auto (or explicit A-1,A-2) so `use` has ≥2 targets.
        if len(use) > 1 and not dual_hdmi_enabled():
            best = self._best_single(use)
            if not self._multi_log_once:
                names = ", ".join(t["name"] for t in use)
                print(
                    f"[*] multi-HDMI ({names}): using single on {best[0]['name']} only "
                    f"(set O3_DUAL_HDMI=1 and DRM_CONNECTOR=auto for dual-atomic both)",
                    flush=True,
                )
                self._multi_log_once = True
            use = best
        if len(use) == 1:
            if dual_hdmi_enabled() and not self._multi_log_once:
                print(
                    f"[!] O3_DUAL_HDMI=1 but only 1 usable target "
                    f"({use[0]['name']}, spec={self.connector_spec!r}) — "
                    f"set DRM_CONNECTOR=auto (or HDMI-A-1,HDMI-A-2) for dual",
                    flush=True,
                )
                self._multi_log_once = True
            return self._single_player_cmd(use[0])
        # Multi + dual enabled.
        kind = selected_player()
        if kind == "atomic":
            names = ", ".join(t["name"] for t in use)
            if not self._multi_log_once:
                print(f"[*] multi-HDMI dual-atomic page-flip → {names}", flush=True)
                self._multi_log_once = True
            return default_atomic_cmd(use, self.drm_device)
        # Legacy non-atomic dual (dual_drm / fbdev / kmssink) — debug only.
        if any(t.get("id") is None for t in use):
            print("[!] multi-HDMI needs sysfs connector_id; falling back to best single",
                  flush=True)
            use = self._best_single(use)
            return self._single_player_cmd(use[0])
        return default_gst_cmd(use)

    def _single_player_cmd(self, t: dict) -> list[str]:
        """Build the single-HDMI player command for the selected player kind."""
        kind = selected_player()
        if kind == "atomic":
            return default_atomic_cmd(t, self.drm_device)
        if kind == "gst":
            try:
                return default_gst_live_cmd(t)
            except ValueError as e:
                print(f"[!] gst live path unavailable ({e}); falling back to mpv", flush=True)
                return default_player_cmd(
                    self.drm_device, t["name"], container_fps=self._container_fps,
                )
        return default_player_cmd(
            self.drm_device, t["name"], container_fps=self._container_fps,
        )

    def _spawn(self):
        """Kill any existing player and start for the current connector set."""
        with self._spawn_lock:
            now = time.monotonic()
            if self._last_spawn_mono and (now - self._last_spawn_mono) < self._min_spawn_gap:
                # Too soon — leave dead state; hotplug/feed will retry after gap.
                return
            self._kill_proc(who="spawn")
            with self._wake:
                self._q.clear()
            # Always re-align to next SPS after a new decoder process.
            self._need_sps = True
            self._sps_hold = bytearray()
            self._need_prebuffer = True
            self._pre_q.clear()
            self._pre_bytes = 0
            self._au_buf = bytearray()
            # Re-apply level patch on the next SPS we see after respawn.
            self._level_patched_once = False
            if self.fixed_cmd is not None:
                targets: list[dict] = []
                cmd = list(self.fixed_cmd)
                label = "fixed-cmd"
                active_targets: list[dict] = []
            else:
                targets = resolve_hdmi_targets(self.drm_device, self.connector_spec)
                self._targets_key = self._targets_fingerprint(targets)
                cmd = self._build_cmd(targets)
                if cmd is None:
                    print(f"[*] HDMI: no usable connectors on {self.drm_device} "
                          f"(spec={self.connector_spec!r}) — waiting for hotplug", flush=True)
                    # Help field debug: dump what sysfs thinks is there.
                    all_c = list_hdmi_connectors(self.drm_device)
                    if all_c:
                        for c in all_c:
                            print(
                                f"    {c['name']}: status={c['status']} "
                                f"edid={c.get('edid_len', 0)}B "
                                f"modes={c.get('has_modes')} id={c.get('id')}",
                                flush=True,
                            )
                    else:
                        print(f"    (no HDMI-* nodes under {_drm_card_name(self.drm_device)})",
                              flush=True)
                    self._cmd = None
                    # Keep / restore splash while we wait for a connector.
                    self._enter_idle_display(reason="no-connector")
                    return
                # Label from the actual player command (not the full connected set).
                if cmd[0] == "mpv":
                    conn = next(
                        (a.split("=", 1)[1] for a in cmd if a.startswith("--drm-connector=")),
                        "?",
                    )
                    label = f"mpv → {conn}"
                    active_targets = [c for c in targets if c["name"] == conn] or self._best_single(targets)
                elif any("atomic_drm_present" in a for a in cmd):
                    conn = next(
                        (a.split("=", 1)[1] for a in cmd if a.startswith("--connector=")),
                        "?",
                    )
                    conn_names = [n.strip() for n in conn.split(",") if n.strip()]
                    if len(conn_names) > 1:
                        label = f"dual-atomic-page-flip → {conn}"
                    else:
                        label = f"atomic-kms-present → {conn}"
                    active_targets = (
                        [c for c in targets if c["name"] in conn_names]
                        or self._best_single(targets)
                    )
                else:
                    # gst: fbdev dual paints all targets; kmssink dual uses connector-id=
                    is_fbdev = any(a == "fbdevsink" or a.startswith("fbdevsink") for a in cmd)
                    cids: list[int] = []
                    for a in cmd:
                        if a.startswith("connector-id="):
                            try:
                                cids.append(int(a.split("=", 1)[1]))
                            except ValueError:
                                pass
                    if is_fbdev:
                        active_targets = list(targets) if len(targets) > 1 else list(targets)
                    elif cids:
                        by_id = {c.get("id"): c for c in targets if c.get("id") is not None}
                        active_targets = [by_id[i] for i in cids if i in by_id]
                        if not active_targets:
                            active_targets = list(targets)
                    else:
                        active_targets = list(targets)
                    if self._force_single and len(active_targets) > 1:
                        active_targets = self._best_single(active_targets)
                    names = ", ".join(t["name"] for t in active_targets) or "?"
                    is_dual_drm = any("dual_drm_mirror" in a for a in cmd)
                    if is_dual_drm:
                        kind = "dual-drm-scale-to-fit"
                        names = ", ".join(t["name"] for t in targets) or names
                    elif is_fbdev and len(targets) > 1:
                        kind = "gstreamer-fbdev-mirror"
                        names = ", ".join(t["name"] for t in targets) or names
                    elif len(active_targets) > 1:
                        kind = "gstreamer-dual-kms"
                    else:
                        kind = "gstreamer-live"
                    label = f"{kind} → {names}"
            self._cmd = cmd
            # Seamless splash→video for atomic: KEEP external idle_splash up while the
            # new presenter cold-starts + decodes its first frame. Atomic logs
            # NEED_MASTER → we DropMaster the splash → atomic SetMaster+modeset in
            # ~50ms. Hiding splash *before* spawn always flashed the console for
            # the whole GStreamer import (~3–5s). Non-atomic still needs master free.
            is_atomic_cmd = bool(
                cmd and any("atomic_drm_present" in str(a) for a in (cmd or []))
            )
            if is_atomic_cmd:
                print("[*] HDMI: keeping idle splash until first frame (NEED_MASTER handoff)",
                      flush=True)
                self._reap_orphan_players(keep_splash=True)
            else:
                self._hide_idle_splash()
                self._reap_orphan_players(keep_splash=False)
            try:
                # bufsize=0: unbuffered stdin so each chunk hits the OS pipe immediately.
                # stderr piped so silent kmssink/mpv deaths show up in journalctl.
                # Elevate player priority via nice(1) — avoids preexec_fn + threads
                # pitfalls. mpv still gets -10 (present-per-frame vblank). The
                # atomic presenter is SW avdec_h264: nice -10 starved the USB bulk
                # reader (2026-08-31: ES died every ~25s → wait screen). Default
                # +5 so the endpoint keeps the CPU; override O3_ATOMIC_NICE.
                run_cmd = cmd
                if cmd and cmd[0] == "mpv":
                    run_cmd = ["nice", "-n", "-10", *cmd]
                elif cmd and any("atomic_drm_present" in str(a) for a in cmd):
                    try:
                        n = int(os.environ.get("O3_ATOMIC_NICE", "5").strip() or "5")
                    except ValueError:
                        n = 5
                    n = max(-20, min(19, n))
                    run_cmd = ["nice", "-n", str(n), *cmd]
                self._proc = subprocess.Popen(
                    run_cmd, stdin=subprocess.PIPE, bufsize=0,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                )
            except FileNotFoundError:
                need_gst = cmd[0].startswith("gst")
                print(f"[!] HDMI player not found ({cmd[0]}); install mpv"
                      f"{' and gstreamer1.0-plugins-bad' if need_gst else ''}"
                      f" — continuing without HDMI.", flush=True)
                self._proc = None
                self._enter_idle_display(reason="player-missing")
                return
            # Modest pipe grow (default 64 KiB ≈ 70 ms). 1 MiB was part of lag stack.
            # 384 KiB ≈ ~0.4s at 7 Mbit/s — enough for soft prebuffer flush without lag stack.
            try:
                import fcntl
                if self._proc.stdin is not None:
                    fd = self._proc.stdin.fileno()
                    try:
                        fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, 384 * 1024)
                    except (OSError, AttributeError, ValueError):
                        pass
            except Exception:
                pass
            self._last_spawn_mono = time.monotonic()
            # Warm-up window. Most players: 1s (hwdec open), no multi-second backlog.
            # The atomic presenter cold-starts slowly (GStreamer/PyGObject import ~3-5s
            # from cold disk cache). During that window we must NOT drop/re-align, or the
            # first keyframe gets fragmented (SPS split from its IDR in the raw USB stream)
            # and the HW decoder starves → never produces a frame → livelock / frozen
            # console. So give it a deep warm-up buffer that survives the whole cold-start;
            # appsink drop=true + present-latest catches back up to live afterward.
            is_atomic = bool(cmd and any("atomic_drm_present" in a for a in cmd))
            if is_atomic:
                warmup = _env_float("O3_ATOMIC_WARMUP_SECS", 8.0)
                self._spawn_grace_until = self._last_spawn_mono + warmup
                # Backlog SIZE cap during warm-up, decoupled from the TIME window
                # above. Field-confirmed fix (2026-08-22, owner: "much better") for
                # ~5s of "heavy lagged video" on plug-in before it snapped to live.
                # Root cause: this cap used to equal warmup*fps (~424 AUs @ 8s/50fps)
                # — a slow cold-start (3-5s) let that many stale AUs bank before the
                # presenter's stdin even opened, and the presenter then had to
                # decode/present through the whole backlog (queue is deliberately
                # non-leaky — never drop compressed ES) to reach live. The TIME
                # window stays generous (dropping mid cold-start fragments the first
                # keyframe and starves the decoder into livelock), but the SIZE cap
                # doesn't need to grow with it: _enqueue_units' existing
                # back-pressure (clear + re-align to the next SPS, never mid-GOP)
                # fires repeatedly through the whole warm-up window and keeps the
                # backlog small and fresh instead of growing unchecked for the full
                # 8s. 1.5s default field-tested via a systemd drop-in before landing
                # here; override with O3_ATOMIC_WARMUP_BACKLOG_S if needed.
                backlog_s = _env_float("O3_ATOMIC_WARMUP_BACKLOG_S", 1.5)
                self._warmup_limit = max(self._max, int(round(self._container_fps * backlog_s)) + 24)
            else:
                self._spawn_grace_until = self._last_spawn_mono + 1.0
                self._warmup_limit = 0
            self._thr = threading.Thread(target=self._pump, name="hdmi", daemon=True)
            self._thr.start()
            self._err_thr = threading.Thread(
                target=self._drain_stderr, args=(self._proc,), name="hdmi-err", daemon=True,
            )
            self._err_thr.start()
            # Track player kind: fps-adapt + IPC stats are mpv-only (gst sync=false
            # ignores container-fps, so respawning it on fps drift would just flicker).
            self._player_is_mpv = bool(cmd and cmd[0] == "mpv")
            self._player_is_atomic = bool(
                cmd and any("atomic_drm_present" in str(a) for a in cmd)
            )
            self._player_holding_idle = False
            # Presentation diagnostics (vf_fps / drop counters via JSON IPC).
            if cmd and cmd[0] == "mpv":
                # Reset AU rate window so first 1s after spawn is clean.
                self._au_fed_window = 0
                self._au_fed_window_t = time.monotonic()
                self._au_fed_rate = 0.0
                with self._mpv_stats_lock:
                    self._mpv_stats = {}
                self._start_stats_thread()
            print(f"[*] HDMI player started: {label}", flush=True)
            if self.verbose and cmd:
                # Show framedrop/hwdec/ipc in the truncated cmd line (chop A/B).
                print(f"    cmd: {' '.join(cmd[:18])}{' …' if len(cmd) > 18 else ''}",
                      flush=True)
            self._ever_spawned = True

    def _drain_stderr(self, proc: subprocess.Popen):
        """Mirror player stderr into our journal so field failures are diagnosable."""
        err = proc.stderr
        if err is None:
            return
        try:
            for raw in iter(err.readline, b""):
                if self._stop:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                # Always log: --really-quiet is on, so anything that still prints matters.
                print(f"[hdmi-player] {line}", flush=True)
                # Atomic painted splash while keeping DRM master — do NOT spawn external
                # idle_splash (would fight for master / thrash). Just bookkeep.
                if "IDLE_SPLASH" in line:
                    self._player_holding_idle = True
                    # External boot splash (if any) is obsolete; free it so only one owner.
                    if self._splash_proc is not None:
                        print("[*] HDMI: atomic in-process splash — dropping external splash",
                              flush=True)
                        self._hide_idle_splash()
                # Seamless splash→video: first decoded frame is ready — free master now.
                if "NEED_MASTER" in line:
                    if self._splash_proc is not None:
                        print("[*] HDMI: NEED_MASTER — releasing splash for video modeset",
                              flush=True)
                        self._hide_idle_splash()
                    else:
                        # No tracked splash (orphan / already gone); still try reap.
                        try:
                            subprocess.run(
                                ["pkill", "-f", "idle_splash.py"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                check=False,
                            )
                        except Exception:
                            pass
        except (ValueError, OSError):
            pass
        finally:
            try:
                err.close()
            except OSError:
                pass
            # Capture exit code once stderr EOF's (process finished).
            try:
                rc = proc.poll()
                if rc is not None and rc != 0:
                    print(f"[!] HDMI player exited rc={rc}", flush=True)
            except Exception:
                pass
            # Only if the process truly died WITHOUT holding in-process splash (e.g.
            # never modeset). In-process splash path keeps the process alive until
            # SIGTERM from a later spawn.
            try:
                if (
                    not self._stop
                    and self._proc is proc
                    and proc.poll() is not None
                ):
                    was_holding = self._player_holding_idle
                    self._proc = None
                    self._player_holding_idle = False
                    self._need_sps = True
                    self._ever_spawned = False
                    if was_holding:
                        # Process died while it was the splash owner — external fallback.
                        print("[*] HDMI idle-hold process exited — external splash",
                              flush=True)
                    else:
                        print("[*] HDMI player died — external idle splash", flush=True)
                    self._transition_to_idle(reason="player-exit", who_kill="player-exit")
            except Exception as e:
                print(f"[!] HDMI player-exit splash: {e!r}", flush=True)

    def _hotplug_loop(self):
        """Track connector set; do not run an empty mpv just to own DRM (black screen)."""
        while not self._stop:
            # While a video player is live, poll often so death → splash is near-instant
            # (2s sleep used to leave the console on screen for a full second or two).
            p_live = self._proc
            if p_live is not None and p_live.poll() is None:
                time.sleep(0.2)
            else:
                time.sleep(2.0)
            if self._stop:
                break
            targets = resolve_hdmi_targets(self.drm_device, self.connector_spec)
            key = self._targets_fingerprint(targets)
            if key == self._targets_key:
                # If the player died, leave it dead until feed() sees new ES.
                # Auto-respawn here re-grabbed DRM → permanent black console after unplug.
                p = self._proc
                if p is not None and p.poll() is not None:
                    self._proc = None
                    self._need_sps = True
                    self._ever_spawned = False
                    print("[*] HDMI player exited — seamless idle splash until next video",
                          flush=True)
                    self._transition_to_idle(reason="player-exit", who_kill="player-exit")
                continue
            names = ", ".join(t["name"] for t in targets) or "(none)"
            print(f"[*] HDMI hotplug → {names}", flush=True)
            self._force_single = False
            self._respawn_failures = 0
            self._multi_log_once = False
            self._targets_key = key
            # Connector set changed: kill any player. Idle-first re-spawns so a
            # monitor plugged after boot still pays GStreamer import now, not
            # at USB-plug. _spawn() no-ops cleanly when no usable connector.
            if self._proc is not None:
                self._need_sps = True
                self._ever_spawned = False
                self._transition_to_idle(reason="hotplug", who_kill="hotplug")
            idle_first = os.environ.get("O3_ATOMIC_IDLE_FIRST", "1").strip().lower() not in (
                "0", "false", "no", "off",
            )
            if idle_first and selected_player() == "atomic" and self.fixed_cmd is None:
                print("[*] HDMI: idle-first spawn after hotplug", flush=True)
                self._spawn()

    @staticmethod
    def _find_aud_starts(data: bytes) -> list[int]:
        """Offsets of Annex-B AUD NALs (type 9) — O3 stream has one per frame."""
        starts: list[int] = []
        # Fast path: 4-byte start code + AUD (common for O3).
        pat4 = b"\x00\x00\x00\x01\x09"
        pat3 = b"\x00\x00\x01\x09"
        i = 0
        n = len(data)
        while i < n:
            j4 = data.find(pat4, i)
            j3 = data.find(pat3, i)
            if j4 < 0 and j3 < 0:
                break
            if j4 >= 0 and (j3 < 0 or j4 <= j3):
                starts.append(j4)
                i = j4 + 5
            else:
                # Avoid matching the tail of a 4-byte start code as 3-byte.
                if j3 >= 1 and data[j3 - 1] == 0:
                    i = j3 + 1
                    continue
                starts.append(j3)
                i = j3 + 4
        return starts

    def _coalesce_aus(self, chunk: bytes) -> list[bytes]:
        """Split ES into complete access units. Remainder kept.

        O3 layout: first AU is often SPS+PPS+IDR **without** AUD; later frames are
        AUD + slice. Complete units:
          - leading bytes before the first AUD (IDR/params)
          - each AUD .. next AUD span
        Feeding whole frames to mpv reduces demuxer micro-stalls vs 4 KiB USB slices.
        """
        if not self._au_coalesce:
            return [chunk] if chunk else []
        self._au_buf.extend(chunk)
        # Cap assembly buffer (should never grow large — AUDs every ~14 KiB).
        if len(self._au_buf) > 512 * 1024:
            out = bytes(self._au_buf)
            self._au_buf.clear()
            return [out] if out else []
        buf = bytes(self._au_buf)
        starts = self._find_aud_starts(buf)
        out: list[bytes] = []
        if not starts:
            return []
        # Leading SPS/PPS/IDR before first AUD is one complete AU once the next
        # frame's AUD arrives (marks end of the IDR picture).
        if starts[0] > 0:
            lead = buf[: starts[0]]
            if lead:
                out.append(lead)
            # Keep from first AUD; may also emit AUD-delimited AUs below.
            buf = buf[starts[0] :]
            starts = [s - starts[0] for s in starts]
        # Need ≥2 AUDs to close at least one AUD-prefixed frame.
        if len(starts) < 2:
            self._au_buf = bytearray(buf)
            return out
        for i in range(len(starts) - 1):
            piece = buf[starts[i] : starts[i + 1]]
            if piece:
                out.append(piece)
        self._au_buf = bytearray(buf[starts[-1] :])
        return out

    def _enqueue_units(self, units: list[bytes]) -> None:
        """Queue access units / chunks with SPS-realign back-pressure policy."""
        if not units:
            return
        fed = 0
        with self._wake:
            now = time.monotonic()
            if now < self._spawn_grace_until:
                limit = self._warmup_limit or self._grace_max
            else:
                limit = self._max
            for unit in units:
                if not unit:
                    continue
                if len(self._q) >= limit:
                    # NEVER drop-oldest mid-GOP. Clear backlog and re-align to next SPS.
                    n = len(self._q)
                    self._q.clear()
                    self._need_sps = True
                    self._need_prebuffer = True
                    self._pre_q.clear()
                    self._pre_bytes = 0
                    self._au_buf = bytearray()
                    self._sps_hold = bytearray(unit)  # may contain next SPS
                    self._dropped += n
                    self._drop_total += n
                    if self.verbose:
                        print(
                            f"[*] HDMI: back-pressure — discarded {n} queued chunks, "
                            f"re-align to next SPS (total_drops={self._drop_total})",
                            flush=True,
                        )
                    self._wake.notify()
                    break
                self._q.append(unit)
                fed += 1
            self._wake.notify()
        if fed:
            self._note_au_fed(fed)

    def _apply_fps_queue_depth(self) -> None:
        """Keep ~1s of AUs in the hand-off queue at the current content rate."""
        # Only auto-scale when caller left the default-ish depth (not a huge custom).
        target = int(round(self._container_fps * 1.0))
        target = max(24, min(target, 120))
        self._max = target
        self._grace_max = max(target * 2, 24)

    def _snap_fps(self, rate: float) -> float:
        """Snap measured rate to common FPV rates when close; else round."""
        for cand in (24.0, 25.0, 30.0, 50.0, 60.0, 100.0, 120.0):
            if abs(rate - cand) <= max(2.5, cand * 0.06):
                return cand
        return float(round(rate))

    def _consider_fps_adapt(self, measured: float) -> None:
        """If AU feed rate drifts from container_fps, retarget mpv (goggles FPS change)."""
        # gst present-per-frame path has no container-fps to retarget — respawning
        # it on drift would only flicker. Only mpv benefits from adaptive fps.
        if not self._player_is_mpv:
            return
        if not self._fps_adapt or self._fps_env_lock or measured < 15.0:
            return
        snapped = self._snap_fps(measured)
        # Relative change small → ignore (jitter).
        if abs(snapped - self._container_fps) < 4.0 and abs(measured - self._container_fps) < 6.0:
            self._fps_candidate = None
            return
        now = time.monotonic()
        if self._fps_candidate is None or abs(snapped - self._fps_candidate) > 3.0:
            self._fps_candidate = snapped
            self._fps_candidate_since = now
            return
        # Need ~2s of stable new rate before restarting the player.
        if now - self._fps_candidate_since < 2.0:
            return
        old = self._container_fps
        self._container_fps = snapped
        self._fps_candidate = None
        self._apply_fps_queue_depth()
        print(
            f"[*] HDMI: content fps adapt {old:.0f} → {snapped:.0f} "
            f"(measured au_feed={measured:.1f}/s) — respawning player",
            flush=True,
        )
        # Force player rebuild with new container-fps-override.
        self._need_sps = True
        self._need_prebuffer = True
        self._kill_proc(who="fps-adapt")
        self._ever_spawned = False
        # feed() will lazy-spawn on next SPS-aligned data.

    def _note_sps_timing(self, es: bytes) -> None:
        """Log SPS VUI fps (hint only). Measured au_feed is field truth.

        O3 gold SPS advertised 60 while live AU rate was ~50 — never let VUI alone
        override container timing (that was the lag/slow-mo bug). Adaptation is
        driven by _consider_fps_adapt(measured).
        """
        fps = parse_sps_vui_fps(es)
        if fps is None:
            return
        prev = self._sps_vui_fps
        self._sps_vui_fps = fps
        if prev is None or abs(prev - fps) >= 1.0:
            print(
                f"[*] HDMI: SPS VUI timing_info ≈ {fps:.1f} fps "
                f"(hint only; container={self._container_fps:.0f}, "
                f"will follow measured au_feed)",
                flush=True,
            )

    def _note_au_fed(self, n: int) -> None:
        """Track access units handed to the player queue (presentation diagnostics)."""
        now = time.monotonic()
        self._au_fed_total += n
        self._au_fed_window += n
        dt = now - self._au_fed_window_t
        if dt >= 1.0:
            self._au_fed_rate = self._au_fed_window / dt
            self._au_fed_window = 0
            self._au_fed_window_t = now
            self._consider_fps_adapt(self._au_fed_rate)

    def presentation_stats_line(self) -> str:
        """Compact feed vs display counters for the 1 Hz video journal line."""
        with self._mpv_stats_lock:
            st = dict(self._mpv_stats)
        vf = st.get("estimated-vf-fps")
        dfps = st.get("display-fps") or st.get("estimated-display-fps")
        fdrop = st.get("frame-drop-count")
        ddrop = st.get("decoder-frame-drop-count")
        mist = st.get("mistimed-frame-count")
        vdel = st.get("vo-delayed-frame-count")
        parts = [f"au_feed={self._au_fed_rate:.1f}/s"]
        if vf is not None:
            try:
                parts.append(f"vf_fps={float(vf):.1f}")
            except (TypeError, ValueError):
                parts.append(f"vf_fps={vf}")
        if dfps is not None:
            try:
                parts.append(f"disp={float(dfps):.1f}")
            except (TypeError, ValueError):
                parts.append(f"disp={dfps}")
        if fdrop is not None:
            parts.append(f"mpv_drop={fdrop}")
        if ddrop is not None:
            parts.append(f"dec_drop={ddrop}")
        if mist is not None:
            parts.append(f"mistimed={mist}")
        if vdel is not None:
            parts.append(f"vo_delay={vdel}")
        return " ".join(parts)

    def _start_stats_thread(self) -> None:
        thr = self._stats_thr
        if thr is not None and thr.is_alive():
            return
        self._stats_thr = threading.Thread(
            target=self._stats_loop, name="hdmi-mpv-stats", daemon=True,
        )
        self._stats_thr.start()

    def _stats_loop(self) -> None:
        """Poll mpv JSON IPC for fps/drop counters; log every ~2s while playing."""
        props = (
            "estimated-vf-fps",
            "display-fps",
            "estimated-display-fps",
            "container-fps",
            "frame-drop-count",
            "decoder-frame-drop-count",
            "mistimed-frame-count",
            "vo-delayed-frame-count",
            "framedrop",
        )
        # Wait for socket (mpv startup + first frames).
        deadline = time.monotonic() + 8.0
        while not self._stop and time.monotonic() < deadline:
            if os.path.exists(self._ipc_path):
                break
            time.sleep(0.1)
        last_log = 0.0
        while not self._stop:
            p = self._proc
            if p is None or p.poll() is not None:
                break
            if not os.path.exists(self._ipc_path):
                time.sleep(0.5)
                continue
            snap: dict = {}
            for prop in props:
                val = _mpv_ipc_get(self._ipc_path, prop)
                if val is not None:
                    snap[prop] = val
            if snap:
                with self._mpv_stats_lock:
                    self._mpv_stats = snap
                now = time.monotonic()
                if now - last_log >= 2.0:
                    last_log = now
                    vf = snap.get("estimated-vf-fps", "?")
                    try:
                        vf_s = f"{float(vf):.1f}"
                    except (TypeError, ValueError):
                        vf_s = str(vf)
                    print(
                        f"[*] HDMI mpv: vf_fps={vf_s} "
                        f"au_feed={self._au_fed_rate:.1f}/s "
                        f"drop={snap.get('frame-drop-count', '?')} "
                        f"dec_drop={snap.get('decoder-frame-drop-count', '?')} "
                        f"mistimed={snap.get('mistimed-frame-count', '?')} "
                        f"vo_delay={snap.get('vo-delayed-frame-count', '?')} "
                        f"disp={snap.get('display-fps', snap.get('estimated-display-fps', '?'))} "
                        f"framedrop={snap.get('framedrop', '?')}",
                        flush=True,
                    )
            time.sleep(0.5)

    def feed(self, chunk: bytes):
        if not chunk:
            return
        # Lazy start / dead-player recovery: never silently drop forever when mpv dies.
        # Also: if atomic is holding in-process idle splash, wait for SPS then respawn
        # (do not write ES into a process that stopped decoding).
        if not self._stop:
            p = self._proc
            if p is None or p.poll() is not None:
                if p is not None and p.poll() is not None:
                    # Mark dead so _spawn can run (debounce inside).
                    self._proc = None
                    self._need_sps = True
                    self._player_holding_idle = False
                # Only auto-spawn when we are not mid-SPS-hunt with no proc yet —
                # _spawn still runs; SPS gate below prevents mid-GOP.
                if not self._player_holding_idle:
                    self._spawn()
        if self._proc is None:
            return
        # process may have exited without pipe write yet
        if self._proc.poll() is not None:
            return
        # Drop until SPS after (re)spawn / back-pressure recovery so we never hand
        # mpv a mid-GOP ES (mid-GOP drops = unflyable bitty video vs DJI Fly).
        if self._need_sps:
            self._sps_hold.extend(chunk)
            # Cap hold buffer under USB load (~2 MiB ~ 2s of ES).
            if len(self._sps_hold) > 2 * 1024 * 1024:
                self._sps_hold = bytearray(self._sps_hold[-(512 * 1024):])
            pos = _find_sps(bytes(self._sps_hold))
            if pos is None:
                return
            chunk = bytes(self._sps_hold[pos:])
            self._sps_hold.clear()
            self._need_sps = False
            self._need_prebuffer = True
            self._pre_q.clear()
            self._pre_bytes = 0
            self._au_buf = bytearray()
            print(f"[*] HDMI: ES aligned to SPS ({len(chunk)} bytes from keyframe)", flush=True)
            self._note_sps_timing(chunk)
        units = self._coalesce_aus(chunk)
        if not units and self._au_coalesce:
            return  # holding partial AU (resume fires once we have a full AU)
        if not units:
            units = [chunk]
        # Patch SPS level_idc → L5.1 on EVERY access unit (not just the first).
        # CRITICAL (2026-07-14, root-caused from a live capture): the O3 stream repeats
        # an SPS with every keyframe (~one per ~0.3s) and they are all **L5.2**. The Pi
        # HW decoder (v4l2h264dec, bcm2835) advertises ≤ L5.1 and **rejects** any L5.2
        # SPS → GStreamer `not-negotiated (-4)` the moment the 2nd keyframe arrives (mpv's
        # v4l2m2m tolerated it; the strict gst decoder does not). Patching only the first
        # SPS therefore fails on the live path. Patch to **5.1** (not 4.2): the native
        # stream is L5.2 for a reason (high 1080p rate); L5.1 fits it and is the decoder's
        # max, whereas 4.2 could violate macroblock-rate limits for 1080p>64. Units here
        # are complete AUs, so each SPS is intact (no cross-chunk split to miss).
        if self._level_patch:
            saw_any = False
            for i, u in enumerate(units):
                pu, saw = patch_h264_level(u, 51)
                if saw:
                    units[i] = pu
                    saw_any = True
            if saw_any and not self._level_patched_once:
                self._level_patched_once = True
                print("[*] HDMI: SPS level_idc patched → 5.1 on every AU "
                      "(HW decoder needs ≤5.1; O3 repeats L5.2 SPS per keyframe)",
                      flush=True)
        self._enqueue_units(units)
        # Resume idle-holding atomic after ES is queued. Do this on ANY chunk once
        # IDLE_SPLASH has been logged — not only on the SPS-align event — so a
        # boot idle-first presenter (or a respawn whose first SPS arrived during
        # gi import) still gets SIGUSR2. Never SIGUSR2 before IDLE_SPLASH:
        # default SIGUSR2 kills the process during import.
        if self._player_holding_idle:
            self._player_holding_idle = False
            self._pending_idle_resume = True
        if self._pending_idle_resume:
            self._pending_idle_resume = False
            p = self._proc
            if p is not None and p.poll() is None:
                try:
                    os.kill(p.pid, signal.SIGUSR2)
                    print("[*] HDMI: SIGUSR2 in-process resume (keep DRM master)", flush=True)
                except (ProcessLookupError, PermissionError, OSError) as e:
                    print(f"[!] HDMI: SIGUSR2 resume failed: {e!r} — will respawn",
                          flush=True)
                    self._last_spawn_mono = 0.0
                    self._spawn()

    def _write_stdin(self, stdin, data: bytes) -> bool:
        """Write ES bytes to player stdin. Returns False on write error (which may be a
        transient — the caller must check whether the player is actually dead)."""
        try:
            view = memoryview(data)
            off = 0
            while off < len(view) and not self._stop:
                try:
                    n = stdin.write(view[off:])
                    if n is None:
                        n = len(view) - off
                    off += n
                    if off < len(view):
                        time.sleep(0.001)
                except BlockingIOError:
                    time.sleep(0.001)
            return True
        except (BrokenPipeError, ValueError, OSError) as e:
            self._last_write_err = repr(e)
            return False

    def _pump(self):
        proc = self._proc  # this pump feeds exactly THIS process
        stdin = proc.stdin if proc else None
        while not self._stop and stdin is not None:
            # If a newer player has taken over (or we were killed), this pump must exit
            # immediately — a superseded pump touching the shared queue starved the live
            # player to ~1.5 fps (field 2026-07-16 test #4).
            if self._proc is not proc:
                return
            with self._wake:
                while not self._q and not self._stop and self._proc is proc:
                    self._wake.wait(0.5)
                if (self._stop or self._proc is not proc) and not self._q:
                    break
                # Drain everything currently queued in one go so the pipe stays full for
                # the decoder's eager demuxer and we don't wake/sleep per tiny chunk.
                batch = []
                while self._q:
                    batch.append(self._q.popleft())
            if not batch:
                continue
            # Soft prebuffer: only a few frames before first write — NEVER dump the
            # whole hand-off queue (flight 2: 273 units / 4 MiB dump → multi-second lag).
            if self._need_prebuffer and (self._prebuffer_frames > 0 or self._prebuffer_bytes > 0):
                taken = 0
                for u in batch:
                    self._pre_q.append(u)
                    self._pre_bytes += len(u)
                    taken += 1
                    if (
                        len(self._pre_q) >= self._prebuffer_frames
                        or self._pre_bytes >= self._prebuffer_bytes
                    ):
                        break
                # Put unused AUs back at the front of the queue for the next pump cycle.
                if taken < len(batch):
                    with self._wake:
                        for u in reversed(batch[taken:]):
                            self._q.appendleft(u)
                        self._wake.notify()
                ready = (
                    len(self._pre_q) >= self._prebuffer_frames
                    or self._pre_bytes >= self._prebuffer_bytes
                )
                if not ready:
                    continue
                batch = list(self._pre_q)
                self._pre_q.clear()
                self._pre_bytes = 0
                self._need_prebuffer = False
                if self.verbose:
                    print(
                        f"[*] HDMI: soft prebuffer release ({len(batch)} units, "
                        f"{sum(len(x) for x in batch)} bytes)",
                        flush=True,
                    )
            data = b"".join(batch)
            if not self._write_stdin(stdin, data):
                if self._stop or self._proc is not proc:
                    return  # stopped or superseded — do not touch shared state
                rc = None
                try:
                    rc = proc.poll()
                except Exception:
                    pass
                # This pump can no longer feed its player (stdin closed / pipe broke).
                # Exit the pump and mark for a clean SPS-aligned respawn. Do NOT spin
                # retrying (that zombie-cleared the shared queue → 1.5 fps) and do NOT
                # SIGTERM a live cold-starting presenter (that was the rc=-15 thrash) —
                # if it's alive it will be reaped by the next spawn's orphan sweep.
                if not self._stop:
                    print(f"[!] HDMI stdin unwritable ({self._last_write_err}); "
                          f"player rc={rc} — respawn on next SPS-aligned feed.", flush=True)
                self._proc = None
                self._need_sps = True
                self._need_prebuffer = True
                return

    @staticmethod
    def _reap_orphan_players(keep_splash: bool = False) -> None:
        """Best-effort kill leftover DRM players (mpv / gst / dual_drm [/ splash]).

        keep_splash=True: leave idle_splash.py alone so atomic can hand off on NEED_MASTER
        without a console gap during GStreamer cold-start.
        """
        for name in ("mpv", "gst-launch-1.0"):
            try:
                subprocess.run(
                    ["pkill", "-x", name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
            except Exception:
                pass
        pats = ["dual_drm_mirror.py", "atomic_drm_present.py"]
        if not keep_splash:
            pats.append("idle_splash.py")
        for pat in pats:
            try:
                subprocess.run(
                    ["pkill", "-f", pat],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
            except Exception:
                pass
        time.sleep(0.15)

    def _kill_proc(self, who: str = "?"):
        thr, self._thr = self._thr, None
        err_thr, self._err_thr = self._err_thr, None
        stats_thr, self._stats_thr = self._stats_thr, None
        # Signal pump to exit before closing stdin.
        p, self._proc = self._proc, None
        if p is not None:
            alive = p.poll() is None
            print(f"[*] HDMI: _kill_proc(who={who}) closing player pid={p.pid} "
                  f"alive={alive}", flush=True)
        if thr is not None and thr.is_alive():
            with self._wake:
                self._wake.notify()
            thr.join(timeout=1.5)
        if p is not None:
            try:
                if p.stdin:
                    p.stdin.close()
            except OSError:
                pass
            try:
                if p.stderr:
                    p.stderr.close()
            except OSError:
                pass
            try:
                p.terminate()
                p.wait(timeout=2)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
            # GStreamer may leave child helpers; best-effort reap by pgid not needed
            # since we didn't start a new session.
        if err_thr is not None and err_thr.is_alive():
            err_thr.join(timeout=0.5)
        if stats_thr is not None and stats_thr.is_alive():
            stats_thr.join(timeout=0.5)
        # Drop stale IPC socket so the next mpv can bind.
        try:
            if self._ipc_path and os.path.exists(self._ipc_path):
                os.unlink(self._ipc_path)
        except OSError:
            pass
        with self._mpv_stats_lock:
            self._mpv_stats = {}

    def close(self):
        self._stop = True
        with self._wake:
            self._wake.notify()
        w, self._watch = self._watch, None
        if w is not None and w.is_alive():
            w.join(timeout=1.0)
        self._kill_proc(who="close")
        self._hide_idle_splash()
        # Service stop / bench teardown: give the console back for maintenance.
        self._release_console()
        if self._dropped:
            print(f"[*] HDMI: dropped {self._dropped} chunks under back-pressure", flush=True)
            self._dropped = 0

    def _enter_idle_display(self, reason: str = "idle") -> None:
        """Own the HDMI plane while waiting for video (splash), or fall back to console.

        Prefer `_transition_to_idle` when a live player is still holding DRM — that
        path pre-starts splash before killing the player so the console never paints.
        """
        if self._idle_splash:
            if self._show_idle_splash(reason=reason):
                return
            print(f"[*] HDMI: idle splash failed ({reason}) — falling back to console",
                  flush=True)
        self._release_console()

    def _transition_to_idle(self, reason: str = "idle", who_kill: str = "idle") -> None:
        """Seamless feed→splash: unbind console, pre-start splash, then kill player.

        Order matters. The previous path (kill player → DropMaster → console paints →
        cold-start splash ~1–2s later) flashed the Linux console on every drone power
        cycle. New order:

          1. Unbind vtconsole so fbcon cannot reclaim the plane when master drops.
          2. Spawn idle_splash *while the presenter may still hold DRM master* —
             splash spins on drmSetMaster (50ms ticks).
          3. Kill the video player (master free → splash grabs it within ~50ms).
          4. Wait for splash stdout "READY" (modeset done) before returning.

        `_kill_proc` is intentionally *outside* `_idle_lock`: it joins the player
        stderr thread, which may itself call this method on player death — holding
        the lock across that join would deadlock.
        """
        if not self._idle_splash:
            self._kill_proc(who=who_kill)
            self._release_console()
            return

        already_up = False
        started = False
        with self._idle_lock:
            sp = self._splash_proc
            if sp is not None and sp.poll() is None:
                already_up = True
            else:
                # 1) Blind the text console before any DRM master release.
                self._unbind_vtconsole()
                # 2) Pre-start splash (may wait on SetMaster while player still holds DRM).
                started = self._start_idle_splash_proc()

        # 3) Always release the video player (no-op if already dead). Outside the lock.
        self._kill_proc(who=who_kill)

        if already_up:
            return
        if not started:
            print(f"[*] HDMI: idle splash failed to start ({reason}) — console fallback",
                  flush=True)
            self._release_console()
            return

        with self._idle_lock:
            # Another thread may have finished the wait already.
            sp = self._splash_proc
            if sp is None:
                self._release_console()
                return
            # 4) Wait until splash has painted.
            if not self._wait_splash_ready(timeout=4.0):
                err = self._splash_early_err()
                print(f"[!] HDMI idle splash not ready ({reason}) {err} — console fallback",
                      flush=True)
                self._hide_idle_splash()
                self._release_console()
                return
            # Only log once if two threads race through wait.
            print(f"[*] HDMI idle splash ({reason}): {self._idle_splash_text!r}", flush=True)
            try:
                threading.Thread(
                    target=self._drain_splash_stderr,
                    args=(self._splash_proc,),
                    name="idle-splash-err",
                    daemon=True,
                ).start()
            except Exception:
                pass

    @staticmethod
    def _unbind_vtconsole() -> None:
        """Stop fbcon from painting when DRM master drops (handoff gap)."""
        for vt in ("vtcon0", "vtcon1", "vtcon2"):
            path = f"/sys/class/vtconsole/{vt}/bind"
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("0\n")
            except OSError:
                pass

    def _splash_connector_arg(self) -> str:
        """Connector arg for idle_splash — multi when dual-atomic will use ≥2 ports."""
        if dual_hdmi_enabled():
            try:
                targets = resolve_hdmi_targets(self.drm_device, self.connector_spec)
            except Exception:
                targets = []
            if len(targets) > 1:
                return ",".join(t["name"] for t in targets)
        conn = self.connector_spec
        if not conn or conn.strip().lower() in ("auto", "any", "*"):
            return "auto"
        # Single pin (field default HDMI-A-2) or first of an explicit list.
        return conn.split(",")[0].strip() or "auto"

    def _start_idle_splash_proc(self) -> bool:
        """Spawn idle_splash.py (does not wait for READY). Returns False on spawn fail."""
        sp = self._splash_proc
        if sp is not None and sp.poll() is None:
            return True
        script = str(_HERE / "idle_splash.py")
        if not os.path.isfile(script):
            print(f"[!] idle splash script missing: {script}", flush=True)
            return False
        cmd = [
            sys.executable, script,
            "--device", self.drm_device,
            "--connector", self._splash_connector_arg(),
            "--text", self._idle_splash_text,
        ]
        try:
            self._splash_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._splash_ready = False
        except OSError as e:
            print(f"[!] idle splash spawn failed: {e}", flush=True)
            self._splash_proc = None
            self._splash_ready = False
            return False
        return True

    def _wait_splash_ready(self, timeout: float = 4.0) -> bool:
        """Block until splash prints READY on stdout (modeset done), or fail."""
        if self._splash_ready:
            return True
        sp = self._splash_proc
        if sp is None:
            return False
        out = sp.stdout
        if out is None:
            # No pipe — fall back to brief alive check (legacy).
            time.sleep(0.35)
            ok = sp.poll() is None
            self._splash_ready = ok
            return ok
        deadline = time.monotonic() + max(0.5, timeout)
        buf = b""
        fd = out.fileno()
        while time.monotonic() < deadline:
            if self._splash_ready:
                return True
            if sp.poll() is not None:
                return False
            try:
                r, _, _ = select.select([fd], [], [], 0.1)
            except (ValueError, OSError):
                ok = sp.poll() is None
                self._splash_ready = ok
                return ok
            if not r:
                continue
            try:
                chunk = os.read(fd, 256)
            except OSError:
                return False
            if not chunk:
                # EOF before READY — process exiting.
                return False
            buf += chunk
            if b"READY" in buf:
                self._splash_ready = True
                return True
        # Timed out: if still alive, assume paint is in progress (better than console).
        ok = sp.poll() is None
        if ok:
            self._splash_ready = True
        return ok

    def _splash_early_err(self) -> str:
        sp = self._splash_proc
        if sp is None:
            return ""
        bits: list[str] = []
        if sp.poll() is not None:
            bits.append(f"rc={sp.returncode}")
        try:
            if sp.stderr:
                # Non-blocking-ish: only what is already buffered.
                try:
                    r, _, _ = select.select([sp.stderr.fileno()], [], [], 0.0)
                    if r:
                        bits.append(sp.stderr.read().decode("utf-8", "replace")[-400:].strip())
                except Exception:
                    pass
        except Exception:
            pass
        return " ".join(bits)

    def _show_idle_splash(self, reason: str = "idle") -> bool:
        """Spawn idle_splash.py if not already holding the plane. Returns True if up.

        Used when no video player is holding DRM (boot, already-idle). For live
        feed→splash handoff use `_transition_to_idle` instead.
        """
        with self._idle_lock:
            sp = self._splash_proc
            if sp is not None and sp.poll() is None:
                return True  # already painting
            self._unbind_vtconsole()
            if not self._start_idle_splash_proc():
                return False
            if not self._wait_splash_ready(timeout=3.0):
                err = self._splash_early_err()
                print(f"[!] idle splash not ready ({reason}) {err}", flush=True)
                self._hide_idle_splash()
                return False
            print(f"[*] HDMI idle splash ({reason}): {self._idle_splash_text!r}", flush=True)
            try:
                threading.Thread(
                    target=self._drain_splash_stderr,
                    args=(self._splash_proc,),
                    name="idle-splash-err",
                    daemon=True,
                ).start()
            except Exception:
                pass
            return True

    def _drain_splash_stderr(self, proc: subprocess.Popen) -> None:
        err = proc.stderr
        if err is None:
            return
        try:
            for raw in iter(err.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    # idle_splash.py already prefixes "[idle-splash]"; don't double it.
                    if line.startswith("[idle-splash]"):
                        print(line, flush=True)
                    else:
                        print(f"[idle-splash] {line}", flush=True)
        except (ValueError, OSError):
            pass
        finally:
            try:
                err.close()
            except OSError:
                pass

    def _hide_idle_splash(self) -> None:
        """SIGTERM the splash so it DropMaster's before the video presenter opens DRM."""
        sp, self._splash_proc = self._splash_proc, None
        self._splash_ready = False
        if sp is None:
            # Also reap orphans from a previous crash / manual run.
            try:
                subprocess.run(
                    ["pkill", "-f", "idle_splash.py"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
            except Exception:
                pass
            return
        if sp.poll() is None:
            try:
                sp.terminate()
            except Exception:
                pass
            try:
                sp.wait(timeout=1.5)
            except Exception:
                try:
                    sp.kill()
                except Exception:
                    pass
        for stream in (getattr(sp, "stdout", None), getattr(sp, "stderr", None)):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass
        # Give the kernel a beat to hand DRM master to the next client.
        time.sleep(0.1)

    @staticmethod
    def _release_console() -> None:
        """Best-effort return of the HDMI plane to the Linux text console.

        Used when O3_IDLE_SPLASH=0, or on service teardown (close), so maintenance
        still has a usable console. Live idle path prefers the DRM splash instead.
        """
        # Drop any leftover DRM clients first.
        try:
            subprocess.run(
                ["pkill", "-x", "mpv"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        except Exception:
            pass
        # Re-bind vtcon to DRM/fbdev so login console paints again.
        for vt in ("vtcon0", "vtcon1", "vtcon2"):
            path = f"/sys/class/vtconsole/{vt}/bind"
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("1\n")
            except OSError:
                pass
        # Nudge a VT switch so fbcon redraws (ignore failures under pure DRM).
        for cmd in (
            ["chvt", "1"],
            ["systemd-run", "--quiet", "--no-block", "/bin/true"],
        ):
            try:
                subprocess.run(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
            except Exception:
                pass

    def _signal_atomic_idle_splash(self) -> bool:
        """Ask a live atomic presenter to paint splash in-process (SIGUSR1).

        Returns True if the signal was delivered. The presenter keeps DRM master and
        paints the waiting text — zero console flash. Endpoint resumes on the next
        SPS via SIGUSR2 (same process; see feed()).
        """
        p = self._proc
        if p is None or p.poll() is not None:
            return False
        if not self._player_is_atomic:
            return False
        if self._player_holding_idle:
            return True  # already holding
        try:
            os.kill(p.pid, signal.SIGUSR1)
        except (ProcessLookupError, PermissionError, OSError) as e:
            print(f"[!] HDMI: SIGUSR1 idle-splash failed: {e!r}", flush=True)
            return False
        self._player_holding_idle = True  # optimistic; confirmed by IDLE_SPLASH log
        self._need_sps = True
        print(f"[*] HDMI: SIGUSR1 → atomic in-process idle splash (pid={p.pid})",
              flush=True)
        return True

    def notify_stream_restart(self, reason: str = "source restart") -> None:
        """The ES source (goggles) restarted the video stream mid-session — e.g. an
        air-unit hot-swap where the AOA link stayed up but video-out stopped and resumed.

        Drop stale queued/held ES and re-align to the next SPS. Prefer in-process
        splash on the live atomic presenter (keeps DRM master) over kill+external
        splash (that path always flashed the console — confirmed journal 2026-07-16).
        """
        with self._wake:
            self._q.clear()
            self._wake.notify()
        self._need_sps = True
        self._sps_hold = bytearray()
        self._need_prebuffer = True
        self._pre_q.clear()
        self._pre_bytes = 0
        self._au_buf = bytearray()
        p = self._proc
        if p is not None and p.poll() is None:
            # Seamless path: tell atomic to paint splash and HOLD master.
            if self._signal_atomic_idle_splash():
                print(f"[*] HDMI: stream restart ({reason}) — in-process splash, "
                      f"re-aligning to next SPS", flush=True)
                return
            # Non-atomic / signal failed: fall back to kill + external splash.
            self._last_spawn_mono = 0.0
            self._ever_spawned = False
            self._transition_to_idle(
                reason=f"stream-restart:{reason}", who_kill="stream-restart",
            )
        print(f"[*] HDMI: stream restart ({reason}) — dropping stale ES, "
              f"re-aligning to next SPS", flush=True)

    def idle_until_video(self, reason: str = "unplug") -> None:
        """Stop the decoder — do NOT respawn until the next ES feed.

        Field: restart()-on-unplug left mpv sitting on a black frame with no console.
        feed() lazy-starts the player again on the next SPS. Prefer in-process splash
        on atomic; else external idle_splash.
        """
        print(f"[*] HDMI idle ({reason}) — splash until next video", flush=True)
        self._stop = False  # hotplug watcher stays armed
        with self._wake:
            self._q.clear()
            self._wake.notify()
        self._need_sps = True
        self._sps_hold = bytearray()
        self._ever_spawned = False  # hotplug must not auto-respawn empty mpv
        # Prefer seamless in-process splash; only kill+external if that fails.
        if self._signal_atomic_idle_splash():
            return
        self._transition_to_idle(reason=reason, who_kill=f"idle:{reason}")

    def restart(self):
        """Kill and respawn (manual/bench). Prefer idle_until_video() after unplug."""
        was_stopped = self._stop
        self._stop = False
        self._spawn()
        if was_stopped and self.fixed_cmd is None and self._watch is None:
            self._watch = threading.Thread(target=self._hotplug_loop, name="hdmi-hotplug",
                                           daemon=True)
            self._watch.start()


class AccessorySession:
    def __init__(self, rg: RawGadget, out_path: str, verbose: bool, cold_2d01: bool = False,
                 player: "HdmiPlayer | None" = None):
        self.rg = rg
        self.verbose = verbose
        self.player = player
        self.configured = False
        self.ep_out_num = None  # host→device (video)
        self.ep_in_num = None   # device→host (responses)
        # Live path: do not retain every ES chunk (unbounded RAM → GC stalls → choppy).
        # Capture-to-file still gets incremental writes via _emit; retain only needed for
        # offline elementary_from_sps helpers.
        self.deframer = StreamingVideoDeframer(retain_payloads=False)
        self.aoa_buf = bytearray()
        self.out_fh = open(out_path, "wb") if out_path else None
        self._es_written = 0
        self._stop = False
        self._reader = None
        self._driver = None
        self._seq = 0xD4E1
        # Serialize writes to the bulk IN endpoint: the driver thread (command replay) and the
        # reader thread (identity-probe replies) both write 0x81. Without this they race on the
        # single raw-gadget IN request slot → EBUSY/EAGAIN, and the dropped write is often the
        # periodic cs=2/0xeb camera subscribe (the video keepalive) → the goggles stop streaming
        # or the picture goes bitty/choppy.
        self._tx_lock = threading.Lock()
        self._tx_ok = 0
        self._tx_fail = 0
        self._tx_fail_log_t = 0.0
        # Host liveness (monotonic). UDC sysfs often stays "configured" after a real unplug
        # with no USB_RAW_EVENT_DISCONNECT — that's the recurring "phantom host" hang.
        # Any successful bulk RX, bulk IN TX, or EP0 control proves the host is still there.
        now0 = time.monotonic()
        self._host_rx_mono = 0.0
        self._host_tx_ok_mono = 0.0
        self._host_ep0_mono = 0.0
        self._host_activity_mono = now0  # last *any* host proof
        self._rearm_lock = threading.Lock()
        self._phantom_rearm_reason: str | None = None
        # Video-resume re-arm (air-unit hot-swap, 2026-07-16 P0). When the drone is
        # power-cycled while the goggles stay USB-plugged into the Pi, the goggles keep
        # the AOA link up (our keepalive TX still succeeds) but stop sending USB
        # video-out. The light SUSTAIN_SEQ keepalives do NOT re-subscribe the camera/
        # live-view path once the goggles' session tears down — only the full APP_SEQ
        # arming burst does. So when ES has gone silent for a bit while the host is
        # still alive, ask the seed thread to replay the arming burst, and keep
        # re-asking every few seconds until video returns (the drone may still be
        # booting). SAFE vs the 2026-07-14 chop: that came from re-arming *while
        # streaming*; this only fires while there is NO video.
        self._rearm_video_event = threading.Event()
        # When we re-arm, the goggles resume video-out on the SAME session — but the
        # deframer is still holding the partial frame from where the OLD stream cut off
        # mid-frame (drone-off). Feeding the resumed stream onto that stale partial splices
        # old+new → corrupt access units → the decoder never locks (field 2026-07-16 test #2:
        # ES resumed in abnormal 13-20 Mbit/s bursts, garb exploded, presenter thrashed).
        # A cold plug works precisely because its deframer is brand-new. So on re-arm, flag
        # the reader thread to rebuild the deframer from scratch before parsing resumed bytes.
        self._deframer_reset_evt = threading.Event()
        self._video_rearm_count = 0
        self._es_last_grow_mono = 0.0     # last time _es_written increased
        self._video_rearm_last_mono = 0.0  # last time we requested a re-arm (cadence)
        self._last_suspend_mono = 0.0      # USB SUSPEND — unplug in progress, not hot-swap
        self._initial_arm_done = False
        self._initial_arm_mono = 0.0
        self._video_rearm_enable = os.environ.get(
            "O3_VIDEO_REARM", "1"
        ).strip().lower() not in ("0", "false", "no", "off")
        self._video_silence_s = _env_float("O3_VIDEO_SILENCE_REARM_S", 6.0)
        self._video_rearm_period_s = _env_float("O3_VIDEO_REARM_PERIOD_S", 8.0)
        # After the stream RESPONDS to a re-arm, hold off re-arming for this long so the
        # presenter can cold-start (~8s) and lock onto the resumed stream undisturbed.
        # Field 2026-07-16: re-arming every 3s while video was coming back kept resetting
        # the goggles' encoder → bursty garbage → presenter never survived warm-up →
        # eventual USB SUSPEND. The settle window is the key fix.
        self._video_rearm_settle_s = _env_float("O3_VIDEO_REARM_SETTLE_S", 20.0)
        # Incremental video-write state (avoid the O(n²) elementary_from_sps rebuild per chunk).
        self._vid_pending = []      # chunks buffered until first SPS is seen
        self._vid_started = False   # once we've written from the SPS onward
        self._flush_ctr = 0
        self._vid_chunks = 0
        self._stat_t = 0.0
        self.config_value = 0
        # AOA accessory bring-up state. cold_2d01=True skips the handshake and presents the
        # accessory PID directly (the original behavior — kept for A/B comparison). Default is
        # to start in "normal" mode and let the goggles drive GET_PROTOCOL→SEND_STRING→START,
        # exactly as they bring up the phone in the gold capture, then re-enumerate to 2d01.
        self.mode = "accessory" if cold_2d01 else "normal"
        self.cold_2d01 = cold_2d01
        self.aoa_strings = {}      # wIndex → string the goggles sent us
        self.pending_reenum = False
        # Ignore the disconnect/reset that accompanies our own AOA re-enum (ACCESSORY_START).
        self._expect_reenum_bus_cycle = False

    @property
    def cur_pid(self):
        return PID_ACCESSORY if self.mode == "accessory" else PID_NORMAL

    def log(self, *a):
        if self.verbose:
            print(*a, flush=True)

    def _touch_host(self, kind: str = "activity"):
        """Record proof that the USB host (goggles) is still talking to us."""
        now = time.monotonic()
        self._host_activity_mono = now
        if kind == "rx":
            self._host_rx_mono = now
        elif kind == "tx":
            self._host_tx_ok_mono = now
        elif kind == "ep0":
            self._host_ep0_mono = now

    def _stop_io_threads(self):
        """Stop bulk reader + gold driver and wait for them to exit."""
        self._stop = True
        self.configured = False
        for t in (self._reader, self._driver):
            if t is not None and t.is_alive():
                t.join(timeout=2.0)
        self._reader = None
        self._driver = None
        self.ep_out_num = None
        self.ep_in_num = None
        self._stop = False

    def on_bus_reset(self):
        """Host bus reset while still notionally attached — pause I/O; SET_CONFIGURATION
        will re-enable endpoints and restart reader/driver. Does NOT flip AOA mode."""
        if self._expect_reenum_bus_cycle:
            self.log("bus reset during AOA re-enum — ignored")
            return
        was = self.configured
        self._stop_io_threads()
        self.config_value = 0
        if was:
            print("[*] bus reset — I/O paused; waiting for re-configuration", flush=True)

    def signal_phantom_unplug(self, reason: str) -> bool:
        """Host is gone but UDC/raw-gadget never delivered DISCONNECT (common on dwc2).

        Stops gold/video I/O immediately (kills EAGAIN spam) and closes the raw-gadget fd
        so the main thread's blocking event_fetch wakes with EBADF and runs a full rearm.
        Returns True if this call armed the rearm path.
        """
        with self._rearm_lock:
            if self._expect_reenum_bus_cycle:
                return False
            if self._phantom_rearm_reason is not None:
                return False
            # Idle already (never configured / already rearmed) — nothing to do.
            if not self.configured and self._reader is None and self._driver is None:
                return False
            self._phantom_rearm_reason = reason
            print(
                f"[*] {reason} — phantom host (UDC often stays 'configured' with no "
                f"DISCONNECT); stopping I/O + re-arm for next plug",
                flush=True,
            )
            # Drop configured first so seed/reader loops exit without more ep_in spam.
            self._stop = True
            self.configured = False
            try:
                self.rg.close_fd_only()
            except OSError:
                pass
            return True

    def take_phantom_rearm_reason(self) -> str | None:
        with self._rearm_lock:
            r = self._phantom_rearm_reason
            self._phantom_rearm_reason = None
            return r

    def rearm_for_next_plug(self, reason: str = "disconnect"):
        """Full re-arm after goggles unplug / power-off so the next plug runs the full
        AOA handshake again. Idempotent: safe if already idle. Restarts the HDMI player
        so the next SPS-aligned ES doesn't land on a half-decoded stream.

        Also used for *phantom* unplug (no DISCONNECT event; UDC stuck at 'configured').
        """
        if self._expect_reenum_bus_cycle and not reason.startswith("phantom"):
            self.log(f"{reason} during AOA re-enum — ignored")
            return
        print(f"[*] {reason} — re-arming for next plug (AOA from scratch)", flush=True)
        self._stop_io_threads()
        self.config_value = 0
        # Real DISCONNECT: keep the accessory bind. Closing /dev/raw-gadget here
        # oopses dwc2 (raw_release → gadget_resume → raw_event_queue_add) and the
        # watchdog reboots the Pi (2026-08-31, twice). Phantom unplug still
        # re-inits because the UDC is stuck with no DISCONNECT.
        keep_bind = (reason == "disconnect" and not self.cold_2d01)
        self.mode = "accessory" if (self.cold_2d01 or keep_bind) else "normal"
        self.aoa_strings = {}
        self.pending_reenum = False
        self.aoa_buf = bytearray()
        self.deframer = StreamingVideoDeframer(retain_payloads=False)
        self._vid_pending = []
        self._vid_started = False
        self._vid_chunks = 0
        self._es_written = 0
        self._flush_ctr = 0
        self._tx_ok = 0
        self._tx_fail = 0
        self._host_rx_mono = 0.0
        self._host_tx_ok_mono = 0.0
        self._host_ep0_mono = 0.0
        self._host_activity_mono = time.monotonic()
        # Reset hot-swap video-resume state (fresh AOA session from scratch).
        self._rearm_video_event.clear()
        self._deframer_reset_evt.clear()
        self._es_last_grow_mono = 0.0
        self._video_rearm_last_mono = 0.0
        self._initial_arm_done = False
        self._initial_arm_mono = 0.0
        with self._rearm_lock:
            self._phantom_rearm_reason = None
        # Keep the capture file open (append more after re-plug) when recording; only
        # reset the in-memory ES bookkeeping so stats stay honest per session.
        # Prefer in-process atomic splash (keep DRM master). Killing the player here
        # DropMasters → Linux console flash (2026-08-31 reconnect).
        if self.player is not None:
            try:
                self.player.idle_until_video(reason=reason)
            except Exception as e:
                print(f"[!] HDMI idle after rearm: {e}", flush=True)
        # Present normal PID again so the goggles re-run GET_PROTOCOL→…→START.
        # Skip on a real DISCONNECT — keep the existing accessory bind.
        if not self.cold_2d01 and not keep_bind:
            self._reinit_gadget_for_next_plug()
        elif keep_bind:
            print("[*] gadget kept bound after DISCONNECT (no close/reinit)", flush=True)

    def _wait_udc_quiescent(self, timeout_s: float = 4.0) -> str:
        """Wait until dwc2 reports not-attached (unplug finished winding down)."""
        deadline = time.monotonic() + timeout_s
        st = _udc_state(self.rg.device)
        while time.monotonic() < deadline:
            if st in ("not attached", "not_attached"):
                return st
            time.sleep(0.2)
            st = _udc_state(self.rg.device)
        return st

    def _reinit_gadget_for_next_plug(self) -> None:
        st = self._wait_udc_quiescent(4.0)
        print(f"[*] gadget re-init: udc={st} after unplug settle", flush=True)
        last_err: OSError | None = None
        # Keep DRM splash up; do not SystemExit after 2× EBUSY (that was the
        # "crash then recover" — systemd RestartSec=5, console). Wait as long
        # as a service restart would, in-process.
        gaps = (0.0, 4.0, 5.0)
        for i, gap in enumerate(gaps):
            if gap:
                time.sleep(gap)
            try:
                self.rg.reinit(settle=2.0)
                print(
                    f"[*] gadget re-init as normal (awaiting AOA); "
                    f"udc={_udc_state(self.rg.device)} attempt={i + 1}",
                    flush=True,
                )
                return
            except OSError as e:
                last_err = e
                retryable = e.errno in (errno.EBUSY, errno.EAGAIN, errno.ENODEV)
                more = retryable and i < len(gaps) - 1
                print(
                    f"[!] gadget re-init attempt {i + 1} failed: {e}"
                    f"{' — waiting, retry' if more else ''}",
                    flush=True,
                )
                if not retryable:
                    break
        print(
            f"[!] gadget re-init failed: {last_err} — process will exit for systemd restart",
            flush=True,
        )
        raise last_err if last_err is not None else OSError("gadget re-init failed")

    def handle_control(self, data: bytes):
        ctrl = parse_ctrl(data)
        if not ctrl:
            return
        rt, req = ctrl["bRequestType"], ctrl["bRequest"]
        wValue, wIndex, wLength = ctrl["wValue"], ctrl["wIndex"], ctrl["wLength"]
        # All-zero ctrlrequest = event buffer not filled (should not happen after
        # length-in fix); ignore rather than stall the bus.
        if rt == 0 and req == 0 and wValue == 0 and wIndex == 0 and wLength == 0:
            self.log(f"EP0 empty/zero control (raw={data[:16].hex()})")
            return
        self._touch_host("ep0")
        self.log(f"EP0 rt=0x{rt:02x} req=0x{req:02x} val=0x{wValue:04x} idx=0x{wIndex:04x} len={wLength}")

        if (rt & USB_TYPE_MASK) == USB_TYPE_STANDARD:
            if req == USB_REQ_GET_DESCRIPTOR:
                dtype = wValue >> 8
                idx = wValue & 0xFF
                resp = b""
                if dtype == USB_DT_DEVICE:
                    resp = device_descriptor(self.cur_pid)
                elif dtype == USB_DT_CONFIG:
                    resp = config_descriptor()
                elif dtype == USB_DT_STRING:
                    if idx == 0:
                        resp = string_descriptor("", lang=True)
                    else:
                        resp = string_descriptor(STRINGS.get(idx, ""))
                elif dtype == USB_DT_DEVICE_QUALIFIER:
                    resp = qualifier_descriptor()
                elif dtype == USB_DT_OTHER_SPEED_CONFIG:
                    resp = config_descriptor()
                elif dtype == USB_DT_BOS:
                    self.rg.ep0_stall()
                    return
                else:
                    self.rg.ep0_stall()
                    return
                resp = resp[:wLength]
                self.rg.ep0_write(resp)
                return
            if req == USB_REQ_SET_CONFIGURATION:
                # data stage empty OUT
                try:
                    self.rg.ep0_read(0)
                except Exception:
                    pass
                self._set_configuration(wValue)
                return
            if req == USB_REQ_GET_CONFIGURATION:
                self.rg.ep0_write(bytes([self.config_value])[:wLength])
                return
            if req == USB_REQ_SET_INTERFACE:
                try:
                    self.rg.ep0_read(0)
                except Exception:
                    pass
                return
            if req == USB_REQ_GET_INTERFACE:
                self.rg.ep0_write(b"\x00"[:wLength])
                return
            if req == USB_REQ_SET_ADDRESS:
                # UDC handles address; just ack
                try:
                    self.rg.ep0_read(0)
                except Exception:
                    pass
                return
            if req == USB_REQ_GET_STATUS:
                self.rg.ep0_write(b"\x00\x00"[:wLength])
                return
            # clear/set feature etc.
            try:
                if not (rt & USB_DIR_IN) and wLength:
                    self.rg.ep0_read(wLength)
                elif not (rt & USB_DIR_IN):
                    self.rg.ep0_read(0)
                else:
                    self.rg.ep0_write(b"")
            except Exception:
                self.rg.ep0_stall()
            return

        # Vendor requests — the goggles (as AOA USB host) drive the accessory bring-up here.
        if (rt & USB_TYPE_MASK) == USB_TYPE_VENDOR:
            if req == AOA_GET_PROTOCOL and (rt & USB_DIR_IN):
                # Advertise AOA protocol support so the goggles proceed with SEND_STRING/START.
                self.log(f"AOA GET_PROTOCOL → v{AOA_PROTOCOL_VERSION}")
                self.rg.ep0_write(struct.pack("<H", AOA_PROTOCOL_VERSION)[:wLength])
                return
            if req == AOA_SEND_STRING and not (rt & USB_DIR_IN):
                # Host sends identity string idx wIndex; read + store, then ack.
                try:
                    s = self.rg.ep0_read(wLength) if wLength else b""
                except Exception:
                    s = b""
                self.aoa_strings[wIndex] = s
                self.log(f"AOA SEND_STRING[{wIndex}] = {s!r}")
                return
            if req == AOA_START and not (rt & USB_DIR_IN):
                # ACCESSORY_START: ack the (zero-length) control transfer, then re-enumerate
                # as the accessory PID. Deferred to the main loop so the status stage completes
                # before we drop off the bus.
                try:
                    self.rg.ep0_read(0)
                except Exception:
                    pass
                if self.mode != "accessory":
                    print("[*] AOA ACCESSORY_START — re-enumerating as accessory 18d1:2d01", flush=True)
                    self.pending_reenum = True
                return
            # Unknown vendor request — best-effort ack (AOA audio 0x3a/register 0x3c etc.).
            self.log(f"vendor req 0x{req:02x} — ack")
            try:
                if rt & USB_DIR_IN:
                    self.rg.ep0_write(b"\x00\x00"[:wLength])
                elif wLength:
                    self.rg.ep0_read(wLength)
                else:
                    self.rg.ep0_read(0)
            except Exception:
                self.rg.ep0_stall()
            return

        self.rg.ep0_stall()

    def _set_configuration(self, value: int):
        self.log(f"SET_CONFIGURATION {value}")
        self.config_value = value
        if value == 0:
            self.configured = False
            return
        self.rg.vbus_draw(500)
        self.rg.configure()
        # Enable bulk endpoints
        try:
            self.ep_out_num = self.rg.ep_enable(0x01, wMaxPacketSize=512)
            self.ep_in_num = self.rg.ep_enable(0x81, wMaxPacketSize=512)
            self.log(f"endpoints enabled out={self.ep_out_num} in={self.ep_in_num}")
        except OSError as e:
            print(f"[!] ep_enable failed: {e}")
            # try with maxpacket 64
            self.ep_out_num = self.rg.ep_enable(0x01, wMaxPacketSize=64)
            self.ep_in_num = self.rg.ep_enable(0x81, wMaxPacketSize=64)
            self.log(f"endpoints enabled (64) out={self.ep_out_num} in={self.ep_in_num}")
        self.configured = True
        # Host just configured us — seed liveness so the phantom watchdog doesn't
        # fire before the first bulk/EP0 transfer.
        self._touch_host("ep0")
        if self.mode != "accessory":
            # Normal (pre-accessory) mode: the goggles configure us, then drive the AOA
            # handshake. Do NOT seed or start the bulk reader yet — that belongs to the
            # accessory session established after ACCESSORY_START re-enumeration.
            self.log("configured in normal mode — awaiting AOA handshake")
            return
        # Start the bulk reader (receives goggles telemetry/video + answers identity probes)
        # and the gold-replay driver (feeds DJI Fly's real command stream) concurrently.
        # They touch different endpoints (reader=ep_out, driver=ep_in) so no contention.
        if self._reader is None or not self._reader.is_alive():
            self._stop = False
            self._reader = threading.Thread(target=self._bulk_reader, daemon=True)
            self._reader.start()
        if self._driver is None or not self._driver.is_alive():
            self._driver = threading.Thread(target=self._seed_handshake, daemon=True)
            self._driver.start()

    def _ep_in_write(self, framed: bytes, what: str = "") -> bool:
        """Locked + EAGAIN/EBUSY-retrying write to the bulk IN endpoint.

        The lock keeps the driver and reply-sender threads from colliding on the single
        raw-gadget IN request slot. The host (goggles) only has one outstanding bulk-IN
        at a time; submitting another while the previous is still queued returns
        -EAGAIN (11) or -EBUSY (16) from usb_ep_queue (dmesg: "fail, usb_ep_queue
        returned -11"). Field 2026-07-14: only retrying EBUSY left ~all gold keepalives
        failing with EAGAIN → bitty/choppy HDMI while mpv sat nearly idle. Retry both,
        with longer back-off, and rate-limit failure logs (per-packet spam drowned
        journal + burned CPU).
        """
        # errno 11 = EAGAIN / EWOULDBLOCK, 16 = EBUSY
        retryable = (11, 16)
        max_attempts = 40  # ~ up to ~200 ms with 5 ms sleeps
        with self._tx_lock:
            if self.ep_in_num is None or self.rg.fd < 0 or not self.configured:
                return False
            for attempt in range(max_attempts):
                try:
                    self.rg.ep_write(self.ep_in_num, framed)
                    self._tx_ok += 1
                    self._touch_host("tx")
                    return True
                except OSError as e:
                    if e.errno in retryable and attempt < max_attempts - 1:
                        # Linear-ish back-off: host poll interval is often a few ms.
                        time.sleep(0.002 + 0.001 * min(attempt, 8))
                        continue
                    self._tx_fail += 1
                    # Rate-limit: one summary line every 2s max (not per packet).
                    now = time.monotonic()
                    if now - self._tx_fail_log_t >= 2.0:
                        self.log(
                            f"ep_in write FAIL {what}: {e} "
                            f"(ok={self._tx_ok} fail={self._tx_fail} last_attempts={attempt + 1})"
                        )
                        self._tx_fail_log_t = now
                    return False
        return False

    def _send_app(self, cs, cid, st, si, rt, ri, ack, payload):
        """Build one app→goggles DUML with a fresh rolling seq and write it AOA-wrapped.
        payload may be a hex string (as stored in gold_app_seq) or raw bytes."""
        import duml as duml_mod
        if isinstance(payload, str):
            payload = bytes.fromhex(payload)
        self._seq = (self._seq + 1) & 0xFFFF
        pkt = duml_mod.build(
            sender_type=st, receiver_type=rt, seq=self._seq,
            cmd_set=cs, cmd_id=cid, payload=payload,
            sender_idx=si, receiver_idx=ri, pack_type=0, ack_type=ack,
        )
        return self._ep_in_write(aoa_wrap(pkt), f"{cs}/0x{cid:02x}")

    def _replay_arm(self, app_seq, label: str) -> int:
        """Replay the gold app→goggles arming burst (initial arm or hot-swap re-arm).

        Honors gold inter-packet gaps but caps long stalls so the burst finishes quickly.
        Returns the count of packets that TX'd ok. Bails early if we lose configuration.
        """
        # Inter-packet gap cap: gold capture spans ~4.7s. Field default 0.08s
        # (sleeps ~3.3s) is the proven arming cadence. 2026-08-30 tried 0.02s
        # (~1.5s sleeps) to cut plug-in wait; 2026-08-31 eyes-on: goggles did
        # not stay armed (8 never-ES re-arms in 70s → half-rate ES → USB
        # SUSPEND). Keep 0.02 as an explicit opt-in. 0 = firehose.
        gap_cap = _env_float("O3_ARM_GAP_CAP_S", 0.08)
        print(f"[*] replaying {label} ({len(app_seq)} pkts, gap_cap={gap_cap:.3f}s)",
              flush=True)
        prev = 0.0
        sent = 0
        for rel, tup in app_seq:
            if self._stop or not self.configured:
                break
            dt = rel - prev
            prev = rel
            if dt > 0 and gap_cap > 0:
                time.sleep(min(dt, gap_cap))
            sent += self._send_app(*tup)
        print(f"[*] {label} done ({sent}/{len(app_seq)} pkts)", flush=True)
        return sent

    def maybe_request_video_rearm(self) -> None:
        """Ask the seed thread to replay the arming burst when USB video-out is silent
        but the AOA host (goggles) is still present.

        Covers air-unit hot-swap (video flowed, drone power-cycled) AND cold
        USB-first (goggles plugged, drone not on yet — ES never started). Fires
        only while there is NO live video (safe vs the 2026-07-14 re-arm-while-
        streaming chop), only after the initial gold burst has finished, and only
        when the host is proven alive (recent keepalive TX or bulk RX). If BOTH TX
        and RX are long-dead it's a real/phantom unplug → left to that path.
        Re-asks every O3_VIDEO_REARM_PERIOD_S while silent so a still-booting
        drone eventually catches.
        """
        if not self._video_rearm_enable:
            return
        if not self.configured or self.mode != "accessory":
            return
        if self._expect_reenum_bus_cycle or self._phantom_rearm_reason is not None:
            return
        # SUSPEND usually means the cable is coming out. Do not fire APP_SEQ into
        # a dying bus (2026-08-31 reconnect: re-arm 6s after SUSPEND, then
        # DISCONNECT, then UDC EBUSY on re-init).
        now_pre = time.monotonic()
        if self._last_suspend_mono and (now_pre - self._last_suspend_mono) < 15.0:
            return
        # Initial gold burst must have finished (don't hammer APP_SEQ during it).
        # After that, re-arm even if video has NEVER flowed this session: USB
        # plugged, drone still off (owner 2026-08-30). Previously required
        # `_es_written > 0`, which meant "plug goggles first, power drone later"
        # from cold boot never re-subscribed USB video-out.
        if not self._initial_arm_done:
            return
        now = time.monotonic()
        never_es = self._es_written == 0
        if self._es_last_grow_mono > 0:
            silent = now - self._es_last_grow_mono
        else:
            silent = now - (self._initial_arm_mono or now)
        # If the stream RESPONDED to our most recent re-arm (ES grew after we asked), be
        # patient: give the presenter a long, quiet window to cold-start and lock onto the
        # resumed stream. Re-arming again now is exactly what destabilised the goggles'
        # encoder in the field (bursty garbage → presenter never locks → USB SUSPEND). Only
        # re-arm again if the stream stays TRULY dead for the full settle window.
        responded = (
            self._video_rearm_last_mono > 0
            and self._es_last_grow_mono > self._video_rearm_last_mono
        )
        # Never-ES (USB first, drone later): still re-arm so a late air-unit
        # power-on subscribes USB video-out, but do NOT use the 6s/8s hot-swap
        # cadence. 2026-08-31: eight 0.02s APP_SEQ bursts in 70s with no ES
        # reset the encoder and ended in USB SUSPEND. Wait a full settle
        # window after gold and between attempts.
        if never_es:
            threshold = self._video_rearm_settle_s
            period = self._video_rearm_settle_s
        else:
            threshold = self._video_rearm_settle_s if responded else self._video_silence_s
            period = self._video_rearm_period_s
        if silent < threshold:
            return
        # Host must still be present (AOA up): keepalive TX still succeeding, or recent RX.
        alive_window = threshold + 4.0
        host_alive = (
            (self._host_tx_ok_mono > 0 and now - self._host_tx_ok_mono < alive_window)
            or (self._host_rx_mono > 0 and now - self._host_rx_mono < alive_window)
        )
        if not host_alive:
            return
        # Rate-limit re-arm requests (the drone may still be booting after power-on; never
        # hammer — that resets the encoder and eventually forces a USB SUSPEND).
        if self._video_rearm_last_mono and (
            now - self._video_rearm_last_mono
        ) < period:
            return
        self._video_rearm_last_mono = now
        if never_es:
            why = "never-ES settle"
        elif responded:
            why = "post-resume settle expired"
        else:
            why = "no resume yet"
        print(
            f"[*] video silent {silent:.0f}s, AOA host alive (tx_ok={self._tx_ok}) — "
            f"video re-arm (hot-swap; {why})",
            flush=True,
        )
        # Rebuild the deframer before the resumed stream is parsed (drop the stale
        # mid-frame partial from the pre-drone-off stream).
        self._deframer_reset_evt.set()
        self._rearm_video_event.set()

    def _seed_handshake(self):
        """Drive the session by replaying DJI Fly's REAL app→goggles command stream from the
        gold capture (poc/pi_endpoint/gold_app_seq.py), then loop a LIGHT keepalive set.

        Field 2026-07-14: replaying the full APP_SEQ every ~1s re-armed the camera path and
        ES rate oscillated 8→0 Mbit/s (choppy) even with healthy USB TX and hdmi_drop=0.
        Arm once with full gold; sustain with SUSTAIN_SEQ (heartbeats + 2/0xeb + version pings).
        """
        try:
            time.sleep(0.3)
            if not self.configured or self.ep_in_num is None:
                print(f"[!] seed skip configured={self.configured} ep_in={self.ep_in_num}", flush=True)
                return
            try:
                from gold_app_seq import APP_SEQ, SUSTAIN_SEQ
            except Exception as e:
                print(f"[!] gold_app_seq import failed: {e!r} — falling back to version inquiries", flush=True)
                APP_SEQ, SUSTAIN_SEQ = self._fallback_seq()

            self._replay_arm(APP_SEQ, "gold app→goggles arming burst")
            self._initial_arm_done = True
            self._initial_arm_mono = time.monotonic()
            print(
                f"[*] gold arm done — sustaining with light keepalives "
                f"({len(SUSTAIN_SEQ)} pkts / ~1s)",
                flush=True,
            )

            loops = 0
            while not self._stop and self.configured:
                # Hot-swap video-resume re-arm (2026-07-16 P0): the air unit was
                # power-cycled while the goggles stayed plugged; ES went silent but the
                # AOA host is still up. SUSTAIN_SEQ alone won't re-subscribe video — replay
                # the full arming burst (requested by the video-silence watchdog).
                if self._rearm_video_event.is_set():
                    self._rearm_video_event.clear()
                    self._video_rearm_count += 1
                    self._replay_arm(
                        APP_SEQ, f"video-resume re-arm burst #{self._video_rearm_count}"
                    )
                prev = 0.0
                for rel, tup in SUSTAIN_SEQ:
                    if self._stop or not self.configured:
                        break
                    if self._rearm_video_event.is_set():
                        break  # service the re-arm request promptly
                    dt = rel - prev
                    prev = rel
                    if dt > 0:
                        # Full gaps for sustain (period is only ~1s) — do not compress.
                        time.sleep(min(dt, 0.5))
                    self._send_app(*tup)
                loops += 1
                if self.verbose and loops % 10 == 0:
                    print(f"[*] sustain loop {loops} (light keepalives)", flush=True)
        except Exception as e:
            print(f"[!] seed driver crashed: {e!r}", flush=True)

    @staticmethod
    def _fallback_seq():
        """Version-inquiry-only fallback if gold_app_seq.py is missing (old behavior)."""
        arm = [(0.0, (0, 1, 2, 0, rt, ri, 2, "")) for (rt, ri) in
               [(14, 0), (31, 0), (2, 5), (0, 0), (1, 0), (9, 0), (28, 0)]]
        sustain = [
            (0.0, (0, 1, 2, 0, 14, 0, 2, "")),
            (0.0, (0, 1, 2, 0, 31, 0, 2, "")),
            (0.5, (0, 1, 2, 0, 2, 5, 2, "")),
            (1.0, (0, 1, 2, 0, 0, 0, 2, "")),
        ]
        return arm, sustain

    def _bulk_reader(self):
        print("[*] bulk reader running — waiting for goggles data", flush=True)
        while not self._stop and self.configured:
            if self.rg.fd < 0 or self.ep_out_num is None:
                break
            try:
                data = self.rg.ep_read(self.ep_out_num, 4096)
            except OSError as e:
                if self.verbose:
                    self.log(f"ep_read: {e}")
                time.sleep(0.01)
                continue
            if not data:
                time.sleep(0.002)
                continue
            self._touch_host("rx")
            self._on_bulk_out(data)

    def _on_bulk_out(self, data: bytes):
        # 0) HOT-SWAP RESUME: a re-arm was fired while video was silent. Rebuild the deframer
        #    (and re-align the player to a fresh SPS) BEFORE parsing these resumed bytes, so
        #    the stale mid-frame partial from the pre-drone-off stream can't splice onto the
        #    new stream. This runs in the reader thread → no race with deframer.feed(). It
        #    fires on the first bulk packet after re-arm (control replies during silence), so
        #    by the time real video resumes the deframer is already clean.
        if self._deframer_reset_evt.is_set():
            self._deframer_reset_evt.clear()
            self.deframer = StreamingVideoDeframer(retain_payloads=False)
            self.aoa_buf = bytearray()
            self._vid_pending = []
            self._vid_started = False
            if self.player is not None:
                try:
                    self.player.notify_stream_restart("air-unit hot-swap re-arm")
                except Exception as e:
                    print(f"[!] player stream-restart notify: {e!r}", flush=True)
            print("[*] hot-swap: deframer rebuilt for resumed stream (dropped stale partial)",
                  flush=True)
        # 1) VIDEO: feed the deframer and write ONLY the newly completed chunks it returns —
        #    never rebuild the whole elementary stream per 4KB chunk (that O(n²) rebuild is
        #    what made the Python reader fall behind and the kernel drop URBs → frame loss).
        completed = self.deframer.feed(data)
        if completed:
            self._vid_chunks += len(completed)
            self._write_video(completed)

        # 2) CONTROL DUML: only for control-channel data. Video URBs (magic 55 cc 4a 57 or raw
        #    frame continuations) must NOT enter the DUML path — that was the throughput killer.
        #    Control rides 55 cc 30 75 (AOA_MAGIC) in the standalone session; gate strictly on it.
        if AOA_MAGIC in data:
            self.aoa_buf.extend(data)
            for pkt in list(aoa_unwrap_stream(self.aoa_buf)):
                self._handle_duml(pkt)

        # periodic stats (time-based, cheap) instead of a per-chunk log line
        now = time.time()
        if self.verbose and now - self._stat_t >= 1.0:
            self._stat_t = now
            # Instantaneous ES rate from delta of counters (helps spot stalls vs bitty).
            prev_es = getattr(self, "_stat_es", 0)
            prev_t = getattr(self, "_stat_wall", now)
            dt = max(now - prev_t, 1e-3)
            mbit = 8.0 * (self._es_written - prev_es) / dt / 1e6
            self._stat_es = self._es_written
            self._stat_wall = now
            hdmi_drop = self.player._dropped if self.player is not None else 0
            st = self.deframer.stats
            drop_tot = self.player._drop_total if self.player is not None else 0
            # resyncs = true garbage skips; aoa = control frames demuxed cleanly.
            # hdmi_drop_tot = cumulative SPS re-align discards (should stay ~0 when flyable).
            # pres = AU feed rate vs mpv vf_fps/drops (chop diagnosis 2026-07-14 night).
            pres = ""
            if self.player is not None:
                try:
                    pres = " " + self.player.presentation_stats_line()
                except Exception:
                    pres = ""
            print(
                f"[*] video chunks={self._vid_chunks} es_bytes={self._es_written} "
                f"~{mbit:.2f} Mbit/s buffered={st['buffered']} "
                f"resyncs={st['resyncs']} aoa={st.get('aoa_peeled', 0)} "
                f"badlen={st.get('bad_length', 0)} garb={st.get('garbage_bytes', 0)} "
                f"tx_ok={self._tx_ok} tx_fail={self._tx_fail} "
                f"hdmi_drop={hdmi_drop} drop_tot={drop_tot}{pres}",
                flush=True,
            )

    def _emit(self, data: bytes):
        """Fan out one decodable ES span (SPS-aligned at start) to every sink: the capture
        file and/or the live HDMI player. Both are non-blocking w.r.t. the reader thread."""
        if self.out_fh:
            self.out_fh.write(data)
        if self.player is not None:
            self.player.feed(data)

    def _write_video(self, completed):
        """Write newly completed video chunks incrementally, starting from the first SPS so the
        file is decodable from its head. O(1) amortized per chunk."""
        _es_before = self._es_written
        if self._vid_started:
            for c in completed:
                self._es_written += len(c)
                self._emit(c)
        else:
            self._vid_pending.extend(completed)
            # Once an SPS has arrived, join pending ONCE (small — SPS comes early), write from
            # the exact SPS byte offset (matches batch elementary_from_sps), then switch to
            # direct per-chunk writes. The join happens a single time, not per chunk.
            if self.deframer.stats["has_sps"]:
                joined = b"".join(self._vid_pending)
                pos = _find_sps(joined)
                if pos is not None:
                    self._emit(joined[pos:])
                    self._es_written += len(joined) - pos
                    self._vid_started = True
                    self._vid_pending = []
        # Track live-video liveness (drives the hot-swap video-resume re-arm).
        if self._es_written != _es_before:
            self._es_last_grow_mono = time.monotonic()
        # flush occasionally, not every 4KB
        self._flush_ctr += 1
        if self.out_fh and self._flush_ctr % 32 == 0:
            self.out_fh.flush()

    def _handle_duml(self, pkt: bytes):
        replies = maybe_handle_duml(pkt)
        for rep in replies:
            if self._ep_in_write(aoa_wrap(rep), f"reply 0x{rep[10]:02x}"):
                # Identity probes fire many times/sec; don't flood the journal.
                pass

    def close(self):
        self._stop = True
        if self.player is not None:
            self.player.close()
        if self.out_fh:
            self.out_fh.close()
            print(f"[*] closed output, es_bytes≈{self._es_written}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--driver", default="fe980000.usb")
    ap.add_argument("--device", default="fe980000.usb")
    ap.add_argument("--out", default="/tmp/o3live.h264",
                    help="H.264 ES capture file; pass '' to skip recording (HDMI-only)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--cold-2d01", action="store_true",
                    help="skip the AOA handshake and present 2d01 directly (old behavior, A/B)")
    ap.add_argument("--hdmi", action="store_true",
                    help="decode the live stream to the Pi's HDMI port(s) (mpv / gstreamer)")
    ap.add_argument("--drm-device", default="/dev/dri/card1",
                    help="DRM device for HDMI out (vc4-kms-v3d display node)")
    ap.add_argument("--drm-connector", default="auto",
                    help="HDMI target(s): 'auto' = every connected HDMI-A-* (hotplug-aware); "
                         "or explicit 'HDMI-A-1', 'HDMI-A-2', 'HDMI-A-1,HDMI-A-2'")
    ap.add_argument("--player-cmd", default=None,
                    help="override the decoder command entirely (reads H.264 ES from stdin); "
                         "disables auto dual-HDMI / hotplug")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("need root (sudo)")

    player = None
    if args.hdmi:
        fixed = args.player_cmd.split() if args.player_cmd else None
        player = HdmiPlayer(
            drm_device=args.drm_device,
            connector_spec=args.drm_connector,
            verbose=args.verbose,
            fixed_cmd=fixed,
        )

    mode = "cold 2d01 (no handshake)" if args.cold_2d01 else "AOA handshake → 2d01"
    print(f"[*] raw-gadget accessory on {args.device} — bring-up: {mode}")
    if args.out:
        print(f"[*] writing H.264 ES to {args.out}")
    if player:
        if args.player_cmd:
            print(f"[*] HDMI live decode → fixed player-cmd on {args.drm_device}")
        else:
            print(f"[*] HDMI live decode → connector={args.drm_connector!r} "
                  f"on {args.drm_device} (single=mpv, multi=dual_drm_mirror)")
    print("[*] plug goggles into Pi USB-C (VBUS isolated). Ctrl-C to stop.")

    if player:
        player.start()
    rg = RawGadget(args.driver, args.device)
    sess = AccessorySession(rg, args.out, args.verbose, cold_2d01=args.cold_2d01, player=player)

    # Field heartbeat + phantom-host watchdog. Main loop blocks on event_fetch, so
    # both run from side threads. Phantom: UDC stays 'configured' after unplug with
    # no DISCONNECT — gold sustain then EAGAIN-spams forever (field 2026-07-14).
    stop_hb = threading.Event()
    # Seconds with zero host proof (bulk RX / successful IN / EP0) before we declare
    # the host gone. Live video produces bulk RX continuously; keepalives produce TX ok.
    PHANTOM_SILENCE_S = float(os.environ.get("O3_PHANTOM_SILENCE_S", "8"))

    def _usb_wait_heartbeat():
        while not stop_hb.wait(20.0):
            if sess._es_written > 0:
                return  # video flowing; stop nagging
            udc = _udc_state(args.device)
            mode = getattr(sess, "mode", "?")
            print(
                f"[*] still waiting for goggles USB video "
                f"(udc={udc}, mode={mode}, es_bytes=0, configured={sess.configured}) "
                f"— plug goggles into Pi USB-C (VBUS-isolated); "
                f"goggles internal video alone does not fill HDMI "
                f"(note: udc=configured ≠ host present)",
                flush=True,
            )

    def _host_liveness_watchdog():
        """Detect phantom unplug: session thinks it's configured, UDC may say
        'configured', but no host traffic for PHANTOM_SILENCE_S seconds."""
        while not stop_hb.wait(2.0):
            if sess._expect_reenum_bus_cycle:
                continue
            if sess._phantom_rearm_reason is not None:
                continue
            if not sess.configured:
                continue
            now = time.monotonic()
            last = sess._host_activity_mono
            if last <= 0:
                continue
            silent = now - last
            if silent < PHANTOM_SILENCE_S:
                continue
            # Quiet can be OK briefly mid-handshake; require either long silence or
            # the classic dead-host signature (IN writes failing, no bulk RX).
            no_rx = (sess._host_rx_mono <= 0) or (now - sess._host_rx_mono >= PHANTOM_SILENCE_S)
            no_tx_ok = (sess._host_tx_ok_mono <= 0) or (
                now - sess._host_tx_ok_mono >= PHANTOM_SILENCE_S
            )
            failing = sess._tx_fail > max(sess._tx_ok, 1) * 3 or (
                sess._tx_fail >= 30 and sess._tx_ok == 0
            )
            long_dead = silent >= max(PHANTOM_SILENCE_S * 2, 15.0)
            if not ((no_rx and no_tx_ok and (failing or long_dead)) or long_dead):
                continue
            sess.signal_phantom_unplug(
                f"phantom unplug (silent {silent:.0f}s, "
                f"tx_ok={sess._tx_ok} tx_fail={sess._tx_fail}, "
                f"udc={_udc_state(args.device)})"
            )

    def _video_resume_watchdog():
        """Air-unit hot-swap: when the drone is power-cycled with goggles still plugged,
        USB video-out stops but the AOA link stays up. Poll for that silence and ask the
        seed thread to re-fire the arming burst so video resumes without a cable unplug."""
        while not stop_hb.wait(1.0):
            try:
                sess.maybe_request_video_rearm()
            except Exception as e:
                print(f"[!] video-resume watchdog: {e!r}", flush=True)

    hb = threading.Thread(target=_usb_wait_heartbeat, name="usb-wait-hb", daemon=True)
    hb.start()
    wd = threading.Thread(target=_host_liveness_watchdog, name="host-live-wd", daemon=True)
    wd.start()
    vrw = threading.Thread(target=_video_resume_watchdog, name="video-resume-wd", daemon=True)
    vrw.start()

    def _do_rearm(reason: str):
        try:
            sess.rearm_for_next_plug(reason)
        except OSError:
            # Gadget wedged — exit non-zero so systemd Restart=on-failure picks us up
            # (only safe after the host is already gone).
            raise SystemExit(1)

    try:
        while True:
            # Phantom path may have closed the fd to wake us; rearm before next fetch.
            if rg.fd < 0:
                reason = sess.take_phantom_rearm_reason() or "gadget fd closed"
                _do_rearm(reason)
                continue
            try:
                typ, data = rg.event_fetch()
            except OSError as e:
                reason = sess.take_phantom_rearm_reason()
                if reason is not None or rg.fd < 0:
                    _do_rearm(reason or f"event_fetch after host loss: {e}")
                    continue
                # Transient ioctl error with fd still open — log and retry briefly.
                print(f"[!] event_fetch: {e}", flush=True)
                time.sleep(0.05)
                if rg.fd < 0:
                    continue
                raise
            if typ == USB_RAW_EVENT_CONNECT:
                # After our intentional AOA re-enum the next CONNECT is expected.
                if sess._expect_reenum_bus_cycle:
                    sess._expect_reenum_bus_cycle = False
                sess._touch_host("ep0")
                print(f"[*] CONNECT (mode={sess.mode})", flush=True)
            elif typ == USB_RAW_EVENT_CONTROL:
                sess.handle_control(data)
                if sess.pending_reenum:
                    # AOA normal→accessory transition: flip PID and re-enumerate the gadget.
                    sess.pending_reenum = False
                    sess.configured = False
                    sess.mode = "accessory"
                    sess._expect_reenum_bus_cycle = True
                    rg.reinit()
                    print("[*] re-enumerated; presenting accessory descriptors", flush=True)
            elif typ == USB_RAW_EVENT_RESET:
                print("[*] event RESET", flush=True)
                sess.on_bus_reset()
            elif typ == USB_RAW_EVENT_DISCONNECT:
                print("[*] event DISCONNECT", flush=True)
                # Real unplug / goggles power-off: full re-arm so the next plug works
                # without restarting the process (systemd power-cycle friendly).
                _do_rearm("disconnect")
            elif typ == USB_RAW_EVENT_SUSPEND:
                # Often precedes a silent unplug that never delivers DISCONNECT.
                print("[*] event SUSPEND", flush=True)
                sess.log("host suspend")
                sess._last_suspend_mono = time.monotonic()
            elif typ == USB_RAW_EVENT_RESUME:
                print("[*] event RESUME", flush=True)
                sess._last_suspend_mono = 0.0
                sess._touch_host("ep0")
            else:
                sess.log(f"event type={typ} len={len(data)}")
    except KeyboardInterrupt:
        print("\n[*] stop")
    finally:
        stop_hb.set()
        sess.close()
        rg.close()
        print(f"[*] final video stats: {sess.deframer.stats}")
        if sess._es_written and args.out:
            print(f"[*] play: ffplay -f h264 {args.out}")


if __name__ == "__main__":
    main()
