# Session close — 2026-07-16: open-source publish + Pi docs

## TL;DR

| Item | Result |
|---|---|
| Out-of-box Pi README | ✅ full flash → live HDMI guide |
| `install_hdmi_service.sh` | ✅ auto-installs `python3-gi` + Gst introspection |
| Open-source prep | ✅ MIT, NOTICE, CONTRIBUTING, support matrix, export script |
| Public GitHub | ✅ **https://github.com/dr00min/o3-usb-decoder** |
| Public tag | ✅ **`v0.1.0`** on origin |
| Field Pi RAM correction | ✅ **2GB** (not 8GB — wrong item ordered; works) |
| Monetize / legal / release advice | ✅ discussed (advisory only; not legal advice) |
| Runtime HDMI code | unchanged this session (still seamless splash baseline) |

**No Pi deploy this session** — docs + packaging + public repo only. Live path left as prior session left it.

---

## What shipped (monorepo `main`)

| Commit | Summary |
|---|---|
| `29409d6` | Pi out-of-box README; install python-gi on reflash |
| `cb98f05` | Open-source prep: LICENSE/NOTICE/CONTRIBUTING, untrack Frida + bulk captures |
| `6ffb072` | Field Pi is 2GB (not 8GB) |
| `b9cebb4` | export_public_tree: do not wipe destination `.git` |

### New / updated files

| Path | Role |
|---|---|
| `README.md` | Empty-Pi → live HDMI + license badge + public links |
| `LICENSE` | MIT |
| `NOTICE` | Not affiliated with DJI; no proprietary binaries; trademarks |
| `CONTRIBUTING.md` | PR scope / what not to send |
| `docs/SUPPORTED_HARDWARE.md` | Field matrix (Goggles 2 + O3 + Pi 4 **2GB**) |
| `docs/OPEN_SOURCE.md` | How to re-export and update the public tree |
| `captures/README.md` | Captures not required for runtime |
| `scripts/export_public_tree.sh` | Curated public tree (~1.3 MiB, no APKs/Frida) |
| `scripts/install_hdmi_service.sh` | + PyGObject / Gst GI packages |

### Git hygiene

**Untracked** (still on disk in the lab monorepo):

- `poc/frida_repack/`
- Frida/logcat dumps under `captures/aoa/`, bulk usbproxy stdout, pcaps, etc.

**Never in public tree:** `analysis/dji_fly_apk/`, handovers, SAMFW zips, built APKs.

---

## Public repo

| | |
|---|---|
| URL | https://github.com/dr00min/o3-usb-decoder |
| Default branch | `main` |
| First commit | `8ba3f81` Initial public release |
| Follow-up | `fca3acf` docs: field Pi is 2GB |
| Tag | **`v0.1.0`** (source-only release; no package registry) |

**How to update public after monorepo fixes:**

```bash
# monorepo
scripts/export_public_tree.sh
cd ../dji-o3-usb-decoder-public   # clone of dr00min/o3-usb-decoder
git add -A && git status
git commit -m "…" && git push
# optional: git tag -a v0.1.1 -m "…" && git push --tags
```

Local public clone path used this session: `/home/ubuntu/dji-o3-usb-decoder-public`  
(SSH key on this machine is authorized for `dr00min`.)

### Suggested release notes (owner may have published)

- **Title:** `v0.1.0 — First public O3 → Pi HDMI path`
- Body: field matrix, install pointer, VBUS + gotcha #4, MIT, not affiliated with DJI  
  (full paste text was given in-session)

**Packages (ghcr/npm/etc.):** do not bother for this stack.  
**Releases:** lightweight tags are enough; no binary assets required.

---

## Decisions / advice captured (no code)

1. **Open-source heart decision → executed.** Ship runtime + docs; keep RE lab private.
2. **Monetization:** viable (Cosmo precedent); best fit = hardware kits / event support + open software, not Cosmo-style closed license keys. Legal gray area = app EULA / interop framing — not “automatic crime”; not legal advice.
3. **Pi RAM:** field unit is **2GB** and is what field-proved the stack.

---

## Pi / runtime left-state

Unchanged from seamless-splash session (not redeployed today):

| | |
|---|---|
| Host | `o3pi` / `192.168.1.57` — Pi 4 Model B **2GB** |
| Services | Expect `o3-hdmi` + `o3-oops-watchdog` still as last deploy |
| Player | `O3_HDMI_PLAYER=atomic`, SW decode, idle splash, hot-swap re-arm |
| HDMI | Shipping **HDMI-A-2** (far) |

If code on Pi is older than monorepo: unplug goggles → `scripts/push_to_pi.sh` → `install_hdmi_service.sh`.

---

## Still open (next session)

1. **`o3_soak.sh` 5m PASS** (cool drone, continuous stream) — still the top field QA item:
   ```bash
   ssh pi@192.168.1.57
   sudo ~/o3test/scripts/o3_soak.sh
   cat /var/log/o3/soak_*/summary.txt   # want RESULT: PASS
   ```
2. Optional GitHub polish: About blurb, topics (`dji`, `fpv`, `o3`, `raspberry-pi`), confirm v0.1.0 release notes published.
3. External compatibility reports (other firmware / Goggles 3 / O4) once people find the repo.
4. Backlog (unchanged): dual-CRTC, re-stream, arming trim, packaging/SD image if demand, 1440×1080 curiosity.

**Do not regress:** atomic-only field path; no mpv; in-process splash DRM discipline; VBUS + gotcha #4; never leak compressed ES.

---

## Handoff for next agent

1. Read this file + prior: `docs/SESSION_CLOSE_2026-07-16_seamless_splash.md` (runtime).
2. Public updates: `docs/OPEN_SOURCE.md` + `scripts/export_public_tree.sh` (protects `.git`).
3. Operator truth remains `CLAUDE.md` CURRENT STATUS (updated to point here for packaging).
4. Field code path: `poc/pi_endpoint/` — not touched this session.
