# captures/

Append-only research and field logs. **Not required to run the HDMI stack.**

The shipping path encodes the gold arming sequence in
`poc/pi_endpoint/gold_app_seq.py`. You do not need any files from this directory
to install or operate `o3-hdmi`.

## Public vs private

| Usually publishable | Keep private / out of git |
|---|---|
| `pi_pull/*GOLD_ANALYSIS*.txt` | `analysis/dji_fly_apk/` (not under this dir) |
| `recon/*.txt` host scans | Frida / logcat dumps (`aoa/`, `*frida*`) |
| Short text benches | Raw `*_payload.log`, large `.h264` / `.mp4` |

`scripts/export_public_tree.sh` copies only a small curated subset into the
public tree. Local full captures can stay on disk for the authors’ lab.
