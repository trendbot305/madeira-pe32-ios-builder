#!/usr/bin/env python3
"""Shrink an unsigned arm64 Mach-O executable's __PAGEZERO reservation.

The iOS linker emits a 4 GiB __PAGEZERO segment for 64-bit executables.
Wine WoW64 needs to reserve guest memory below 2 GiB. This post-link patch
keeps __TEXT at its original address and only reduces __PAGEZERO, making that
low range available if the iOS loader accepts the modified load command.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import struct
import sys

MH_MAGIC_64 = 0xFEEDFACF
LC_SEGMENT_64 = 0x19
MACH_HEADER_64_SIZE = 32
SEGMENT_COMMAND_64_SIZE = 72
VM_PAGE_SIZE = 0x4000


def parse_size(value: str) -> int:
    size = int(value, 0)
    if size < VM_PAGE_SIZE or size % VM_PAGE_SIZE:
        raise argparse.ArgumentTypeError(
            f"size must be at least and aligned to 0x{VM_PAGE_SIZE:x}"
        )
    return size


def patch_pagezero(path: Path, new_size: int) -> tuple[int, int]:
    data = bytearray(path.read_bytes())
    if len(data) < MACH_HEADER_64_SIZE:
        raise ValueError("file is too small to be a Mach-O executable")

    magic, = struct.unpack_from("<I", data, 0)
    if magic != MH_MAGIC_64:
        raise ValueError(
            f"expected a thin little-endian 64-bit Mach-O (magic 0x{MH_MAGIC_64:x}); "
            f"found 0x{magic:x}"
        )

    filetype, = struct.unpack_from("<I", data, 12)
    if filetype != 2:  # MH_EXECUTE
        raise ValueError(f"expected MH_EXECUTE (2); found filetype {filetype}")

    ncmds, sizeofcmds = struct.unpack_from("<II", data, 16)
    commands_end = MACH_HEADER_64_SIZE + sizeofcmds
    if commands_end > len(data):
        raise ValueError("Mach-O load commands extend beyond end of file")

    offset = MACH_HEADER_64_SIZE
    found = False
    old_size = 0
    for _ in range(ncmds):
        if offset + 8 > commands_end:
            raise ValueError("truncated Mach-O load command")
        cmd, cmdsize = struct.unpack_from("<II", data, offset)
        if cmdsize < 8 or offset + cmdsize > commands_end:
            raise ValueError("invalid Mach-O load command size")

        if cmd == LC_SEGMENT_64:
            if cmdsize < SEGMENT_COMMAND_64_SIZE:
                raise ValueError("truncated LC_SEGMENT_64 command")
            segname = bytes(data[offset + 8:offset + 24]).split(b"\0", 1)[0]
            if segname == b"__PAGEZERO":
                vmaddr, old_size, fileoff, filesize = struct.unpack_from(
                    "<QQQQ", data, offset + 24
                )
                if found:
                    raise ValueError("multiple __PAGEZERO segments found")
                if vmaddr != 0 or fileoff != 0 or filesize != 0:
                    raise ValueError("unexpected __PAGEZERO layout")
                if old_size < 0x100000000:
                    raise ValueError(
                        f"__PAGEZERO is already smaller than 4 GiB: 0x{old_size:x}"
                    )
                if new_size >= old_size:
                    raise ValueError(
                        f"new __PAGEZERO size 0x{new_size:x} is not smaller than "
                        f"existing size 0x{old_size:x}"
                    )
                struct.pack_into("<Q", data, offset + 32, new_size)
                found = True
        offset += cmdsize

    if not found:
        raise ValueError("__PAGEZERO LC_SEGMENT_64 not found")
    if offset != commands_end:
        raise ValueError("Mach-O load-command size mismatch")

    path.write_bytes(data)
    return old_size, new_size


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument(
        "--size",
        type=parse_size,
        default=VM_PAGE_SIZE,
        help="new __PAGEZERO size (default: 0x4000, one iOS arm64 page)",
    )
    args = parser.parse_args()

    try:
        old_size, new_size = patch_pagezero(args.executable, args.size)
    except (OSError, ValueError, struct.error) as exc:
        print(f"pagezero patch failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"patched {args.executable}: __PAGEZERO "
        f"0x{old_size:x} -> 0x{new_size:x}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
