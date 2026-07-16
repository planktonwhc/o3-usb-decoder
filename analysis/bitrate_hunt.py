#!/usr/bin/env python3
"""Hunt USB live-view quality / bitrate control in a pi-mitm payload log.

Goal: find app→goggles DUML SETs that could raise the ~7 Mbit/s H.264 ES
bitrate of the AOA consumer path. Designed for the gold natural-flow capture
and any future quality-delta MITM logs.

What it reports
  - Unique app→goggles REQUESTS, with early (< window) vs late first-seen
  - Focus sets: CAMERA(2), WIFI(7), DM368(8), OFDM/HDLnk(9)
  - Full-session HDLnk inventory (usually goggles/air → app telemetry only)
  - Camera 2/0xeb + 2/0xe8 subscribe/keepalive variants
  - Camera 2/0x80 status-push field stability (device → app)
  - Optional: compare two captures for app→goggles SET deltas
  - Optional: measure elementary-stream Mbit/s from a .h264 file

Usage:
  python3 analysis/bitrate_hunt.py captures/pi_pull/2026-07-13_103110_pi-mitm_natural_payload.log
  python3 analysis/bitrate_hunt.py gold.log --window 6 --es captures/foo.h264
  python3 analysis/bitrate_hunt.py base.log --diff other.log
"""
from __future__ import annotations

import argparse
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import duml  # noqa: E402
from aoa_capture_parse import deframe_aoa, load_bytes_from_proxylog  # noqa: E402

# Sets most likely to carry live-view / encode / link config.
FOCUS_SETS = {2, 7, 8, 9}
APP_DIRS = frozenset({"IN", "TXG"})  # phone spoke / delivered to goggles
DEVICE_DIRS = frozenset({"OUT", "TXP"})  # goggles spoke / delivered to phone
VIDEO_MAGIC = bytes.fromhex("55cc4a57")


def _es_bitrate_mbps(path: Path) -> tuple[float, int, float | None]:
    """Return (mbps, size_bytes, duration_s_or_None).

    Duration from ffprobe if available; else None (caller can pass --es-duration).
    """
    size = path.stat().st_size
    duration = None
    try:
        import json
        import subprocess

        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            text=True,
            timeout=60,
        )
        duration = float(json.loads(out)["format"]["duration"])
    except Exception:
        pass
    if duration and duration > 0:
        return size * 8 / duration / 1e6, size, duration
    return float("nan"), size, duration


def iter_duml(records):
    """Yield (rel_ts, direction, parsed) with base_ts = first record ts."""
    if not records:
        return
    base = records[0][0]
    for ts, direction, payload in records:
        rel = ts - base
        for _off, _pkt, parsed in deframe_aoa(payload):
            yield rel, direction, parsed


def collect(records, window: float):
    """Build analysis tables from proxy log records."""
    base = records[0][0] if records else 0.0
    span = (records[-1][0] - base) if records else 0.0

    # unique app→goggles requests: key → {first, count}
    app_req: dict[tuple, dict] = {}
    # all (cs,cid,pt) histogram
    hist: Counter = Counter()
    # HDLnk (cs=9) samples
    hdl: list[tuple] = []
    # focus-set unique app reqs
    focus_app: dict[tuple, dict] = {}
    # camera 0x80 payloads (device push)
    cam80: list[bytes] = []
    # bulk video-frame bytes (OUT lines containing video magic)
    bulk: list[tuple[float, int]] = []

    n_duml = 0
    for rel, direction, p in iter_duml(records):
        n_duml += 1
        hist[(p["cmd_set"], p["cmd_id"], p["packet_type"])] += 1

        if direction in DEVICE_DIRS and p["cmd_set"] == 2 and p["cmd_id"] == 0x80:
            cam80.append(p["payload"])

        if p["cmd_set"] == 9:
            hdl.append((rel, direction, p))

        if direction in APP_DIRS and p["packet_type"] == 0:
            key = (
                p["cmd_set"],
                p["cmd_id"],
                p["sender_type"],
                p["sender_idx"],
                p["receiver_type"],
                p["receiver_idx"],
                p["ack_type"],
                p["payload"],
            )
            if key not in app_req:
                app_req[key] = {"first": rel, "count": 0}
            app_req[key]["count"] += 1
            if p["cmd_set"] in FOCUS_SETS:
                if key not in focus_app:
                    focus_app[key] = {"first": rel, "count": 0}
                focus_app[key]["count"] += 1

    for ts, direction, payload in records:
        if direction in DEVICE_DIRS and VIDEO_MAGIC in payload:
            bulk.append((ts - base, len(payload)))

    early_keys = {k for k, v in app_req.items() if v["first"] < window}
    late_new = {
        k: v for k, v in app_req.items() if k not in early_keys
    }

    return {
        "base": base,
        "span": span,
        "n_records": len(records),
        "n_duml": n_duml,
        "hist": hist,
        "app_req": app_req,
        "focus_app": focus_app,
        "early_keys": early_keys,
        "late_new": late_new,
        "hdl": hdl,
        "cam80": cam80,
        "bulk": bulk,
        "window": window,
    }


def _fmt_key(key) -> str:
    cs, ci, st, si, rt, ri, ack, pl = key
    name = duml.CMD_SET_NAMES.get(cs, "?")
    plh = pl.hex()
    if len(plh) > 96:
        plh = plh[:96] + "..."
    return (
        f"cs={cs}/0x{ci:02x} ({name:12s}) "
        f"s={st}.{si}->r={rt}.{ri} ack={ack} pl={plh}"
    )


def report(data, title: str = ""):
    w = data["window"]
    print("=" * 72)
    print(f"BITRATE / QUALITY HUNT{(' — ' + title) if title else ''}")
    print("=" * 72)
    print(
        f"records={data['n_records']}  duml={data['n_duml']}  "
        f"span={data['span']:.1f}s  early_window={w:.1f}s"
    )
    print(
        f"unique app→goggles REQs: {len(data['app_req'])}  "
        f"(first-seen <{w:.0f}s: {len(data['early_keys'])}; "
        f"first-seen ≥{w:.0f}s only: {len(data['late_new'])})"
    )

    # --- focus sets ---
    print(f"\n--- FOCUS app→goggles REQs (cs in {sorted(FOCUS_SETS)}) ---")
    items = sorted(data["focus_app"].items(), key=lambda kv: kv[1]["first"])
    if not items:
        print("  (none)")
    for key, info in items:
        tag = "EARLY" if info["first"] < w else "LATE "
        print(
            f"  {tag} t+{info['first']:7.3f}s n={info['count']:4d}  {_fmt_key(key)}"
        )

    # --- HDLnk ---
    print(f"\n--- OFDM/HDLnk cs=9 (all directions) n={len(data['hdl'])} ---")
    app_hdl_req = [
        x
        for x in data["hdl"]
        if x[1] in APP_DIRS and x[2]["packet_type"] == 0
    ]
    print(f"  app→goggles HDLnk REQs: {len(app_hdl_req)}")
    if app_hdl_req:
        for rel, d, p in app_hdl_req[:40]:
            print(
                f"    t+{rel:7.3f}s {d} cid=0x{p['cmd_id']:02x} "
                f"s={p['sender_type']}->r={p['receiver_type']} "
                f"pl={p['payload'].hex()}"
            )
    else:
        print("  (zero — DJI Fly did not SET HDLnk in this capture)")

    # fingerprint device→app HDLnk
    fp = Counter()
    for rel, d, p in data["hdl"]:
        if d in DEVICE_DIRS:
            fp[
                (
                    p["cmd_id"],
                    p["sender_type"],
                    p["receiver_type"],
                    p["packet_type"],
                    p["payload"][:4].hex(),
                )
            ] += 1
    print("  device→app HDLnk fingerprints:")
    for (cid, st, rt, pt, pl4), n in fp.most_common(12):
        print(
            f"    n={n:4d} cid=0x{cid:02x} s={st}->r={rt} pt={pt} pl[:4]={pl4}"
        )

    # --- late-new non-focus summary (collapse high-churn) ---
    print(f"\n--- NEW app→goggles REQs after t+{w:.0f}s (not in early window) ---")
    late_by_cmd: dict[tuple, list] = defaultdict(list)
    for key, info in data["late_new"].items():
        late_by_cmd[(key[0], key[1])].append((key, info))
    if not late_by_cmd:
        print("  (none)")
    else:
        for (cs, ci), group in sorted(
            late_by_cmd.items(), key=lambda kv: min(g[1]["first"] for g in kv[1])
        ):
            name = duml.CMD_SET_NAMES.get(cs, "?")
            n_unique = len(group)
            n_total = sum(g[1]["count"] for g in group)
            first = min(g[1]["first"] for g in group)
            # show full detail only for focus or rare cmds; else summarize
            if cs in FOCUS_SETS or n_unique <= 3:
                for key, info in sorted(group, key=lambda g: g[1]["first"])[:12]:
                    print(
                        f"  t+{info['first']:7.3f}s n={info['count']:4d}  {_fmt_key(key)}"
                    )
            else:
                print(
                    f"  t+{first:7.3f}s  cs={cs}/0x{ci:02x} ({name:12s}) "
                    f"{n_unique} unique payloads × {n_total} total "
                    f"(churn — likely telemetry/heartbeat, not a one-shot SET)"
                )

    # --- camera 0x80 status ---
    print(f"\n--- CAMERA 2/0x80 status push (device→app) n={len(data['cam80'])} ---")
    if data["cam80"]:
        pl0 = data["cam80"][0]
        print(f"  payload_len={len(pl0)}  first={pl0.hex()}")
        # u32 stability
        nfields = max(0, (len(pl0) - 3) // 4)
        for i in range(min(nfields, 12)):
            vals = Counter(
                struct.unpack_from("<I", pl, i * 4)[0]
                for pl in data["cam80"]
                if len(pl) >= i * 4 + 4
            )
            if len(vals) == 1:
                v = next(iter(vals))
                print(f"  u32[{i}] always 0x{v:08x} ({v})")
            else:
                sample = sorted(vals.keys())[:6]
                print(f"  u32[{i}] {len(vals)} unique e.g. {sample}")
    else:
        print("  (none)")

    # --- bulk rate windows (USB framing, not ES) ---
    print("\n--- goggles→app bulk (55 CC 4A 57) ~5s windows (framing, not ES) ---")
    bulk = data["bulk"]
    if not bulk:
        print("  (no video-magic transfers)")
    else:
        t0, t1 = bulk[0][0], bulk[-1][0]
        t = 0.0
        win = 5.0
        while t < t1:
            total = sum(n for rt, n in bulk if t <= rt < t + win)
            if total:
                print(
                    f"  t+{t:5.1f}-{t + win:5.1f}s  "
                    f"{total * 8 / win / 1e6:6.2f} Mbit/s bulk"
                )
            t += win
        print(f"  samples={len(bulk)} span={t1 - t0:.1f}s")

    # --- verdict helpers ---
    print("\n--- INTERPRETATION HINTS ---")
    late_focus = [
        (k, v) for k, v in data["focus_app"].items() if v["first"] >= w
    ]
    # 2/0xeb with only mid-byte churn is keepalive, not a quality SET
    late_eb_only = all(k[0] == 2 and k[1] == 0xEB for k, _ in late_focus)
    if not app_hdl_req and (not late_focus or late_eb_only):
        print(
            "  No app HDLnk SETs; late focus traffic is only camera 0xeb keepalive\n"
            "  variants (or empty). Steady-state after arming has no quality SET.\n"
            "  A post-start bitrate command is NOT present in this capture."
        )
    elif app_hdl_req:
        print("  App sent HDLnk — inspect payloads above as inject candidates.")
    else:
        print(
            f"  {len(late_focus)} late focus-set uniques — inspect as inject candidates."
        )
    print()


def diff_app_reqs(base_data, other_data, label_a="A", label_b="B"):
    """Print app→goggles REQ keys present in only one capture."""
    a = set(base_data["app_req"])
    b = set(other_data["app_req"])
    only_a = a - b
    only_b = b - a
    print("=" * 72)
    print(f"DIFF app→goggles REQs: only in {label_a}={len(only_a)}  "
          f"only in {label_b}={len(only_b)}")
    print("=" * 72)

    def show(keys, data, tag):
        # prefer focus sets first
        ordered = sorted(
            keys,
            key=lambda k: (0 if k[0] in FOCUS_SETS else 1, data["app_req"][k]["first"]),
        )
        for key in ordered[:80]:
            info = data["app_req"][key]
            focus = "*" if key[0] in FOCUS_SETS else " "
            print(
                f"  {focus}{tag} t+{info['first']:7.3f}s n={info['count']:4d}  "
                f"{_fmt_key(key)}"
            )
        if len(ordered) > 80:
            print(f"  ... ({len(ordered) - 80} more)")

    print(f"\n--- only in {label_a} (focus marked *) ---")
    show(only_a, base_data, label_a)
    print(f"\n--- only in {label_b} (focus marked *) ---")
    show(only_b, other_data, label_b)
    print()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("capture", help="usb-proxy natural/payload log")
    ap.add_argument(
        "--window",
        type=float,
        default=6.0,
        help="seconds treated as arming window (default 6, matches gold_app_seq)",
    )
    ap.add_argument(
        "--diff",
        default=None,
        help="second payload log — report app→goggles REQ set difference",
    )
    ap.add_argument(
        "--es",
        default=None,
        help="optional .h264 elementary stream to measure Mbit/s",
    )
    ap.add_argument(
        "--es-duration",
        type=float,
        default=None,
        help="override ES duration seconds if ffprobe unavailable",
    )
    args = ap.parse_args()

    path = Path(args.capture)
    if not path.exists():
        sys.exit(f"no such file: {path}")

    print(f"[*] loading {path} ...")
    records = load_bytes_from_proxylog(path)
    print(f"[*] {len(records)} transfers")
    data = collect(records, args.window)
    report(data, title=path.name)

    if args.diff:
        path2 = Path(args.diff)
        if not path2.exists():
            sys.exit(f"no such file: {path2}")
        print(f"[*] loading diff capture {path2} ...")
        rec2 = load_bytes_from_proxylog(path2)
        data2 = collect(rec2, args.window)
        report(data2, title=path2.name)
        diff_app_reqs(data, data2, label_a=path.name[:20], label_b=path2.name[:20])

    if args.es:
        es_path = Path(args.es)
        if not es_path.exists():
            sys.exit(f"no such file: {es_path}")
        mbps, size, dur = _es_bitrate_mbps(es_path)
        if args.es_duration is not None:
            dur = args.es_duration
            mbps = size * 8 / dur / 1e6
        print("--- ES bitrate ---")
        print(f"  file={es_path}  size={size}  duration={dur}")
        if dur:
            print(f"  ≈ {mbps:.3f} Mbit/s elementary stream")
        else:
            print("  (duration unknown — pass --es-duration)")
        print()


if __name__ == "__main__":
    main()
