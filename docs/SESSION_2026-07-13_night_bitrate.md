# Session — 2026-07-13 night: USB ES bitrate hunt

## Goal
Raise ~7 Mbit/s H.264 USB live-view ES (from evening handoff).

## Done (offline, no hardware)

1. Built `analysis/bitrate_hunt.py` — full gold payload DUML quality hunt + optional
   capture diff + ES measure.
2. Ran on `2026-07-13_103110_pi-mitm_natural_payload.log` (~87s working phone FPV).
3. Measured phone vs standalone ES bitrates.
4. Checked CosmoStreamer public statements on Goggles 2 USB bitrate.

## Results

| Check | Result |
|---|---|
| App→goggles HDLnk (cs=9) in gold | **0** |
| Post-6s quality SET (cs=2/7/8/9) | **none** (only `2/0xeb` keepalive churn) |
| Phone gold ES | **~6.94 Mbit/s** |
| Phase B standalone ES | **~6.91 Mbit/s** |
| CosmoStreamer (public) | Goggles 2 USB out **~8–10 Mbps**, “far too low” |

**Verdict:** ~7 Mbit/s is almost certainly the **product ceiling** for the USB/AOA
consumer encode. Standalone is not missing a quality command that the phone uses.

## Docs updated
- `docs/protocol_notes.md` — full section
- `docs/GROK_STATE.md`, `CLAUDE.md` next tasks

## Not done (optional, needs owner + hardware)
- UI quality-toggle MITM delta capture
- Blind post-stream HDLnk inject (deprioritized — not in gold)

## Suggested next product work
Network re-stream, arming-set trim, open-source packaging — not more player polish.
