#!/usr/bin/env python3
"""
ap09: import/export loops on the Ammoon AP-09 nano looper (Rowin OEM, USB 0416:5555).

See README.md for usage and for the reverse-engineered protocol.

Copyright (c) 2026 wdog <wdog666@gmail.com>
SPDX-License-Identifier: MIT  (see LICENSE; keep this notice in copies and derivatives)
"""

__author__ = "wdog <wdog666@gmail.com>"
__license__ = "MIT"
__version__ = "0.3"

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
CMD_24 = 0x24  # checksum: body area, addr LE32, len LE32 -> echo + u16 sum of the bytes

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
        self.dev = dev
        self.detached = []
        for cfg in dev:
            for intf in cfg:
                if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                    dev.detach_kernel_driver(intf.bInterfaceNumber)
                    self.detached.append(intf.bInterfaceNumber)
        # Always (re)select the configuration: it resets the endpoints' state, which
        # the firmware needs after a previous session (skipping it -> pipe errors).
        dev.set_configuration()
        usb.util.claim_interface(dev, INTERFACE)
        self._sync()

    def _drain(self):
        """Throw away reply bytes left over from an interrupted previous run."""
        for _ in range(10000):
            try:
                self.dev.read(EP_IN, 4096, timeout=50)
            except usb.core.USBError:
                return

    def _sync(self):
        """Make sure the pipe is clean: drain, then check that 'info' answers properly.
        If not (stalled endpoint after an aborted transfer), USB-reset the pedal once.
        clear_halt is avoided on purpose: this firmware wedges after it."""
        for attempt in range(2):
            self._drain()
            try:
                self.command(CMD_INFO, timeout=1000)
                return
            except DeviceError:
                if attempt:
                    raise
            try:
                usb.util.release_interface(self.dev, INTERFACE)
                self.dev.reset()
                usb.util.claim_interface(self.dev, INTERFACE)
            except usb.core.USBError as e:
                raise DeviceError(f"pedal not responding and USB reset failed ({e}); unplug/replug it") from e

    def close(self):
        try:
            usb.util.release_interface(self.dev, INTERFACE)
        except usb.core.USBError:
            pass
        for i in self.detached:  # give the MIDI port back to snd-usb-audio
            try:
                self.dev.attach_kernel_driver(i)
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
        if len(reply) < 7 + n or reply[6 + n] != checksum(rbody):
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

    def checksum(self, area: int, addr: int, length: int) -> int:
        """16-bit sum of `length` bytes computed on the device (cmd 0x24)."""
        body = bytes([area]) + addr.to_bytes(4, "little") + length.to_bytes(4, "little")
        r = self.command(CMD_24, body, timeout=30000)
        if r[:9] != body:
            raise DeviceError("checksum reply does not echo the request")
        return int.from_bytes(r[9:11], "little")

    def block_is_erased(self, blk: int) -> bool:
        a = blk * BLOCK_SIZE
        # Quick reject (~5 ms): used blocks almost always have data in their first bytes.
        # Page 63 is checked too, because a loop's first chunk lives there.
        if self.read(AREA_NAND, a, 16) != b"\xff" * 16 or \
                self.read(AREA_NAND, a + (PAGES_PER_BLOCK - 1) * PAGE_SIZE, 16) != b"\xff" * 16:
            return False
        n = BLOCK_SIZE - 1  # odd length so an all-FF block (0xff01) can't be confused with all-00
        return self.checksum(AREA_NAND, a, n) == (0xFF * n) & 0xFFFF  # full check, ~0.1 s

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

RECORD_HEAD = 16  # bytes before the block list


def record_length(rec: bytes) -> int:
    pages_hi = int.from_bytes(rec[12:14], "little")   # whole blocks
    pages_lo = rec[14]                                # extra whole pages
    tail = rec[15]                                    # 186-byte units in last page, minus 1
    return (pages_hi * PAGES_PER_BLOCK + pages_lo) * PAGE_AUDIO + (tail + 1) * TAIL_UNIT


def has_loop_fields(rec: bytes) -> bool:
    """True if the record carries a loop description (length/blocks). Clear records written
    by this tool do (they name the loop being removed); legacy/pedal ones may be all FF."""
    return rec[:7] == b"looper\x00" and rec[12:16] != b"\xff" * 4 and \
        rec[RECORD_HEAD:RECORD_HEAD + 2] not in (b"\xff\xff", b"\x00\x00")


def parse_record(rec: bytes, allow_cleared=False):
    """Decode one 'looper' index record (needs the full block list). None if not valid.
    With allow_cleared, a clear record that names a loop is decoded too."""
    if rec[:7] != b"looper\x00":
        return None
    if rec[8] == 0 and not (allow_cleared and has_loop_fields(rec)):
        return None
    length = record_length(rec)
    nblocks = -(-length // (PAGE_AUDIO * PAGES_PER_BLOCK))
    if nblocks > MAX_BLOCKS:
        return None
    return {
        "seq": int.from_bytes(rec[10:12], "little"),
        "length": length,
        "blocks": list(struct.unpack_from(f"<{nblocks}H", rec, RECORD_HEAD)),
    }


def read_index(lp: Looper):
    """Walk the record log in the index block, oldest first.

    Returns (records, last_offset): every 'looper' record found, each as a dict with
    'offset' and 'valid' (plus 'length'/'blocks'/'seq' when valid), and the offset of
    the newest record (None if the log is empty). The newest record is the current
    state; if its valid flag is 0 the pedal has no loop.
    """
    recs, last_off = [], None
    base = INDEX_BLOCK * BLOCK_SIZE
    for off in range(0, BLOCK_SIZE, RECORD_STRIDE):
        head = lp.read(AREA_NAND, base + off, 0x60)
        if head[:6] == b"\xff" * 6:
            break  # rest of the log is unwritten
        if head[:7] != b"looper\x00":
            continue
        last_off = off
        rec = head
        if head[8] or has_loop_fields(head):
            need = RECORD_HEAD + 2 * -(-record_length(head) // (PAGE_AUDIO * PAGES_PER_BLOCK))
            if need > len(head):
                rec = head + lp.read(AREA_NAND, base + off + len(head), min(need, RECORD_SIZE) - len(head))
        valid = bool(head[8])
        r = parse_record(rec, allow_cleared=True) or {}
        r.update(offset=off, valid=valid and bool(r))
        recs.append(r)
    return recs, last_off


def scan_index(lp: Looper):
    """Returns (current_loop_or_None, last_offset_or_None, blocks referenced by any record)."""
    recs, last_off = read_index(lp)
    used = set(b for r in recs if "blocks" in r for b in r["blocks"])
    current = recs[-1] if recs and recs[-1]["valid"] else None
    return current, last_off, used


def all_records(lp: Looper, include_deleted=False):
    """Distinct loops in the index, oldest first, one entry per audio.

    The index is an append-only log. Replaying it:
      valid record        -> that loop becomes current (and is no longer deleted)
      clear record naming a loop -> that loop is deleted (hidden from the list)
      clear record without a loop (pedal / older tool) -> the loop current at that moment is deleted
    Records pointing at the same audio (e.g. after 'select') are merged: each loop appears once.
    'current' marks the loop the pedal plays; 'deleted' loops are dropped unless include_deleted.
    """
    raw, _ = read_index(lp)
    loops, current, deleted = {}, None, set()
    for pos, r in enumerate(raw):
        key = (r["length"], tuple(r["blocks"])) if "blocks" in r else None
        if key and key not in loops:
            loops[key] = dict(r, first=pos, records=0)
        if key:
            loops[key]["records"] += 1
        if r["valid"]:
            current = key
            deleted.discard(key)
        else:
            target = key or current
            if target:
                deleted.add(target)
            current = None
    recs = sorted(loops.values(), key=lambda r: r["first"])
    for r in recs:
        k = (r["length"], tuple(r["blocks"]))
        r["current"] = k == current
        r["deleted"] = k in deleted
        r["same_as"] = None  # duplicates are merged, kept for callers that check it
        # Audio is written once, when the loop first appears; a different loop that shows
        # up later and uses one of its blocks has overwritten that part.
        later = set(b for x in recs if x["first"] > r["first"] for b in x["blocks"])
        r["overwritten"] = len(set(r["blocks"]) & later)
    return recs if include_deleted else [r for r in recs if not r["deleted"]]


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


def _report(progress, fraction: float, text: str, end=False):
    """progress: True = bar on stderr, callable(fraction, text) = GUI, falsy = silent."""
    if callable(progress):
        progress(fraction, text)
    elif progress:
        width = 28
        fill = int(round(max(0.0, min(1.0, fraction)) * width))
        bar = "█" * fill + "░" * (width - fill)
        line = f"\r  [{bar}] {100 * fraction:5.1f}%  {text}"
        print(f"{line:<110}", end="\n" if end else "", file=sys.stderr, flush=True)


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
            if done % 16 == 0:
                _report(progress, done / total_pages, f"reading page {done}/{total_pages}")
    _report(progress, 1.0, f"reading page {total_pages}/{total_pages}", end=True)
    return bytes(pcm)


def load_audio(path: str) -> bytes:
    """Return raw mono 24-bit LE PCM at 46875 Hz.

    A WAV already in the pedal's format is read directly; anything else
    (other WAV formats, mp3, flac, ...) is converted with ffmpeg.
    """
    import os
    if not os.path.isfile(path):
        raise DeviceError(f"file not found: {path}")
    if not os.access(path, os.R_OK):
        raise DeviceError(f"cannot read file: {path}")
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

    # Find the index slot first, so we fail before writing any audio.
    slot = find_record_slot(lp, last_off)

    # Same order as the official tool (0x406ca0): start at a random block, go up,
    # wrap around, skip blocks referenced by any record still in the index log.
    start = random.randrange(INDEX_BLOCK)
    order = list(range(start, INDEX_BLOCK)) + list(range(0, start))
    candidates = [b for b in order if b not in used]

    # The erase command answers OK but does nothing on this firmware, so only blocks
    # that are already erased (all FF) can be programmed cleanly. Reserve all of them
    # before writing anything, so a too-long file fails without wasting blocks.
    checked = [0]

    def next_empty():
        # (no erase here: this firmware ignores it, it would only cost a round trip)
        while candidates:
            blk = candidates.pop(0)
            checked[0] += 1
            if checked[0] % 10 == 0:
                _report(progress, 0.15 * len(reserve) / nblocks,
                        f"searching empty memory: {len(reserve)}/{nblocks} found, {checked[0]} blocks checked")
            if lp.block_is_erased(blk):
                return blk
        return None

    reserve = []
    for n in range(nblocks):
        blk = next_empty()
        if blk is None:
            per_block_s = per_block / SAMPLE_WIDTH / SAMPLE_RATE
            raise DeviceError(
                f"not enough empty memory: need {nblocks} blocks ({len(pcm) / SAMPLE_WIDTH / SAMPLE_RATE:.1f} s), "
                f"found {len(reserve)} (~{len(reserve) * per_block_s:.0f} s). Nothing was written.")
        reserve.append(blk)
        _report(progress, 0.15 * (n + 1) / nblocks,
                f"searching empty memory: {n + 1}/{nblocks} found, {checked[0]} blocks checked",
                end=n + 1 == nblocks)

    blocks = []
    pos = 0
    while pos < len(pcm):
        blk = reserve.pop(0) if reserve else next_empty()
        if blk is None:
            raise DeviceError("ran out of empty memory while replacing blocks that failed verification")
        addr = blk * BLOCK_SIZE
        bi = len(blocks)
        image = bytearray(b"\xff" * BLOCK_SIZE)
        block_pcm = pcm[pos:pos + per_block]
        try:
            for chunk in range(-(-len(block_pcm) // PAGE_AUDIO)):
                data = block_pcm[chunk * PAGE_AUDIO:(chunk + 1) * PAGE_AUDIO]
                page = page_for_chunk(bi, chunk)
                lp.write(AREA_NAND, addr + page * PAGE_SIZE, data)
                image[page * PAGE_SIZE:page * PAGE_SIZE + len(data)] = data
            ok = lp.checksum(AREA_NAND, addr, BLOCK_SIZE) == sum(image) & 0xFFFF
        except DeviceError as e:
            print(f"\n  block {blk}: {e}", file=sys.stderr)
            ok = False
        if not ok:
            print(f"\n  block {blk}: verify failed, using another block", file=sys.stderr)
            used.add(blk)
            continue
        blocks.append(blk)
        pos += len(block_pcm)
        _report(progress, 0.15 + 0.85 * pos / len(pcm),
                f"writing block {len(blocks)}/{nblocks}", end=pos >= len(pcm))

    rec = build_record(len(pcm), blocks)
    lp.write(AREA_NAND, INDEX_BLOCK * BLOCK_SIZE + slot * PAGE_SIZE, rec)
    if lp.read(AREA_NAND, INDEX_BLOCK * BLOCK_SIZE + slot * PAGE_SIZE, 0x60) != rec[:0x60]:
        raise DeviceError("index record did not read back correctly")
    return blocks


def find_record_slot(lp: Looper, last_off):
    """Next erased 2-page slot after the newest record in the index block."""
    first = 0 if last_off is None else last_off // PAGE_SIZE + 2
    base = INDEX_BLOCK * BLOCK_SIZE
    for slot in range(first, PAGES_PER_BLOCK - 1, 2):
        a = base + slot * PAGE_SIZE
        if lp.checksum(AREA_NAND, a, RECORD_SIZE) == (0xFF * RECORD_SIZE) & 0xFFFF and \
                lp.read(AREA_NAND, a, 16) == b"\xff" * 16:
            return slot
    # The official tool erases the index block here (0x407cfa), but erase does
    # nothing on this firmware. Recording a loop on the pedal makes it rotate
    # its own index, which frees slots again.
    raise DeviceError("loop index block is full; record any loop on the pedal once, then retry")


def write_wav(path: str, pcm: bytes, rate=SAMPLE_RATE):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(rate)
        w.writeframes(pcm[:len(pcm) - len(pcm) % SAMPLE_WIDTH])


# ---------------------------------------------------------------- CLI

def cmd_info(lp, args):
    info = lp.info()
    model = int.from_bytes(lp.read(AREA_MCU, 0x2180, 16)[4:8], "little")
    loop = current_loop(lp)
    ok = model == 0x2715
    rows = [
        ("device", f"{VID:04x}:{PID:04x}  bus {lp.dev.bus} addr {lp.dev.address}", None),
        ("model", f"0x{model:04x} " + ("NANO LOOPER" if ok else "unknown"), "32" if ok else "31"),
        ("info", info[:16].decode("ascii", "replace") + " " + info[16:].hex(" "), "2"),
        ("format", f"mono  24-bit  {SAMPLE_RATE} Hz", None),
    ]
    if loop:
        secs = loop["length"] / SAMPLE_WIDTH / SAMPLE_RATE
        rows += [
            ("loop", f"▶ {secs:.2f} s", "1;32"),
            ("size", f"{loop['length']} B  {len(loop['blocks'])} blocks  first {loop['blocks'][0]}", None),
            ("index", f"record @ +{loop['offset']:#x}  seq {loop['seq']:#x}", "2"),
        ]
    else:
        rows.append(("loop", "■ none", "1;31"))
    for key, val, color in rows:
        print(f"{_color('1', f'{key:<7}')} {_color(color, val) if color else val}")


def _color(code: str, text: str) -> str:
    import os
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{code}m{text}\033[0m"


STATES = {  # key: (icon, label, ANSI color, legend)
    "playing": ("▶", "playing", "1;32", "loop the pedal plays now"),
    "saved": ("●", "saved", "36", "old loop, not playing, audio still in memory -> select N / download -r N"),
    "damaged": ("⚠", "damaged", "33", "old loop, N/M blocks overwritten by a later one"),
    "dup": ("↺", "duplicate", "2", "same audio as another entry"),
    "deleted": ("✖", "deleted", "2", "removed with clear/delete; audio may still be in memory -> select N"),
}


def cmd_list(lp, args):
    recs = all_records(lp, include_deleted=args.all)
    current = next((i for i, r in enumerate(recs) if r["current"]), None)
    if current is None:
        print(_color("1;31", "■ no loop"))
    else:
        secs = recs[current]["length"] / SAMPLE_WIDTH / SAMPLE_RATE
        print(_color("1;32", f"▶ playing #{current}  {secs:.2f} s"))
    if not recs:
        return
    print()
    print(_color("1", " #    length  blocks  state"))
    seen = []
    for i, r in enumerate(recs):
        secs = r["length"] / SAMPLE_WIDTH / SAMPLE_RATE
        if r["current"]:
            key, extra = "playing", ""
        elif r["deleted"]:
            key, extra = "deleted", ""
        elif r["same_as"] is not None:
            key, extra = "dup", f" of #{r['same_as']}"
        elif r["overwritten"]:
            key, extra = "damaged", f" {r['overwritten']}/{len(r['blocks'])}"
        else:
            key, extra = "saved", ""
        icon, label, color, _ = STATES[key]
        print(f"{i:2d}  {secs:7.2f}s  {len(r['blocks']):6d}  " + _color(color, f"{icon} {label}{extra}"))
        if key not in seen:
            seen.append(key)
    print()
    for key in seen:
        icon, label, color, legend = STATES[key]
        print(_color("2", f"{icon} {label:<9} {legend}"))
    if "saved" in seen:
        print(_color("2", "  history stays until the pedal compacts its index (seen when the log was half full)"))


def cmd_download(lp, args):
    if args.all:
        import os
        os.makedirs(args.file, exist_ok=True)
        recs = all_records(lp)
        for i, r in enumerate(recs):
            if r["same_as"] is not None:
                continue
            path = os.path.join(args.file, f"loop{i:02d}{'-current' if r['current'] else ''}.wav")
            print(f"#{i}: {r['length'] / SAMPLE_WIDTH / SAMPLE_RATE:.2f} s -> {path}", file=sys.stderr)
            write_wav(path, read_loop_pcm(lp, r))
        return
    if args.record is not None:
        recs = all_records(lp)
        if not 0 <= args.record < len(recs):
            raise DeviceError(f"no record #{args.record} (see 'list')")
        loop = recs[args.record]
    else:
        loop = current_loop(lp)
    if not loop:
        raise DeviceError("no loop stored on the pedal")
    secs = loop["length"] / SAMPLE_WIDTH / SAMPLE_RATE
    print(f"downloading {secs:.2f} s loop -> {args.file}", file=sys.stderr)
    write_wav(args.file, read_loop_pcm(lp, loop))
    print(f"saved {args.file} (mono, 24-bit, {SAMPLE_RATE} Hz)", file=sys.stderr)


def cmd_upload(lp, args):
    model = int.from_bytes(lp.read(AREA_MCU, 0x2180, 16)[4:8], "little")
    if model != 0x2715 and not args.force:
        raise DeviceError(f"model id 0x{model:04x} is not a NANO LOOPER (0x2715); refusing to write (--force)")
    pcm = load_audio(args.file)
    secs = len(pcm) / SAMPLE_WIDTH / SAMPLE_RATE
    print(f"uploading {args.file} ({secs:.2f} s); this replaces the loop on the pedal", file=sys.stderr)
    blocks = upload_loop(lp, pcm)
    print(f"done: {len(blocks)} blocks written. Unplug/replug the pedal so it reloads the loop.",
          file=sys.stderr)


def cmd_delete(lp, args):
    recs = all_records(lp)
    if not 0 <= args.record < len(recs):
        raise DeviceError(f"no loop #{args.record} (see 'list')")
    r = recs[args.record]
    if not args.yes:
        what = "the loop the pedal plays (the pedal will have no loop)" if r["current"] else "an old loop"
        answer = input(f"delete loop #{args.record} ({r['length'] / SAMPLE_WIDTH / SAMPLE_RATE:.2f} s), {what}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes", "s", "si", "sì"):
            print("aborted", file=sys.stderr)
            return
    delete_loop(lp, r)
    print(f"loop #{args.record} deleted from the list (numbers shift; see 'list')."
          + (" Unplug/replug the pedal." if r["current"] else ""), file=sys.stderr)


def cmd_select(lp, args):
    recs = all_records(lp, include_deleted=args.all)
    if not 0 <= args.record < len(recs):
        raise DeviceError(f"no record #{args.record} (see 'list')")
    r = recs[args.record]
    if r["current"]:
        print(f"loop #{args.record} is already the one the pedal plays; nothing written", file=sys.stderr)
        return
    if r["overwritten"]:
        print(f"warning: {r['overwritten']} blocks of loop #{args.record} were reused later; "
              "it will play partly corrupted", file=sys.stderr)
    select_loop(lp, r)
    print(f"loop #{args.record} ({r['length'] / SAMPLE_WIDTH / SAMPLE_RATE:.2f} s) is now current. "
          "Unplug/replug the pedal so it reloads the loop.", file=sys.stderr)


def append_record(lp: Looper, rec: bytes):
    """Write a record into the next empty slot of the index log and read it back."""
    _, last_off, _ = scan_index(lp)
    slot = find_record_slot(lp, last_off)
    addr = INDEX_BLOCK * BLOCK_SIZE + slot * PAGE_SIZE
    lp.write(AREA_NAND, addr, rec)
    if lp.read(AREA_NAND, addr, 0x60) != rec[:0x60]:
        raise DeviceError("index record did not read back correctly")


def select_loop(lp: Looper, loop):
    """Make an existing loop (from all_records) the current one."""
    append_record(lp, build_record(loop["length"], loop["blocks"]))


def clear_loop(lp: Looper):
    """Leave the pedal without a loop. The removed loop is named in the clear record, so it
    disappears from the list (its audio stays in memory until reused)."""
    current = scan_index(lp)[0]
    append_record(lp, build_clear_record(current))
    if scan_index(lp)[0] is not None:
        raise DeviceError("clear record did not take effect")


def delete_loop(lp: Looper, loop):
    """Hide a loop from the list. If it is the playing one this is clear_loop; otherwise a
    clear record naming it is written and the playing loop is re-selected right after
    (the pedal only looks at the newest record). Uses 1 or 2 index slots."""
    current = scan_index(lp)[0]
    if current and current["blocks"] == loop["blocks"] and current["length"] == loop["length"]:
        clear_loop(lp)
        return
    append_record(lp, build_clear_record(loop))
    if current:
        append_record(lp, build_record(current["length"], current["blocks"]))


def build_clear_record(loop=None) -> bytes:
    """Record with the valid flag at 0: the pedal then has no loop. Like the pedal's own clear
    records, it keeps the removed loop's length/blocks, which the list uses to hide it."""
    if loop:
        rec = bytearray(build_record(loop["length"], loop["blocks"]))
    else:
        rec = bytearray(b"\xff" * RECORD_SIZE)
        rec[0:7] = b"looper\x00"
        rec[RECORD_SIZE - 8:RECORD_SIZE - 1] = b"looper\x00"
    rec[8] = 0
    return bytes(rec)


def cmd_clear(lp, args):
    current, last_off, _ = scan_index(lp)
    if not current and not args.yes:
        print("the pedal already has no loop", file=sys.stderr)
        return
    if not args.yes:
        secs = current["length"] / SAMPLE_WIDTH / SAMPLE_RATE
        answer = input(f"remove the current {secs:.2f} s loop from the pedal? "
                       "(it stays in 'list' and can come back with 'select') [y/N] ")
        if answer.strip().lower() not in ("y", "yes", "s", "si", "sì"):
            print("aborted", file=sys.stderr)
            return
    clear_loop(lp)
    print("the pedal now has no loop. Unplug/replug the pedal so it reloads.", file=sys.stderr)


PER_BLOCK_S = PAGE_AUDIO * PAGES_PER_BLOCK / SAMPLE_WIDTH / SAMPLE_RATE  # ~0.93 s of audio per block


def count_space(lp: Looper, progress=True):
    """Return (empty_blocks, free_index_slots). Full scan, ~0.1 s per block."""
    _, last_off, used = scan_index(lp)
    free = 0
    for blk in range(INDEX_BLOCK):
        if blk not in used and lp.block_is_erased(blk):
            free += 1
        if blk % 20 == 0:
            _report(progress, blk / INDEX_BLOCK, f"scanning block {blk}/{INDEX_BLOCK}, {free} empty")
    _report(progress, 1.0, f"scanned {INDEX_BLOCK} blocks, {free} empty", end=True)
    slots = 0
    base = INDEX_BLOCK * BLOCK_SIZE
    first = 0 if last_off is None else last_off // PAGE_SIZE + 2
    for slot in range(first, PAGES_PER_BLOCK - 1, 2):
        if lp.checksum(AREA_NAND, base + slot * PAGE_SIZE, RECORD_SIZE) == (0xFF * RECORD_SIZE) & 0xFFFF:
            slots += 1
    return free, slots


def cmd_space(lp, args):
    free, slots = count_space(lp)
    per_block_s = PER_BLOCK_S
    total_s = free * per_block_s
    max_s = min(free, MAX_BLOCKS) * per_block_s
    col = "32" if free > 60 else "33" if free > 10 else "31"
    print(f"{_color('1', 'free   ')} {_color(col, f'{free} blocks  ~{total_s:.0f} s of audio')}")
    print(f"{_color('1', 'max    ')} {max_s:.0f} s per upload (pedal limit {MAX_BLOCKS * per_block_s / 60:.0f} min)")
    slots_txt = _color("32" if slots > 3 else "31", str(slots))
    print(f"{_color('1', 'slots  ')} {slots_txt} free in the index (upload/select/clear use 1)")


def cmd_probe(lp, args):
    body = bytes.fromhex(args.body) if args.body else b""
    print(lp.command(int(args.cmd, 16), body).hex(" "))


def cmd_dump(lp, args):
    data = lp.read(int(args.area), int(args.addr, 0), int(args.length, 0))
    if args.out:
        with open(args.out, "wb") as f:
            f.write(data)
    else:
        for i in range(0, len(data), 16):
            print(f"{int(args.addr, 0) + i:08x}: {data[i:i + 16].hex(' ')}")


def main():
    fmt = argparse.RawDescriptionHelpFormatter
    ap = argparse.ArgumentParser(
        prog="ap09.py", formatter_class=fmt,
        description=f"Copy loops between an Ammoon AP-09 nano looper (USB 0416:5555) and this computer.\n"
                    f"v{__version__} · by {__author__} · MIT license",
        epilog="""\
typical use:
  ap09.py info                     what is on the pedal
  ap09.py list                     current loop + older loops still in memory
  ap09.py download loop.wav        save the current loop
  ap09.py upload song.mp3          put an audio file on the pedal
  ap09.py select 3                 play old loop #3 again
  ap09.py clear                    leave the pedal without a loop

After upload / select / clear: unplug and replug the pedal so it reloads.
Audio on the pedal: mono, 24-bit, 46875 Hz. Run 'ap09.py COMMAND -h' for details.
Without the udev rule (see README) every command needs sudo.""")
    sub = ap.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser(
        "info", formatter_class=fmt, help="show device model and the current loop",
        description="""\
Show the device info block, the model id (must be 0x2715 = NANO LOOPER) and the
loop the pedal currently plays: length, size, memory blocks and where its index
record is. Read-only.""")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser(
        "list", formatter_class=fmt, help="list current and older loops still in memory",
        description="""\
List the loops in the pedal's index, oldest first, one line per audio (records that
point at the same audio, e.g. after 'select', are merged). The pedal plays only the
one marked playing; the others usually still have their audio in memory and can be
downloaded (download -r N) or made current again (select N).
'damaged N/M': N of its M memory blocks were reused by a later loop. Read-only.""")
    p.add_argument("-a", "--all", action="store_true", help="also show deleted loops (✖)")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser(
        "download", formatter_class=fmt, help="save a loop from the pedal as WAV",
        description="""\
Save a loop as a WAV file: mono, 24-bit PCM, 46875 Hz (the pedal's native
format, bit-exact). About 12 s for a 20 s loop. Read-only on the pedal.

examples:
  ap09.py download loop.wav          current loop
  ap09.py download -r 3 old.wav      loop #3 from 'list'
  ap09.py download -a myloops/       every loop into myloops/loopNN.wav

convert afterwards if needed:
  ffmpeg -i loop.wav -ar 48000 loop48k.wav
  ffmpeg -i loop.wav -b:a 320k loop.mp3""")
    p.add_argument("file", help="output WAV file (a directory with --all)")
    p.add_argument("-r", "--record", type=int, metavar="N",
                   help="download loop #N from 'list' instead of the current one")
    p.add_argument("-a", "--all", action="store_true", help="download every loop into directory FILE")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser(
        "upload", formatter_class=fmt, help="put an audio file on the pedal as the current loop",
        description="""\
Write an audio file to the pedal and make it the current loop.

input: anything ffmpeg can read (wav, mp3, flac, ogg, ...). It is converted to
mono, 24-bit, 46875 Hz; stereo is mixed down. A WAV already in that format is
sent as-is (no ffmpeg needed). Max ~10 min, in practice limited by free space.

how: the pedal's erase command does not work, so only memory blocks that are
already empty are used (check with 'space'); every block is verified with the
pedal's checksum. The previous loop is NOT deleted: it stays in 'list' and can
come back with 'select'. Takes ~1 s per second of audio.

After the upload unplug and replug the pedal.""")
    p.add_argument("file", help="audio file to upload")
    p.add_argument("--force", action="store_true", help="write even if the model id is not NANO LOOPER (0x2715)")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser(
        "select", formatter_class=fmt, help="make an older loop from 'list' current again",
        description="""\
Make loop #N from 'list' the current loop. No audio is copied: a new index
record pointing at that loop's memory is added. If 'list' says the loop was
partly overwritten, it will play partly corrupted. Uses one index slot.

After select unplug and replug the pedal.""")
    p.add_argument("record", type=int, metavar="N", help="loop number from 'list'")
    p.add_argument("-a", "--all", action="store_true", help="N is a number from 'list --all' (restore a deleted loop)")
    p.set_defaults(func=cmd_select)

    p = sub.add_parser(
        "delete", formatter_class=fmt, help="remove a loop from the list",
        description="""\
Remove loop #N (number from 'list') from the list. Deleting the playing loop is the
same as 'clear'. The audio cannot be wiped over USB: it stays in memory until reused,
and 'list --all' / 'select --all N' can still bring it back. Uses 1-2 index slots.""")
    p.add_argument("record", type=int, metavar="N", help="loop number from 'list'")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser(
        "clear", formatter_class=fmt, help="leave the pedal without a loop",
        description="""\
Remove the current loop, like clearing it on the pedal: a 'no loop' record naming
it is added to the index, and it disappears from 'list' (see 'list --all').
Asks for confirmation unless -y is given.

limits: the audio itself cannot be wiped over USB (the erase command is ignored).
Older loops stay in 'list' as history and can be brought back with 'select'
until the pedal compacts its index (observed once at power-on, when the index
log was half full); the audio stays in memory until it gets reused.

After clear unplug and replug the pedal.""")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser(
        "space", formatter_class=fmt, help="how much empty memory is left for uploads (~4 min)",
        description="""\
Scan the whole audio memory (1980 blocks, ~0.1 s each, ~4 min) and report how
many blocks are empty, i.e. usable by 'upload', and how many index slots are
free (upload, select and clear each use one). Read-only.""")
    p.set_defaults(func=cmd_space)

    p = sub.add_parser(
        "dump", formatter_class=fmt, help="(debug) read raw memory",
        description="""\
Read raw memory and print it as hex (or save it with -o). Read-only.

areas: 0 = MCU flash (firmware/settings), 1 = 256 MiB NAND (audio + index)
examples:
  ap09.py dump 1 0xF780000 0x60      first index record
  ap09.py dump 0 0x2180 16           model id is bytes 4..7""")
    p.add_argument("area", help="0 = MCU flash, 1 = NAND")
    p.add_argument("addr", help="start address (e.g. 0xF780000)")
    p.add_argument("length", help="number of bytes (e.g. 0x60)")
    p.add_argument("-o", "--out", metavar="FILE", help="write raw bytes to FILE instead of printing")
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser(
        "probe", formatter_class=fmt, help="(debug) send a raw protocol command",
        description="""\
Send one raw command packet and print the reply body (see README, 'Packet
format'). Only for protocol work: 0x21/0x22 write to the pedal.

examples:
  ap09.py probe 11                                   device info
  ap09.py probe 23 010000780f600000                  read 0x60 bytes of NAND at 0xF780000""")
    p.add_argument("cmd", help="command byte, hex (e.g. 11)")
    p.add_argument("body", nargs="?", help="body bytes, hex")
    p.set_defaults(func=cmd_probe)

    args = ap.parse_args()
    # check the input before touching the pedal
    if args.command == "upload":
        import os
        if not os.path.isfile(args.file):
            sys.exit(f"error: file not found: {args.file}")
    try:
        lp = Looper()
    except usb.core.USBError as e:
        if e.errno == 16:
            sys.exit("error: pedal busy: another ap09.py (or program) is using it; wait for it to finish")
        if e.errno == 13:
            sys.exit("error: permission denied: run with sudo or install the udev rule (see README)")
        sys.exit(f"error: cannot open device: {e}; unplug/replug the pedal and retry")
    except DeviceError as e:
        sys.exit(f"error: {e}")
    try:
        args.func(lp, args)
    except DeviceError as e:
        sys.exit(f"error: {e}")
    except OSError as e:  # file errors (permissions, missing directory, disk full, ...)
        sys.exit(f"error: {e.strerror or e}: {e.filename or ''}".rstrip(": "))
    except KeyboardInterrupt:
        sys.exit("interrupted")
    finally:
        lp.close()


if __name__ == "__main__":
    main()
