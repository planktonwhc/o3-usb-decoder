#!/usr/bin/env python3
"""
AOA pre-trigger — put the phone into DJI-Fly accessory mode from the Pi (or any Linux
USB host). Sends the Android Open Accessory (AOA) handshake with DJI's exact identity
strings so Android launches DJI Fly (dji.go.v5) into its accessory/FPV path.

Why this exists (two uses):
  1. SANITY CHECK, independent of the proxy: run this on the Pi with the phone on a
     USB-A port. If the phone re-enumerates as 18d1:2d0x and DJI Fly pops up, we've
     proven the Pi can drive the phone into the exact mode the goggles normally do.
  2. FALLBACK for the MITM (see docs/pi4_mitm_setup.md): if usb-proxy cannot survive
     the phone's mid-session re-enumeration into accessory mode, pre-run this so the
     phone is ALREADY 18d1:2d0x before the proxy starts — the proxy then presents a
     stable accessory device to the goggles from the first enumeration.

DJI identity strings (CONFIRMED via `adb dumpsys usb`, see protocol_notes.md):
    manufacturer = DJI
    model        = com.dji.logiclink
    description  = DJI glass
    version      = v0.0.0.0
    uri          = www.dji.com
Android's accessory intent-filter matches on manufacturer+model, so those two are the
critical ones; the rest are sent for fidelity with what the goggles present.

Usage:
    # find the phone's *current* VID:PID first (charging/MTP/adb mode):
    lsusb
    sudo .venv/bin/python poc/pi_mitm/aoa_trigger.py --vid 0x04e8 --pid 0x6860

Requires: pyusb (in .venv) + libusb + permission to claim the device (run as root, or
udev rule). This does NOT need the goggles connected — it only talks to the phone.
"""
import argparse
import sys
import time

try:
    import usb.core
    import usb.util
except ImportError:
    sys.exit("pyusb required: .venv/bin/pip install pyusb")

# AOA control-request constants (Google's Android Open Accessory protocol v2).
ACCESSORY_GET_PROTOCOL = 51
ACCESSORY_SEND_STRING = 52
ACCESSORY_START = 53
# String indices for SEND_STRING (wIndex).
STR_MANUFACTURER, STR_MODEL, STR_DESCRIPTION, STR_VERSION, STR_URI, STR_SERIAL = range(6)

DJI_STRINGS = {
    STR_MANUFACTURER: "DJI",
    STR_MODEL: "com.dji.logiclink",
    STR_DESCRIPTION: "DJI glass",
    STR_VERSION: "v0.0.0.0",
    STR_URI: "www.dji.com",
}
# Android accessory-mode VID/PID after a successful start.
GOOGLE_VID = 0x18D1
ACCESSORY_PIDS = (0x2D00, 0x2D01, 0x2D04, 0x2D05)


def send_string(dev, index, value):
    data = value.encode("ascii") + b"\x00"
    n = dev.ctrl_transfer(0x40, ACCESSORY_SEND_STRING, 0, index, data, timeout=1000)
    if n != len(data):
        raise IOError(f"SEND_STRING[{index}] short write: {n}/{len(data)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vid", required=True, help="phone's CURRENT vendor id, e.g. 0x04e8")
    ap.add_argument("--pid", required=True, help="phone's CURRENT product id, e.g. 0x6860")
    ap.add_argument("--wait", type=float, default=6.0,
                    help="seconds to wait for re-enumeration as accessory (default 6)")
    args = ap.parse_args()

    vid = int(args.vid, 0)
    pid = int(args.pid, 0)

    dev = usb.core.find(idVendor=vid, idProduct=pid)
    if dev is None:
        sys.exit(f"[!] no USB device {vid:04x}:{pid:04x} — check `lsusb` and that the "
                 f"phone is plugged into the Pi's USB-A port (and unlocked).")

    # Detach any kernel driver so we can issue control transfers.
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, usb.core.USBError):
        pass

    print(f"[*] querying AOA protocol on {vid:04x}:{pid:04x} ...")
    try:
        proto = dev.ctrl_transfer(0xC0, ACCESSORY_GET_PROTOCOL, 0, 0, 2, timeout=1000)
    except usb.core.USBError as e:
        sys.exit(f"[!] GET_PROTOCOL failed ({e}). This device may not support AOA, or "
                 f"you targeted the wrong VID:PID.")
    version = proto[0] | (proto[1] << 8)
    if version < 1:
        sys.exit("[!] device reports AOA protocol 0 — AOA not supported.")
    print(f"[+] AOA protocol v{version} supported")

    print("[*] sending DJI identity strings ...")
    for idx, val in DJI_STRINGS.items():
        send_string(dev, idx, val)
        print(f"      [{idx}] = {val!r}")

    print("[*] sending ACCESSORY_START (device will now re-enumerate) ...")
    dev.ctrl_transfer(0x40, ACCESSORY_START, 0, 0, None, timeout=1000)
    usb.util.dispose_resources(dev)

    print(f"[*] waiting up to {args.wait:.0f}s for accessory re-enumeration ...")
    deadline = time.time() + args.wait
    while time.time() < deadline:
        for apid in ACCESSORY_PIDS:
            acc = usb.core.find(idVendor=GOOGLE_VID, idProduct=apid)
            if acc is not None:
                print(f"[+] SUCCESS: phone is now {GOOGLE_VID:04x}:{apid:04x} "
                      f"(Android accessory mode). DJI Fly should be launching.")
                print("[i] Next: start usb-proxy pointed at this VID:PID (see "
                      "docs/pi4_mitm_setup.md), or watch the phone for DJI Fly.")
                return
        time.sleep(0.3)
    print("[!] did not observe an accessory-mode device. Common causes: phone screen "
          "locked, DJI Fly not installed, or the phone rejected the accessory prompt. "
          "Check the phone screen for an 'Open DJI Fly?' dialog and tick 'always'.")


if __name__ == "__main__":
    main()
