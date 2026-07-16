"""
Minimal Python binding for xairy/raw-gadget (/dev/raw-gadget).
Used for Phase B because FunctionFS is not enabled on the Pi kernel
(CONFIG_USB_FUNCTIONFS is not set; usb_f_fs alone is insufficient).
"""
from __future__ import annotations

import ctypes
import fcntl
import os
import struct
from typing import Optional

_libc = ctypes.CDLL("libc.so.6", use_errno=True)
_libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
_libc.ioctl.restype = ctypes.c_int

UDC_NAME_LENGTH_MAX = 128

# enum usb_device_speed
USB_SPEED_UNKNOWN = 0
USB_SPEED_LOW = 1
USB_SPEED_FULL = 2
USB_SPEED_HIGH = 3
USB_SPEED_WIRELESS = 4
USB_SPEED_SUPER = 5
USB_SPEED_SUPER_PLUS = 6

# event types
USB_RAW_EVENT_INVALID = 0
USB_RAW_EVENT_CONNECT = 1
USB_RAW_EVENT_CONTROL = 2
USB_RAW_EVENT_SUSPEND = 3
USB_RAW_EVENT_RESUME = 4
USB_RAW_EVENT_RESET = 5
USB_RAW_EVENT_DISCONNECT = 6

# ch9
USB_DIR_OUT = 0
USB_DIR_IN = 0x80
USB_TYPE_MASK = 0x60
USB_TYPE_STANDARD = 0x00
USB_TYPE_CLASS = 0x20
USB_TYPE_VENDOR = 0x40
USB_RECIP_MASK = 0x1F
USB_RECIP_DEVICE = 0x00
USB_RECIP_INTERFACE = 0x01
USB_RECIP_ENDPOINT = 0x02
USB_REQ_GET_STATUS = 0x00
USB_REQ_CLEAR_FEATURE = 0x01
USB_REQ_SET_FEATURE = 0x03
USB_REQ_SET_ADDRESS = 0x05
USB_REQ_GET_DESCRIPTOR = 0x06
USB_REQ_SET_DESCRIPTOR = 0x07
USB_REQ_GET_CONFIGURATION = 0x08
USB_REQ_SET_CONFIGURATION = 0x09
USB_REQ_GET_INTERFACE = 0x0A
USB_REQ_SET_INTERFACE = 0x0B
USB_DT_DEVICE = 0x01
USB_DT_CONFIG = 0x02
USB_DT_STRING = 0x03
USB_DT_INTERFACE = 0x04
USB_DT_ENDPOINT = 0x05
USB_DT_DEVICE_QUALIFIER = 0x06
USB_DT_OTHER_SPEED_CONFIG = 0x07
USB_DT_BOS = 0x0F

USB_ENDPOINT_XFER_BULK = 2

# Android Open Accessory (AOA) vendor control requests, driven by the goggles (USB host).
# Confirmed in the gold usbproxy.stdout: the goggles send GET_PROTOCOL → SEND_STRING×6 →
# ACCESSORY_START before the phone re-enumerates as 18d1:2d01 and video begins.
AOA_GET_PROTOCOL = 0x33   # bmRequestType 0xC0 (IN), wLength 2 → reply protocol version (>=1)
AOA_SEND_STRING = 0x34    # bmRequestType 0x40 (OUT), wIndex 0..5 → host sends identity string
AOA_START = 0x35          # bmRequestType 0x40 (OUT), wLength 0 → enter accessory mode (re-enum)
AOA_PROTOCOL_VERSION = 2

# USB product IDs. Normal (pre-accessory) mode uses a non-accessory PID so the goggles run
# the full AOA bring-up; after ACCESSORY_START we re-enumerate as the accessory PID.
PID_NORMAL = 0x4EE0       # generic Android (Nexus/Pixel) — any non-2d0x PID triggers AOA probe
PID_ACCESSORY = 0x2D01    # AOA accessory (+adb), the PID the goggles converse with in gold

MAX_TRANSFER_SIZE = 4096


def _IOC(dir_, type_, nr, size):
    return (dir_ << 30) | (ord(type_) << 8) | nr | (size << 16)


def _IO(type_, nr):
    return _IOC(0, type_, nr, 0)


def _IOR(type_, nr, size):
    return _IOC(2, type_, nr, size)


def _IOW(type_, nr, size):
    return _IOC(1, type_, nr, size)


def _IOWR(type_, nr, size):
    return _IOC(3, type_, nr, size)


# struct usb_raw_init: 128 + 128 + 1 = 257, typically padded
SIZEOF_USB_RAW_INIT = 257
# event: type u32 + length u32 + data[0] — we allocate large buffer
# ep_io: ep u16 + flags u16 + length u32 + data[]
SIZEOF_EP_IO_HDR = 8

USB_RAW_IOCTL_INIT = _IOW("U", 0, SIZEOF_USB_RAW_INIT)
USB_RAW_IOCTL_RUN = _IO("U", 1)
# EVENT_FETCH uses a large buffer; size in ioctl encoding is often sizeof(struct) with flexible array
# raw-gadget uses _IOR('U', 2, struct usb_raw_event) where event is type+length only in some versions
# Looking at kernel uapi: struct has __u32 type; __u32 length; __u8 data[];
# The ioctl size is typically sizeof without flexible array = 8, and length field says data size.
USB_RAW_IOCTL_EVENT_FETCH = _IOR("U", 2, 8)
USB_RAW_IOCTL_EP0_WRITE = _IOW("U", 3, SIZEOF_EP_IO_HDR)
USB_RAW_IOCTL_EP0_READ = _IOWR("U", 4, SIZEOF_EP_IO_HDR)
USB_RAW_IOCTL_EP_ENABLE = _IOW("U", 5, 9)  # struct usb_endpoint_descriptor (9 bytes)
USB_RAW_IOCTL_EP_DISABLE = _IOW("U", 6, 4)
USB_RAW_IOCTL_EP_WRITE = _IOW("U", 7, SIZEOF_EP_IO_HDR)
USB_RAW_IOCTL_EP_READ = _IOWR("U", 8, SIZEOF_EP_IO_HDR)
USB_RAW_IOCTL_CONFIGURE = _IO("U", 9)
USB_RAW_IOCTL_VBUS_DRAW = _IOW("U", 10, 4)
USB_RAW_IOCTL_EPS_INFO = _IOR("U", 11, 30 * (16 + 4 + 8 + 8))  # approximate
USB_RAW_IOCTL_EP0_STALL = _IO("U", 12)


class RawGadget:
    def __init__(self, driver: str = "fe980000.usb", device: str = "fe980000.usb"):
        self.driver = driver
        self.device = device
        self.fd = -1
        self._open()

    def _open(self):
        self.fd = os.open("/dev/raw-gadget", os.O_RDWR)
        init = bytearray(SIZEOF_USB_RAW_INIT)
        d = self.driver.encode()[: UDC_NAME_LENGTH_MAX - 1]
        dev = self.device.encode()[: UDC_NAME_LENGTH_MAX - 1]
        init[0 : len(d)] = d
        init[128 : 128 + len(dev)] = dev
        init[256] = USB_SPEED_HIGH
        fcntl.ioctl(self.fd, USB_RAW_IOCTL_INIT, bytes(init))
        fcntl.ioctl(self.fd, USB_RAW_IOCTL_RUN, 0)

    def reinit(self, settle: float = 0.2):
        """Force a bus re-enumeration by dropping off /dev/raw-gadget and re-binding the
        same UDC. Used for the AOA normal→accessory transition (ACCESSORY_START): the host
        sees a disconnect then re-reads descriptors, which now advertise the accessory PID.
        NOTE: closing raw-gadget while the goggles are actively transferring can oops the
        dwc2 stack (Pi gotcha #4); ACCESSORY_START is the sanctioned quiescent moment for it.
        Also used after *phantom* unplug (UDC stays 'configured' with no host) — that path
        only runs when host liveness has already proven the bus is dead."""
        import time
        try:
            if self.fd >= 0:
                os.close(self.fd)
        finally:
            self.fd = -1
        time.sleep(settle)
        self._open()

    def close_fd_only(self):
        """Close the gadget fd without reopening — wakes a blocked event_fetch (EBADF)
        so the main loop can run a full rearm. Used by the phantom-host watchdog."""
        try:
            if self.fd >= 0:
                os.close(self.fd)
        finally:
            self.fd = -1

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def _ioctl(self, req: int, buf: bytearray) -> int:
        """ioctl via ctypes libc so the GIL is RELEASED during the (possibly blocking) call.
        CRITICAL for the multi-threaded endpoint: fcntl.ioctl HOLDS the GIL for the whole
        blocking call, so a main thread parked in EVENT_FETCH starves the bulk reader/driver
        threads (they block on the GIL futex and never run — no commands ever go out).
        ctypes foreign calls drop the GIL, letting event_fetch / ep_read / ep_write on
        different endpoints block concurrently (raw-gadget is built for this). buf is mutated
        in place; returns the driver retcode (raises OSError on <0)."""
        cbuf = (ctypes.c_ubyte * len(buf)).from_buffer(buf)
        rv = _libc.ioctl(self.fd, ctypes.c_ulong(req), ctypes.cast(cbuf, ctypes.c_void_p))
        if rv < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        return rv

    def event_fetch(self, max_data: int = 4096):
        # buffer: type(4) length(4) data(max_data)
        # IMPORTANT: length is an IN/OUT parameter — set to max data capacity
        # before the ioctl (matches usb-proxy / raw-gadget examples).
        buf = bytearray(8 + max_data)
        struct.pack_into("<II", buf, 0, 0, max_data)
        self._ioctl(USB_RAW_IOCTL_EVENT_FETCH, buf)
        typ, length = struct.unpack_from("<II", buf, 0)
        if length > max_data:
            length = max_data
        data = bytes(buf[8 : 8 + length])
        return typ, data

    def ep0_write(self, data: bytes):
        # ep=0, flags=0, length, data — allocate full max for kernel copy
        io = bytearray(SIZEOF_EP_IO_HDR + MAX_TRANSFER_SIZE)
        struct.pack_into("<HHI", io, 0, 0, 0, len(data))
        io[8 : 8 + len(data)] = data
        self._ioctl(USB_RAW_IOCTL_EP0_WRITE, io)
        return len(data)

    def ep0_read(self, length: int = 0):
        # For OUT status / data stage
        io = bytearray(SIZEOF_EP_IO_HDR + MAX_TRANSFER_SIZE)
        struct.pack_into("<HHI", io, 0, 0, 0, length)
        self._ioctl(USB_RAW_IOCTL_EP0_READ, io)
        ln = struct.unpack_from("<I", io, 4)[0]
        return bytes(io[8 : 8 + ln])

    def ep0_stall(self):
        fcntl.ioctl(self.fd, USB_RAW_IOCTL_EP0_STALL, 0)

    def configure(self):
        fcntl.ioctl(self.fd, USB_RAW_IOCTL_CONFIGURE, 0)

    def vbus_draw(self, ma: int = 500):
        buf = struct.pack("<I", ma)
        fcntl.ioctl(self.fd, USB_RAW_IOCTL_VBUS_DRAW, buf)

    def ep_enable(self, bEndpointAddress: int, bmAttributes: int = USB_ENDPOINT_XFER_BULK,
                  wMaxPacketSize: int = 512, bInterval: int = 0) -> int:
        # usb_endpoint_descriptor (9 bytes packed: + bRefresh + bSynchAddress)
        desc = bytearray(struct.pack(
            "<BBBBHBBB",
            7,  # bLength reported as 7 (non-audio)
            USB_DT_ENDPOINT,
            bEndpointAddress,
            bmAttributes,
            wMaxPacketSize,
            bInterval,
            0,  # bRefresh
            0,  # bSynchAddress
        ))
        # Must use libc.ioctl so the integer *return value* (ep handle) is preserved;
        # fcntl.ioctl with a buffer returns the buffer, not the driver retcode.
        buf = (ctypes.c_ubyte * len(desc)).from_buffer(desc)
        rv = _libc.ioctl(self.fd, ctypes.c_ulong(USB_RAW_IOCTL_EP_ENABLE),
                         ctypes.cast(buf, ctypes.c_void_p))
        if rv < 0:
            err = ctypes.get_errno()
            raise OSError(err, f"EP_ENABLE 0x{bEndpointAddress:02x} failed errno={err}")
        return rv

    def ep_disable(self, ep_num: int):
        buf = struct.pack("<I", ep_num)
        fcntl.ioctl(self.fd, USB_RAW_IOCTL_EP_DISABLE, buf)

    def ep_write(self, ep_num: int, data: bytes) -> int:
        # GIL-releasing (ctypes) so this bulk-IN write can run while the reader thread is
        # parked in ep_read and the main thread in event_fetch. Raises OSError on error.
        io = bytearray(SIZEOF_EP_IO_HDR + MAX_TRANSFER_SIZE)
        struct.pack_into("<HHI", io, 0, ep_num, 0, len(data))
        io[8 : 8 + len(data)] = data
        return self._ioctl(USB_RAW_IOCTL_EP_WRITE, io)

    def ep_read(self, ep_num: int, length: int = 4096) -> bytes:
        io = bytearray(SIZEOF_EP_IO_HDR + MAX_TRANSFER_SIZE)
        struct.pack_into("<HHI", io, 0, ep_num, 0, length)
        try:
            rv = self._ioctl(USB_RAW_IOCTL_EP_READ, io)   # GIL released while blocking
        except OSError:
            return b""
        ln = struct.unpack_from("<I", io, 4)[0]
        if ln <= 0 and rv > 0:
            ln = rv
        return bytes(io[8 : 8 + min(ln, length)])


def parse_ctrl(data: bytes):
    """Parse usb_ctrlrequest (8 bytes)."""
    if len(data) < 8:
        return None
    bRequestType, bRequest, wValue, wIndex, wLength = struct.unpack_from("<BBHHH", data, 0)
    return {
        "bRequestType": bRequestType,
        "bRequest": bRequest,
        "wValue": wValue,
        "wIndex": wIndex,
        "wLength": wLength,
    }


# --- Descriptors for 18d1:2d01 accessory-like device ---

def device_descriptor(pid: int = PID_ACCESSORY):
    # usb_device_descriptor 18 bytes
    return struct.pack(
        "<BBHBBBBHHHBBBB",
        18,  # bLength
        USB_DT_DEVICE,
        0x0200,  # bcdUSB
        0x00,  # bDeviceClass (per interface)
        0x00,
        0x00,
        64,  # bMaxPacketSize0
        0x18D1,  # idVendor Google
        pid,   # idProduct: PID_NORMAL pre-accessory, PID_ACCESSORY after ACCESSORY_START
        0x0100,  # bcdDevice
        1,  # iManufacturer
        2,  # iProduct
        3,  # iSerial
        1,  # bNumConfigurations
    )


def config_descriptor():
    # config (9) + interface (9) + ep_out (7) + ep_in (7) = 32
    total = 9 + 9 + 7 + 7
    cfg = struct.pack(
        "<BBHBBBBB",
        9,
        USB_DT_CONFIG,
        total,
        1,  # bNumInterfaces
        1,  # bConfigurationValue
        0,  # iConfiguration
        0x80,  # bmAttributes bus powered
        250,  # 500mA in 2mA units
    )
    iface = struct.pack(
        "<BBBBBBBBB",
        9,
        USB_DT_INTERFACE,
        0,  # bInterfaceNumber
        0,  # alt
        2,  # bNumEndpoints
        0xFF,
        0xFF,
        0x00,
        0,  # iInterface
    )
    ep_out = struct.pack("<BBBBHB", 7, USB_DT_ENDPOINT, 0x01, USB_ENDPOINT_XFER_BULK, 512, 0)
    ep_in = struct.pack("<BBBBHB", 7, USB_DT_ENDPOINT, 0x81, USB_ENDPOINT_XFER_BULK, 512, 0)
    return cfg + iface + ep_out + ep_in


def string_descriptor(s: str, lang: bool = False):
    if lang:
        # LANGID list: 0x0409 English US
        return struct.pack("<BBH", 4, USB_DT_STRING, 0x0409)
    raw = s.encode("utf-16-le")
    return struct.pack("<BB", 2 + len(raw), USB_DT_STRING) + raw


def qualifier_descriptor():
    return struct.pack(
        "<BBHBBBBBH",
        10,
        USB_DT_DEVICE_QUALIFIER,
        0x0200,
        0,
        0,
        0,
        64,
        1,
        0,
    )
