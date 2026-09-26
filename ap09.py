#!/usr/bin/env python3
"""
ap09: import/export loops on the Ammoon AP-09 nano looper (Rowin OEM, USB 0416:5555).

See README.md for usage and for the reverse-engineered protocol.
"""

import argparse
import struct
import sys
import wave

import usb.core
import usb.util

VID, PID = 0x0416, 0x5555
EP_IN, EP_OUT = 0x81, 0x02
INTERFACE = 1  # MIDIStreaming interface holding the bulk endpoints

# Packet header and commands (unpacked payload level)
HEADER = b"\x00\x59"
CMD_INFO = 0x11
CMD_ERASE = 0x21
CMD_WRITE = 0x22
CMD_READ = 0x23
CMD_24 = 0x24  # purpose unknown (9-byte body: area, addr LE32, LE32)

AREA_MCU = 0   # MCU internal flash (firmware + settings) - never write here
AREA_NAND = 1  # 256 MiB NAND holding the audio and the loop index

MAX_CHUNK = 0x3F1  # max bytes per read request (official tool's cap)
MAX_WRITE = 0x1400  # max bytes per write request (official tool's cap)

# NAND geometry / audio format for the "NANO LOOPER" model (model id 0x2715),
# from the official tool's model table at 0x48ea20.
BLOCK_SIZE = 0x20000       # 128 KiB erase block
PAGE_SIZE = 0x800          # 2 KiB page
PAGES_PER_BLOCK = 0x40
PAGE_AUDIO = 0x7FE         # audio bytes per page; last 2 bytes of each page are spare (00 00)
TAIL_UNIT = 0xBA           # 186 bytes: granularity of the partial last page
INDEX_BLOCK = 0x7BC        # block 1980: log of "looper" records; audio uses blocks 0..1979
RECORD_STRIDE = 0x1000     # one record every 2 pages
RECORD_SIZE = 0xA40
MAX_BLOCKS = 0x28A         # 650 blocks per loop (~10 min)
SAMPLE_RATE = 46875        # 12 MHz / 256
SAMPLE_WIDTH = 3           # 24-bit signed little-endian, mono


class DeviceError(Exception):
    pass


# ---------------------------------------------------------------- transport

def encode_usb_midi(data: bytes) -> bytes:
    """Wrap a SysEx byte stream into 4-byte USB-MIDI event packets (cable 0)."""
    out = bytearray()
    for i in range(0, len(data), 3):
        chunk = data[i:i + 3]
        last = i + 3 >= len(data)
        cin = (0x4 if not last else {1: 0x5, 2: 0x6, 3: 0x7}[len(chunk)])
        out.append(cin)
        out += chunk + b"\x00" * (3 - len(chunk))
    return bytes(out)


def decode_usb_midi(packets: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(packets) - 3, 4):
        cin = packets[i] & 0x0F
        n = {0x4: 3, 0x5: 1, 0x6: 2, 0x7: 3}.get(cin, 0)
        out += packets[i + 1:i + 1 + n]
    return bytes(out)


def pack7(data: bytes) -> bytes:
    """8-bit bytes -> 7-bit SysEx data bytes (little-endian bitstream)."""
    out, acc, n = bytearray(), 0, 0
    for b in data:
        acc |= b << n
        n += 8
        while n >= 7:
            out.append(acc & 0x7F)
            acc >>= 7
            n -= 7
    if n:
        out.append(acc & 0x7F)
    return bytes(out)


def unpack7(data: bytes) -> bytes:
    out, acc, n = bytearray(), 0, 0
    for b in data:
        acc |= b << n
        n += 7
        if n >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            n -= 8
    return bytes(out)


def checksum(body: bytes) -> int:
    return (~sum(body)) & 0xFF


class Looper:
    def __init__(self):
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            raise DeviceError(f"looper {VID:04x}:{PID:04x} not found (plugged in?)")
        for cfg in dev:
            for intf in cfg:
                if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                    dev.detach_kernel_driver(intf.bInterfaceNumber)
        dev.set_configuration()
        usb.util.claim_interface(dev, INTERFACE)
        self.dev = dev

    def close(self):
        try:
            usb.util.release_interface(self.dev, INTERFACE)
            self.dev.attach_kernel_driver(INTERFACE)
        except usb.core.USBError:
            pass

    def raw(self, sysex: bytes, timeout=3000) -> bytes:
        self.dev.write(EP_OUT, encode_usb_midi(sysex), timeout=timeout)
        reply = bytearray()
        while not reply.endswith(b"\xf7"):
            try:
                reply += decode_usb_midi(bytes(self.dev.read(EP_IN, 4096, timeout=timeout)))
            except usb.core.USBError as e:
                raise DeviceError(f"no reply from looper: {e}") from e
        return bytes(reply)

    def transact(self, cmd: int, body: bytes = b"", timeout=3000):
        """Send one packet, return (reply_cmd, reply_body), checksum verified."""
        pkt = HEADER + bytes([cmd]) + len(body).to_bytes(3, "little") + body + bytes([checksum(body)])
        reply = unpack7(self.raw(b"\xf0" + pack7(pkt) + b"\xf7", timeout)[1:-1])
        if reply[:2] != HEADER:
            raise DeviceError(f"bad reply header: {reply[:8].hex(' ')}")
        n = int.from_bytes(reply[3:6], "little")
        rbody = reply[6:6 + n]
        if len(rbody) != n or reply[6 + n] != checksum(rbody):
            raise DeviceError(f"reply checksum/length error for cmd 0x{cmd:02x}")
        return reply[2], rbody

    def command(self, cmd: int, body: bytes = b"", timeout=3000) -> bytes:
        """Command answered with a packet of the same type (info, read)."""
        rcmd, rbody = self.transact(cmd, body, timeout)
        if rcmd != cmd:
            raise DeviceError(f"device rejected cmd 0x{cmd:02x} (status {rbody.hex(' ')})")
        return rbody

    def status_command(self, cmd: int, body: bytes, timeout) -> None:
        """Command answered with a status packet: cmd 0x00, body [code], 0 = OK."""
        rcmd, rbody = self.transact(cmd, body, timeout)
        if rcmd != 0x00 or rbody != b"\x00":
            raise DeviceError(f"cmd 0x{cmd:02x} failed (reply cmd 0x{rcmd:02x}, status {rbody.hex(' ')})")

    def erase_block(self, area: int, addr: int):
        self.status_command(CMD_ERASE, bytes([area]) + addr.to_bytes(4, "little"), timeout=30000)

    def write(self, area: int, addr: int, data: bytes):
        for i in range(0, len(data), MAX_WRITE):
            chunk = data[i:i + MAX_WRITE]
            body = bytes([area]) + (addr + i).to_bytes(4, "little") + len(chunk).to_bytes(3, "little") + chunk
            self.status_command(CMD_WRITE, body, timeout=50000)

    def info(self) -> bytes:
        return self.command(CMD_INFO)

    def read(self, area: int, addr: int, length: int) -> bytes:
        out = bytearray()
        while length > 0:
            n = min(length, MAX_CHUNK)
            body = bytes([area]) + addr.to_bytes(4, "little") + n.to_bytes(3, "little")
            r = self.command(CMD_READ, body)
            if r[:8] != body:
                raise DeviceError("read reply does not echo the request")
            out += r[8:]
            addr += n
            length -= n
        return bytes(out)


# ---------------------------------------------------------------- loop index

def parse_record(rec: bytes):
    """Decode one 'looper' index record. Returns None if not a valid record."""
    if rec[:7] != b"looper\x00" or rec[8] == 0:
        return None
    pages_hi = int.from_bytes(rec[12:14], "little")   # whole blocks
    pages_lo = rec[14]                                # extra whole pages
    tail = rec[15]                                    # 186-byte units in last page, minus 1
    length = (pages_hi * PAGES_PER_BLOCK + pages_lo) * PAGE_AUDIO + (tail + 1) * TAIL_UNIT
    nblocks = -(-length // (PAGE_AUDIO * PAGES_PER_BLOCK))
    blocks = struct.unpack_from(f"<{nblocks}H", rec, 16)
    return {
        "seq": int.from_bytes(rec[10:12], "little"),
        "length": length,
        "blocks": list(blocks),
    }


def scan_index(lp: Looper):
    """Walk the record log in the index block.

    Returns (current_loop_or_None, last_used_offset_or_None, blocks referenced by any record).
    The newest record wins; a record with the valid flag cleared means "no loop".
    """
    current, last_off, used = None, None, set()
    for off in range(0, BLOCK_SIZE, RECORD_STRIDE):
        rec = lp.read(AREA_NAND, INDEX_BLOCK * BLOCK_SIZE + off, 0x60)
        if rec[:6] == b"\xff" * 6:
            break  # rest of the log is unwritten
        if rec[:7] != b"looper\x00":
            continue
        last_off = off
        r = parse_record(rec)
        if r:
            r["offset"] = off
            used.update(r["blocks"])
        current = r
    return current, last_off, used


def current_loop(lp: Looper):
    return scan_index(lp)[0]


def page_for_chunk(block_index: int, chunk: int) -> int:
    """NAND page holding the given 2046-byte audio chunk of a block.

    The first block of a loop is rotated by one page: chunk 0 lives in the last
    page (63), chunk k in page k-1. Every other block is linear. (Official tool,
    download at 0x408900 and upload at 0x408b54.)
    """
    if block_index == 0:
        return PAGES_PER_BLOCK - 1 if chunk == 0 else chunk - 1
    return chunk


def read_loop_pcm(lp: Looper, loop, progress=True) -> bytes:
    pcm = bytearray()
    remaining = loop["length"]
    total_pages = -(-remaining // PAGE_AUDIO)
    done = 0
    for bi, blk in enumerate(loop["blocks"]):
        for chunk in range(PAGES_PER_BLOCK):
            if remaining <= 0:
                break
            page = page_for_chunk(bi, chunk)
            n = min(PAGE_AUDIO, remaining)
            pcm += lp.read(AREA_NAND, blk * BLOCK_SIZE + page * PAGE_SIZE, n)
            remaining -= n
            done += 1
            if progress and done % 64 == 0:
                print(f"\r  {100 * done // total_pages}%", end="", file=sys.stderr, flush=True)
    if progress:
        print("\r  100%", file=sys.stderr)
    return bytes(pcm)


def load_audio(path: str) -> bytes:
    """Return raw mono 24-bit LE PCM at 46875 Hz.

    A WAV already in the pedal's format is read directly; anything else
    (other WAV formats, mp3, flac, ...) is converted with ffmpeg.
    """
    try:
        with wave.open(path, "rb") as w:
            if (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, SAMPLE_WIDTH, SAMPLE_RATE):
                return w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        pass
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        raise DeviceError("input is not mono/24-bit/46875 Hz WAV and ffmpeg is not installed to convert it")
    print(f"converting {path} to mono 24-bit {SAMPLE_RATE} Hz with ffmpeg", file=sys.stderr)
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SAMPLE_RATE),
         "-f", "s24le", "-acodec", "pcm_s24le", "-"],
        capture_output=True)
    if out.returncode != 0:
        raise DeviceError(f"ffmpeg failed: {out.stderr.decode(errors='replace').strip()}")
    return out.stdout


def build_record(length: int, blocks) -> bytes:
    """Index record as written by the official tool (0x407c1e..0x407cf8)."""
    rec = bytearray(b"\xff" * RECORD_SIZE)
    rec[0:7] = b"looper\x00"
    rec[RECORD_SIZE - 8:RECORD_SIZE - 1] = b"looper\x00"
    rec[8] = 1                       # valid
    rec[10:12] = b"\x00\x00"         # sequence (the pedal numbers its own records; tool writes 0)
    units = -(-length // TAIL_UNIT) - 1          # 186-byte units, minus one
    per_page = PAGE_AUDIO // TAIL_UNIT           # 11
    per_block = per_page * PAGES_PER_BLOCK       # 704
    rec[12:14] = (units // per_block).to_bytes(2, "little")
    rec[14] = (units % per_block) // per_page
    rec[15] = (units % per_block) % per_page
    for i, b in enumerate(blocks):
        rec[16 + 2 * i:18 + 2 * i] = b.to_bytes(2, "little")
    return bytes(rec)


def upload_loop(lp: Looper, pcm: bytes, progress=True):
    import random

    pcm = pcm[:len(pcm) - len(pcm) % SAMPLE_WIDTH]
    if not pcm:
        raise DeviceError("audio is empty")
    # the length is stored in 186-byte units: pad the tail with silence
    pcm += bytes(-len(pcm) % TAIL_UNIT)
    per_block = PAGE_AUDIO * PAGES_PER_BLOCK
    nblocks = -(-len(pcm) // per_block)
    if nblocks > MAX_BLOCKS:
        raise DeviceError(f"audio too long: max {MAX_BLOCKS * per_block / SAMPLE_WIDTH / SAMPLE_RATE:.0f} s")

    _, last_off, used = scan_index(lp)

    # Same strategy as the official tool (0x406ca0): start at a random block and
    # take free blocks upwards, wrapping around. Blocks referenced by any record
    # still in the index log are treated as used.
    start = random.randrange(INDEX_BLOCK)
    order = list(range(start, INDEX_BLOCK)) + list(range(0, start))
    candidates = [b for b in order if b not in used]

    blocks = []
    done = 0
    total_pages = -(-len(pcm) // PAGE_AUDIO)
    pos = 0
    while pos < len(pcm):
        if not candidates:
            raise DeviceError("no free NAND blocks left")
        blk = candidates.pop(0)
        try:
            lp.erase_block(AREA_NAND, blk * BLOCK_SIZE)
        except DeviceError as e:
            print(f"\n  block {blk}: erase failed ({e}), skipping", file=sys.stderr)
            continue
        bi = len(blocks)
        block_start = pos
        try:
            for chunk in range(PAGES_PER_BLOCK):
                if pos >= len(pcm):
                    break
                data = pcm[pos:pos + PAGE_AUDIO]
                lp.write(AREA_NAND, blk * BLOCK_SIZE + page_for_chunk(bi, chunk) * PAGE_SIZE, data)
                pos += len(data)
                done += 1
                if progress and done % 32 == 0:
                    print(f"\r  {100 * done // total_pages}%", end="", file=sys.stderr, flush=True)
        except DeviceError as e:
            print(f"\n  block {blk}: write failed ({e}), retrying on another block", file=sys.stderr)
            done -= -(-(pos - block_start) // PAGE_AUDIO)
            pos = block_start
            continue
        blocks.append(blk)
    if progress:
        print("\r  100%", file=sys.stderr)

    # Append the new record to the index log; erase the log block when full (0x407cfa).
    slot = 0 if last_off is None else last_off // PAGE_SIZE + 2
    if slot >= PAGES_PER_BLOCK:
        lp.erase_block(AREA_NAND, INDEX_BLOCK * BLOCK_SIZE)
        slot = 0
    lp.write(AREA_NAND, INDEX_BLOCK * BLOCK_SIZE + slot * PAGE_SIZE, build_record(len(pcm), blocks))
    return blocks


def write_wav(path: str, pcm: bytes, rate=SAMPLE_RATE):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(rate)
        w.writeframes(pcm[:len(pcm) - len(pcm) % SAMPLE_WIDTH])


# ---------------------------------------------------------------- CLI

def cmd_info(lp, args):
    print("device info:", lp.info().hex(" "))
    model = int.from_bytes(lp.read(AREA_MCU, 0x2180, 16)[4:8], "little")
    print(f"model id: 0x{model:04x}" + (" (NANO LOOPER)" if model == 0x2715 else " (not a NANO LOOPER!)"))
    loop = current_loop(lp)
    if not loop:
        print("no loop stored")
        return
    secs = loop["length"] / SAMPLE_WIDTH / SAMPLE_RATE
    print(f"loop: {secs:.2f} s, {loop['length']} bytes, {len(loop['blocks'])} blocks "
          f"(first {loop['blocks'][0]}), index record #{loop['seq']:#x} at +{loop['offset']:#x}")


def cmd_download(lp, args):
    loop = current_loop(lp)
    if not loop:
        raise DeviceError("no loop stored on the pedal")
    secs = loop["length"] / SAMPLE_WIDTH / SAMPLE_RATE
    print(f"downloading {secs:.2f} s loop -> {args.file}", file=sys.stderr)
    write_wav(args.file, read_loop_pcm(lp, loop))
    print(f"saved {args.file} (mono, 24-bit, {SAMPLE_RATE} Hz)", file=sys.stderr)


def cmd_upload(lp, args):
    if not args.experimental:
        raise DeviceError(
            "upload is not working yet: the erase command (0x21) replies OK but does not erase "
            "NAND blocks, so new audio gets ANDed with old data. See README 'Upload status'. "
            "Pass --experimental to run it anyway.")
    model =int.from_bytes(lp.read(AREA_MCU, 0x2180, 16)[4:8], "little")
    if model != 0x2715 and not args.force:
        raise DeviceError(f"model id 0x{model:04x} is not a NANO LOOPER (0x2715); refusing to write (--force)")
    pcm = load_audio(args.file)
    secs = len(pcm) / SAMPLE_WIDTH / SAMPLE_RATE
    print(f"uploading {args.file} ({secs:.2f} s); this replaces the loop on the pedal", file=sys.stderr)
    blocks = upload_loop(lp, pcm)
    loop = current_loop(lp)
    if not loop or loop["blocks"] != blocks:
        raise DeviceError("index record did not read back correctly")
    print(f"done: {len(blocks)} blocks written. Unplug/replug the pedal so it reloads the loop.",
          file=sys.stderr)


def cmd_probe(lp, args):
    body = bytes.fromhex(args.body) if args.body else b""
    print(lp.command(int(args.cmd, 16), body).hex(" "))


def cmd_dump(lp, args):
    data = lp.read(int(args.area), int(args.addr, 0), int(args.length, 0))
    if args.out:
        open(args.out, "wb").write(data)
    else:
        for i in range(0, len(data), 16):
            print(f"{int(args.addr, 0) + i:08x}: {data[i:i + 16].hex(' ')}")


def main():
    ap = argparse.ArgumentParser(description="Ammoon AP-09 nano looper USB tool")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="show device and stored loop info").set_defaults(func=cmd_info)

    p = sub.add_parser("download", help="save the loop stored on the pedal as a WAV file")
    p.add_argument("file")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("upload", help="replace the loop on the pedal with an audio file (wav/mp3/flac/...)")
    p.add_argument("file")
    p.add_argument("--force", action="store_true", help="write even if the model id is not NANO LOOPER")
    p.add_argument("--experimental", action="store_true", help="run the (currently broken) upload anyway")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("dump", help="(debug) read raw memory")
    p.add_argument("area", help="0 = MCU flash, 1 = NAND")
    p.add_argument("addr")
    p.add_argument("length")
    p.add_argument("-o", "--out")
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("probe", help="(debug) send a raw command, e.g. probe 11")
    p.add_argument("cmd", help="command byte, hex")
    p.add_argument("body", nargs="?", help="body bytes, hex")
    p.set_defaults(func=cmd_probe)

    args = ap.parse_args()
    try:
        lp = Looper()
    except usb.core.USBError as e:
        sys.exit(f"error: cannot open device: {e} (run with sudo or install the udev rule)")
    except DeviceError as e:
        sys.exit(f"error: {e}")
    try:
        args.func(lp, args)
    except DeviceError as e:
        sys.exit(f"error: {e}")
    finally:
        lp.close()


if __name__ == "__main__":
    main()
