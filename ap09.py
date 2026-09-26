#!/usr/bin/env python3
"""
ap09: import/export tool for the Ammoon AP-09 nano looper (Rowin OEM chip).

The pedal enumerates as USB ID 0416:5555, with a MIDIStreaming interface
("DFU-Midi") exposing bulk endpoints 0x81 (IN) and 0x02 (OUT). Despite the
USB class, the wire protocol is raw bulk transfer, not USB-MIDI event
packets: SysEx-framed command/response messages go directly over the bulk
endpoints.

Protocol is only partially reverse-engineered so far (see CLAUDE.md and
PROTOCOL.md in this directory). This tool currently focuses on `probe`,
for sending arbitrary SysEx bytes and inspecting the raw response while the
rest of the protocol is being worked out.
"""

import argparse
import sys

import usb.core
import usb.util

VID = 0x0416
PID = 0x5555

ENDPOINT_IN = 0x81
ENDPOINT_OUT = 0x02

# Interface 1 is the MIDIStreaming interface with the bulk endpoints.
# Interface 0 is Audio Control, no endpoints, but the kernel driver still
# needs detaching on both.
INTERFACE = 1


class DeviceError(Exception):
    pass


def open_device():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        raise DeviceError(f"device {VID:04x}:{PID:04x} not found")

    for cfg in dev:
        for intf in cfg:
            if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                dev.detach_kernel_driver(intf.bInterfaceNumber)

    dev.set_configuration()
    usb.util.claim_interface(dev, INTERFACE)
    return dev


def close_device(dev):
    try:
        usb.util.release_interface(dev, INTERFACE)
        dev.attach_kernel_driver(INTERFACE)
    except usb.core.USBError:
        pass


CABLE = 0  # USB-MIDI cable number; device only exposes one cable/port.

# USB-MIDI Code Index Numbers (CIN) used for SysEx framing, per the
# "Universal Serial Bus Device Class Definition for MIDI Devices" spec.
CIN_SYSEX_START_OR_CONT = 0x4  # 3 data bytes, more to come
CIN_SYSEX_END_1 = 0x5  # 1 data byte, ends with F7
CIN_SYSEX_END_2 = 0x6  # 2 data bytes, ends with F7
CIN_SYSEX_END_3 = 0x7  # 3 data bytes, ends with F7


def encode_usb_midi(payload: bytes) -> bytes:
    """Wrap a raw MIDI byte stream (e.g. a full F0..F7 SysEx message) into
    USB-MIDI 4-byte Event Packets."""
    packets = bytearray()
    i = 0
    n = len(payload)
    while i < n:
        remaining = n - i
        chunk = payload[i : i + 3]
        is_last = remaining <= 3
        if is_last:
            cin = {1: CIN_SYSEX_END_1, 2: CIN_SYSEX_END_2, 3: CIN_SYSEX_END_3}[len(chunk)]
        else:
            cin = CIN_SYSEX_START_OR_CONT
        header = (CABLE << 4) | cin
        packets.append(header)
        packets.extend(chunk)
        packets.extend(b"\x00" * (3 - len(chunk)))
        i += len(chunk)
    return bytes(packets)


def decode_usb_midi(packets: bytes) -> bytes:
    """Extract the raw MIDI byte stream back out of USB-MIDI Event Packets."""
    data = bytearray()
    for i in range(0, len(packets) - 3, 4):
        header, b0, b1, b2 = packets[i : i + 4]
        cin = header & 0x0F
        chunk = bytes([b0, b1, b2])
        if cin == CIN_SYSEX_START_OR_CONT:
            data.extend(chunk)
        elif cin == CIN_SYSEX_END_1:
            data.extend(chunk[:1])
        elif cin == CIN_SYSEX_END_2:
            data.extend(chunk[:2])
        elif cin == CIN_SYSEX_END_3:
            data.extend(chunk[:3])
        # other CINs (note on/off, etc.) are ignored; this device only
        # exchanges SysEx over this bulk pipe as far as we've seen.
    return bytes(data)


def send_sysex(dev, payload: bytes, timeout=3000) -> bytes:
    """Send a raw SysEx message (including F0..F7) and return the raw reply,
    both wrapped/unwrapped in USB-MIDI Event Packets under the hood."""
    if not (payload.startswith(b"\xf0") and payload.endswith(b"\xf7")):
        raise ValueError("payload must be a full SysEx message (F0 ... F7)")

    dev.write(ENDPOINT_OUT, encode_usb_midi(payload), timeout=timeout)
    try:
        raw = dev.read(ENDPOINT_IN, 4096, timeout=timeout)
    except usb.core.USBError as e:
        raise DeviceError(f"no reply: {e}") from e
    return decode_usb_midi(bytes(raw))


def parse_hex_bytes(text: str) -> bytes:
    text = text.strip()
    parts = text.replace(",", " ").split()
    return bytes(int(p, 16) for p in parts)


def cmd_probe(args):
    payload = parse_hex_bytes(args.sysex)
    dev = open_device()
    try:
        reply = send_sysex(dev, payload, timeout=args.timeout)
    finally:
        close_device(dev)

    print(f"sent {len(payload)} bytes: {payload.hex(' ')}")
    print(f"recv {len(reply)} bytes: {reply.hex(' ')}")


def cmd_list(args):
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        print(f"device {VID:04x}:{PID:04x} not found")
        sys.exit(1)
    print(f"found {VID:04x}:{PID:04x} on bus {dev.bus} address {dev.address}")
    print(dev)


def main():
    argp = argparse.ArgumentParser(description=__doc__)
    sub = argp.add_subparsers(dest="command", required=True)

    p_probe = sub.add_parser("probe", help="send a raw SysEx message, print the raw reply")
    p_probe.add_argument(
        "sysex",
        help='SysEx bytes as hex, e.g. "F0 00 32 45 00 00 00 40 7f F7"',
    )
    p_probe.add_argument("--timeout", type=int, default=3000, help="USB timeout in ms")
    p_probe.set_defaults(func=cmd_probe)

    p_list = sub.add_parser("list", help="show USB descriptor info for the device")
    p_list.set_defaults(func=cmd_list)

    args = argp.parse_args()
    try:
        args.func(args)
    except DeviceError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
