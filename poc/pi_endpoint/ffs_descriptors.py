"""
FunctionFS descriptor blob for a single-interface bulk-IN/bulk-OUT accessory.

Matches the post-AOA phone personality used with DJI Goggles 2 (18d1:2d01):
one vendor interface, EP 0x81 bulk IN + EP 0x01 bulk OUT, wMaxPacketSize 512.

Written to FunctionFS ep0 as: descs_v2 header + fs/hs descriptors + strings.
"""
from __future__ import annotations

import struct

# linux/usb/functionfs.h constants
FUNCTIONFS_DESCRIPTORS_MAGIC_V2 = 3
FUNCTIONFS_STRINGS_MAGIC = 2
FUNCTIONFS_HAS_FS_DESC = 1
FUNCTIONFS_HAS_HS_DESC = 2
FUNCTIONFS_HAS_SS_DESC = 4  # unused


def _interface(bInterfaceNumber=0, bNumEndpoints=2, iInterface=1):
    # usb_interface_descriptor
    return struct.pack(
        "<BBBBBBBBB",
        9,  # bLength
        4,  # bDescriptorType INTERFACE
        bInterfaceNumber,
        0,  # bAlternateSetting
        bNumEndpoints,
        0xFF,  # bInterfaceClass vendor
        0xFF,
        0x00,
        iInterface,
    )


def _endpoint(addr, maxpacket=512, interval=0):
    # usb_endpoint_descriptor (audio-less 7-byte form)
    return struct.pack(
        "<BBBBHB",
        7,  # bLength
        5,  # bDescriptorType ENDPOINT
        addr,
        2,  # bmAttributes bulk
        maxpacket,
        interval,
    )


def build_descriptors() -> bytes:
    """Return the full FunctionFS descriptors payload for write(ep0)."""
    iface = _interface()
    # OUT = host→device (goggles write video + DUML to us)  EP 0x01
    # IN  = device→host (we write responses)                 EP 0x81
    ep_out = _endpoint(0x01, 512)
    ep_in = _endpoint(0x81, 512)
    fs = iface + ep_out + ep_in
    hs = iface + ep_out + ep_in  # same layout for HS

    flags = FUNCTIONFS_HAS_FS_DESC | FUNCTIONFS_HAS_HS_DESC
    # struct usb_functionfs_descs_head_v2 {
    #   __le32 magic; __le32 length; __le32 flags;
    #   /* __le32 fs_count, hs_count; */ depending on flags
    # }
    counts = struct.pack("<II", 3, 3)  # fs_count=3 descriptors, hs_count=3
    body = counts + fs + hs
    length = 12 + len(body)  # magic+length+flags + body
    head = struct.pack(
        "<III",
        FUNCTIONFS_DESCRIPTORS_MAGIC_V2,
        length,
        flags,
    )
    return head + body


def build_strings() -> bytes:
    """Return FunctionFS strings payload (lang 0x0409, one interface string)."""
    # struct usb_functionfs_strings_head {
    #   __le32 magic; __le32 length; __le32 str_count; __le32 lang_count;
    # }
    s = b"DJI logiclink\x00"
    # one language: __le16 lang + string data
    lang = struct.pack("<H", 0x0409) + s
    str_count = 1
    lang_count = 1
    length = 16 + len(lang)
    head = struct.pack(
        "<IIII",
        FUNCTIONFS_STRINGS_MAGIC,
        length,
        str_count,
        lang_count,
    )
    return head + lang
