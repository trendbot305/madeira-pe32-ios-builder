#!/usr/bin/env python3
"""Apply Madeira Build 79's reviewed FEX VirtualProtect contract fix."""

from __future__ import annotations

import argparse
from pathlib import Path

OLD = "  return ::VirtualProtect(Ptr, Size, prot, nullptr) == 0;\n"
NEW = (
    "  DWORD OldProtect {};\n"
    "  return ::VirtualProtect(Ptr, Size, prot, &OldProtect) != 0;\n"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("fex_root", type=Path)
    args = parser.parse_args()

    target = (
        args.fex_root
        / "FEXCore"
        / "include"
        / "FEXCore"
        / "Utils"
        / "AllocatorHooks.h"
    )
    text = target.read_text(encoding="utf-8")

    if NEW in text and OLD not in text:
        print(f"Build 79 VirtualProtect fix already present: {target}")
        return 0

    count = text.count(OLD)
    if count != 1:
        raise SystemExit(
            f"Expected exactly one VirtualProtect anchor in {target}, found {count}"
        )

    target.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
    print(f"Applied Build 79 VirtualProtect fix: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
