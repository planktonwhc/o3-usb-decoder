# GO-FLY transition analysis — why the bridged session never enters live FPV

> **⚠️ REFRAME (2026-07-13, owner hardware test) — the "Mini-3 profile misID" premise below is
> DEAD.** Plugging the phone **directly** into the goggles (no Pi) shows **live FPV video**, on
> BOTH the rooted and a personal phone. The "Mini-3 screen" is just DJI Fly's normal
> **disconnect / idle fallback** screen (back button only) — pull the cable while on the live-FPV
> screen and it switches to that Mini-3 screen. So there is **no ProductType misID**: through the
> Pi bridge, DJI Fly reaches the disconnect-fallback state, i.e. the goggles effectively **drop as
> a live-FPV device** at/around GO FLY. Lever B (spoof ProductType) is therefore moot. The real
> question is: **why does FPV arm directly but collapse through the bridge?** Findings 1-2 below
> still stand as evidence; read the "Where this leaves the plan" section as updated by this reframe.

**Date:** 2026-07-13 (offline analysis session)
**Inputs:** `captures/pi_pull/2026-07-13_074307_pi-mitm_payload.log` (I-frame injection run,
20 046 DUML packets, 331 s) cross-checked against `2026-07-13_063725_...` (golden recognition run).
**Tool:** `analysis/gofly_transition.py` (new), plus `special_and_timeline.py` / `producttype_hunt.py`.

## Goal
The I-frame injection is proven on the wire (140/140 cs=1/0x01 packs flipped bit5, delivered
with valid CRC — see memory `o3-phaseA-capture-recipe`). But DJI Fly enters a **Mini-3 flight
profile** (black camera, no FPV video path) after GO FLY, so the O3 live-view session never
arms and there is no video context for the injected I-frame request. The owner's reframe: the
home screen **correctly** shows "DJI O3 Air Unit"; the misID happens at the **GO-FLY →
live-view transition**. This session dissected that transition offline.

## Finding 1 — GO FLY introduces NO new product/handshake query
GO FLY = the first `cs=1 (SPECIAL)` packet, at **t+178.64 s**. Windowing the capture around
it (`gofly_transition.py`): the **only** command tuple that appears after GO FLY and not before
is `cs=1/0x01` (the periodic `SendSpecialControllPack` flush) itself. Every other command the
app sends after GO FLY was **already streaming on the home screen** (RC/flight stream, version,
heartbeats, camera queries).

**Implication:** the Mini-3-vs-O3-FPV decision is **not** made by a distinct query issued at
GO FLY. It is determined by state DJI Fly already holds from the **recognition phase**
(t+0…t+3 s) — either from the identity/version responses, or from the outcome of the activation
handshake below. This closes the prior "dissect the post-GO-FLY exchange" lead: there is nothing
new to dissect there.

## Finding 2 — the "gvg" activation (cs=0/0xdd → r18) is DELIVERED but REFUSED
This supersedes the open worry that the bridge might be dropping it.

- App → goggles, `cs=0 cmd_id=0xdd`, `sender=2` (APP) → `receiver=18 idx=4`, `packet_type=req`,
  **`ack_type=2` (a reply is explicitly requested)**, seq 43293.
- Sent **4×** (t+0.50, 1.24, 2.24, 3.25 s) — a retry storm — then the app gives up.
- **Delivery confirmed:** every `IN` line has a matching `TXG` (usb_raw_ep_write completed →
  goggles' host controller consumed it) ~16 ms later. So the goggles **receive** it.
- **Goggles never respond:** 0 `OUT` responses for 0xdd. Identical result and identical blob in
  BOTH the 063725 and 074307 captures → deterministic refusal, not a bridge race.

So this is **not** a bridge-fidelity problem. The goggles get the activation and decline to
answer it. `receiver=18` is the same device-type (`0x12`=18) byte that prefixes every version
response — i.e. the security/identity module.

### gvg blob structure
```
67766783  magic "gvg" + flag 0x83
03 00 01 00                      \
0c 3d fd 65   ( 0x65fd3d0c )      |  25-byte structured header
00 a5 ff ff                       |  (looks like: subcmd/version, a token/nonce or
00 00                             |   timestamp word, and four LE32 params 13/2/20/3)
0d 00 00 00  02 00 00 00          |
14 00 00 00  03 00 00 00         /
<64 bytes high-entropy>          <-- encrypted / signed activation payload
```
The 64-byte tail is high-entropy → an encrypted or signed challenge. This is DJI's
activation / binding / security handshake, aimed at the identity module (r18). It is the **only
meaningful app→goggles request in the whole session that is refused** (all camera and version
queries answer 100%; the other 0%-answered commands are flight-controller/RC streams to r3,
which are *structurally* unanswered because an O3 air unit has no flight controller — not a fault).

## Finding 3 — identity/version responses carry nothing obviously product-distinguishing
All `cs=0/0x01` version responses (senders 1/14/28/31) answer 100% and are byte-stable:
device-type byte `0x12` (18) uniform; strings `zv900 gl` / `Ver.A` / `Ver.05`; camera (sender 1)
reports `H3.2`; camera serial `TTFM372A10DXR` (via cs=2/0xb5). Nothing here screams "Mini 3" vs
"O3". If DJI Fly derives the flight profile from a numeric ProductType, it comes from a field we
can't interpret without DJI Fly's product-enum table (static-RE lane is closed; see memory
`static-re-iframe-anchors`).

## Finding 4 (reverse direction) — the app never answers the goggles' cs=0/0x81 & 0x82 requests
Pairing goggles→app requests (OUT, req) to app→goggles responses (IN, resp) by seq:
- `cs=0/0x81`: **0 / 331** answered by the app (ack_type=2 → reply required)
- `cs=0/0x82`: **0 / 332** answered by the app (ack_type=2)
The goggles retry these ~1 Hz for the entire 331 s session and never get an answer. This is the
mirror image of the gvg refusal. Given the reframe (the app is in the disconnect/fallback state
through the bridge), this is most likely a **symptom**: a healthy live-FPV app would answer 0x81/
0x82, but our bridged app is stuck and ignores them. (Every goggles→app request the app *does*
handle is fire-and-forget, ack_type=0 — camera 0x80 stream, OFDM 0x08/0x43/0x75, radio 0x1e, etc.)

## Where this leaves the plan — REFRAMED (direct FPV works; the bridge collapses the session)

Direct connection = live FPV, confirmed. So the goal-path ("arm DJI Fly, steal the I-frame
trigger, then have the Pi replay it standalone") is **sound**. The bridge is what breaks it: the
app reaches the disconnect/fallback state at/around GO FLY. **The offline (failed-state) captures
are exhausted — every bridge capture is a picture of the FAILURE. To find the cause we need the
WORKING (direct) session to diff against.**

Two symmetric refused exchanges are the evidence of the collapse: (a) app→goggles **gvg**
activation (cs=0/0xdd, delivered but goggles refuse), and (b) goggles→app **cs=0/0x81 & 0x82**
(app never answers). Leading hypothesis for the *cause*: the current bridge recipe **pre-triggers
the phone into AOA accessory mode separately, then lets the goggles enumerate** — so the goggles'
own AOA handshake (GET_PROTOCOL / SEND_STRING×6 / ACCESSORY_START) is a no-op against an
already-accessory phone. If the gvg security handshake is seeded by session/nonce state
established during that goggles-driven AOA bring-up, the pre-trigger **desyncs the key exchange**
→ goggles refuse gvg → app never fully connects → disconnect fallback. (The AOA *identity strings*
match the goggles', verified 2026-07-06, so it's session/nonce state, not the strings.)

### Ranked next steps (all bench)
1. **Natural-flow bridge (test the desync hypothesis AND capture in one shot).** Let the goggles
   drive the AOA handshake *through* the proxy (no pre-trigger), so the gvg exchange happens in its
   native context. If FPV arms through a natural-flow bridge, we've simultaneously **fixed** the
   bridge and **captured** the real trigger — done.
   - **Code ready (2026-07-13):** usb-proxy now follows MTP→accessory re-enum in-process
     (`--follow_aoa_reenum` / `USBPROXY_FOLLOW_AOA=1`) — closes raw_gadget cleanly, waits for
     `18d1:2d0x`, rebuilds descriptors, continues. No process exit mid-re-enum (gotcha #4).
     Apply: `scripts/apply_usb_proxy_o3.sh`. Capture: `scripts/phaseA_capture.sh` (default =
     natural; `--pretrigger` keeps the old recipe).
   - **Still needs a bench run** on the Pi with goggles + phone to validate.
2. **Direct-session reference capture (definitive diff data).** Capture the working direct session
   on the rooted phone (the hard part: phone is USB *peripheral* here, so host-side usbmon may not
   see UDC traffic, and DJI Fly holds `/dev/usb_accessory` exclusively — this is exactly why the Pi
   MITM exists). If obtainable, it shows the real gvg response + the real live-FPV I-frame request
   (pack[5] bit5 set) to replay from the Pi in Phase B.
3. **Lever A — synthesize the gvg ACK to the app (cheap, current pre-trigger bridge).** Have the
   proxy fabricate the goggles→app gvg response (cs=0/0xdd resp, matching seq) so the app believes
   activation succeeded. Reuses the proven injection mechanism (`poc/usb_proxy_o3/iframe_inject.h`),
   fabricating an inbound DUML. Weakness: blind on the response payload, and if the goggles
   themselves gate video on real activation, app-side spoofing won't make them serve frames (the
   140/140 I-frame injections already got zero frames back). Best treated as a quick probe, not the
   main bet — #1 is more likely to actually fix it.

~~Lever B (spoof ProductType)~~ — **dropped**: the reframe shows there is no ProductType misID.

## Reusable artifact
`analysis/gofly_transition.py <payload.log>` — locates GO FLY (first cs=1), lists command tuples
new after GO FLY, pairs app requests to responses by seq, and prints per-command answer rates
(flags <50%). Distinguishes bridge-drop from structural-no-FC from goggles-refusal.
