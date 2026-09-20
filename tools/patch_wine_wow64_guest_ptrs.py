#!/usr/bin/env python3
from pathlib import Path
import sys

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("wine-src")
wow = root / "dlls/wow64"
header = wow / "wow64_private.h"
s = header.read_text(encoding="utf-8")

marker = """static inline void *get_ptr( UINT **args ) { return ULongToPtr( *(*args)++ ); }"""
if "wow64_guest_to_host" not in s:
    helper = """/*
 * Madeira/iOS maps the logical 32-bit address space into a 4-GiB-aligned
 * high host cage.  NtCurrentTeb32() is inside that cage, so its high 32 bits
 * are the process-wide host bias.  Keep the fallback for ordinary Windows.
 */
static inline ULONG_PTR wow64_guest_bias(void)
{
    ULONG_PTR teb = (ULONG_PTR)NtCurrentTeb32();
    return teb & ~(ULONG_PTR)0xffffffffu;
}

static inline void *wow64_guest_to_host( ULONG value )
{
    ULONG_PTR bias;
    if (!value) return NULL;
    bias = wow64_guest_bias();
    return (void *)(bias ? bias + (ULONG_PTR)value : (ULONG_PTR)value);
}

"""
    if marker not in s:
        raise SystemExit("wow64_private.h get_ptr anchor not found")
    s = s.replace(marker, helper + marker, 1)
header.write_text(s, encoding="utf-8")

# Every ULongToPtr in dlls/wow64 converts a 32-bit guest pointer supplied by
# the emulated process. Route all of them through the high-cage translator.
files = sorted(wow.glob("*.c")) + [header]
changed = 0
for p in files:
    data = p.read_text(encoding="utf-8")
    n = data.count("ULongToPtr(")
    if n:
        data = data.replace("ULongToPtr(", "wow64_guest_to_host(")
        p.write_text(data, encoding="utf-8")
        changed += n

# The helper itself must not recurse, and central conversion helpers must use it.
data = header.read_text(encoding="utf-8")
if "return wow64_guest_to_host( *(*args)++ );" not in data:
    raise SystemExit("wow64 get_ptr was not translated")
if "wow64_guest_to_host( value )" in data:
    raise SystemExit("wow64 guest helper became recursive")
if changed < 20:
    raise SystemExit(f"unexpectedly few WoW64 pointer conversions patched: {changed}")

print(f"Installed Wine WoW64 guest pointer translation at {changed} call sites")
