# Open-source release guide

How this monorepo becomes a public GitHub (or similar) project without shipping
third-party copyrighted blobs or multi-GB research dumps.

## Goals

1. Publish a **runnable** O3 → Pi → HDMI stack under the **MIT License**.
2. Publish enough **protocol documentation** that others can reimplement.
3. **Never** redistribute DJI Fly APKs, native libs, phone firmware, or
   CosmoStreamer / other closed binaries.
4. Keep a private lab tree (this checkout) for captures and RE experiments.

## What is public vs private

| Include in public tree | Keep private / out of git |
|---|---|
| `poc/pi_endpoint/` (shipping runtime) | `analysis/dji_fly_apk/` |
| `analysis/*.py` (no APK trees) | `handover/`, `handover2/`, `*.zip` phone firmware |
| `scripts/` install and ops | `poc/frida_repack/` (app instrumentation research) |
| Curated `docs/` (protocol, field, session closes) | Raw Frida/logcat dumps under `captures/aoa/` |
| | `CLAUDE.md`, `docs/GROK_STATE.md` (lab / agent diary) |
| `poc/pi_mitm/`, `poc/usb_proxy_o3/`, `poc/aoa_sniffer` **source** | Built `*.apk`, keystores, `node_modules/` |
| `poc/replay.py` | Large `*.h264` / `*.mp4` / `*.MOV` field media |
| `LICENSE`, `NOTICE`, `README.md`, `CONTRIBUTING.md` | Local `.venv/`, agent state |
| `captures/pi_pull/*GOLD_ANALYSIS*` (text) | Multi-MB usbproxy stdout / payload binaries |
| `captures/recon/` text logs (optional) | Personal secrets, private deploy keys |

Runtime **does not** need any capture files. Gold arming bytes already live in
`poc/pi_endpoint/gold_app_seq.py` as an original encoding of observed traffic.

## One-command public tree

From the monorepo root:

```bash
scripts/export_public_tree.sh
# default output: ../dji-o3-usb-decoder-public/
# override:
#   OUT=/tmp/o3-public scripts/export_public_tree.sh
```

Then create the public git remote from that directory (fresh history):

```bash
cd ../dji-o3-usb-decoder-public
git init
git add .
git commit -m "Initial public release of dji-o3-usb-decoder"
# create empty GitHub repo, then:
git branch -M main
git remote add origin git@github.com:YOU/dji-o3-usb-decoder.git
git push -u origin main
```

Prefer a **fresh** public history over force-pushing this monorepo if the
monorepo ever contained sensitive material in older commits you care about.

## Pre-flight checklist (before first public push)

- [ ] `scripts/export_public_tree.sh` completed without error
- [ ] `find "$OUT" -iname '*.apk' -o -iname '*.so' -o -name 'dji_fly_apk'` is empty
- [ ] No multi-GB zips or `.MOV` field media in `$OUT`
- [ ] `LICENSE` and `NOTICE` present
- [ ] README install path still accurate
- [ ] `docs/SUPPORTED_HARDWARE.md` matches what you claim
- [ ] GitHub repo description: independent interop project, not affiliated with DJI
- [ ] Optional: Discussions / issue templates; no “support guarantees”

## After publishing

1. Announce with **supported matrix** + power/VBUS gotchas in the first post.
2. Pin an issue: “compatibility reports (goggles / air unit / firmware).”
3. Tag a release (`v0.1.0`) when one external person reproduces HDMI on a clean Pi.
4. Keep this monorepo as the lab; cherry-pick or re-export for public updates
   (`export_public_tree.sh` is idempotent into a clean `OUT`).

## Updating the public repo later

```bash
# monorepo has new fixes
scripts/export_public_tree.sh
cd ../dji-o3-usb-decoder-public
# if that dir is already a git clone of the public remote:
git add -A
git status   # review
git commit -m "Describe the user-visible fix"
git push
```

Or re-export into a fresh dir and copy commits carefully — avoid rsyncing
into a dirty public tree without reviewing `git status`.

## License choice

**MIT** — maximises reuse (including commercial hardware kits). Contributors
agree to the same license via `CONTRIBUTING.md`. If you later need a different
policy for a specific component, document it in `NOTICE`.
