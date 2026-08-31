# Contributing

Thanks for helping turn this into the open “voc-poc for O3.”

## What we want

- Bug fixes and reliability improvements on the **shipping** path:
  `poc/pi_endpoint/raw_endpoint.py`, `atomic_drm_present.py`, deframer,
  install/safe-stop/event scripts.
- Reports of **firmware / goggle / air-unit** combinations that work or break
  (include versions, journal snippets, whether a reboot was required).
- Documentation fixes, clearer setup steps, translations later.
- Optional: dual-HDMI, network re-stream, arming-sequence trim — discuss first.

## What we do not want (please don’t open a PR for)

- Redistributed **DJI Fly APKs**, `.so` libraries, firmware blobs, or phone
  factory images.
- Capture dumps that contain someone else’s personal data or full multi-GB
  raw USB logs (summaries and small fixtures are fine).
- “Crack / bypass / unlock DJI” framing, account hacking, or anything that
  targets other people’s devices without consent.
- Drive-by re-enable of **mpv** as the field default (atomic-KMS is shipping;
  see project history).
- Turning dual HDMI off in the boot unit without a reason (it is the shipping
  default; single-port override is in `docs/DUAL_HDMI_PARKED.md`).

## How to develop

1. Prefer a **Pi 4** with the same power/VBUS rules as the README.
2. Never stop `o3-hdmi` / raw-gadget while goggles are still plugged
   (gotcha #4). Use `scripts/o3_hdmi_safe_stop.sh` (runs multi-signal host
   probe — never refuse solely on `udc=configured`; phantom after unplug is common).
3. Measure before changing player defaults: content is often ~50 AU/s, not 60.
4. Do not leak compressed H.264 (drop decoded frames only if you must drop).
5. Patch **every** SPS level if you touch level rewriting, not only the first.

## Pull requests

- Keep PRs focused; one concern per PR when possible.
- Update docs if behaviour or install steps change.
- Say what hardware you tested (goggles model, air unit, Pi, HDMI port).
- No need for a formal CLA; by contributing you license your work under the
  same MIT License as the project (`LICENSE`).

## Security / sensitive reports

If you find something that could brick hardware for many users, or that
involves redistributing proprietary third-party code, open an issue without
attaching the proprietary material, or contact the maintainer privately if a
contact is listed on the GitHub repo.

## Scope reminder

This is interoperability work on hardware people own. Keep contributions in
that spirit.
