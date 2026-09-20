#!/usr/bin/env python3
from pathlib import Path
import sys

root = Path(sys.argv[1])

# 1) Translate instruction fetch while keeping guest RIP accounting logical.
p = root / "FEXCore/Source/Interface/Core/Frontend.cpp"
s = p.read_text(encoding="utf-8")
old = """  return DecodeStream {
    .InstStream = _InstStream - EntryPoint + RIP,
    .AdjustedInstStream = _InstStream - EntryPoint + RIP,
  };
}"""
new = """  // Darwin/iOS 32-bit guest shadow: keep the architectural RIP logical,
  // but fetch instruction bytes from its translated host backing.
  uint64_t HostRIP {};
  if (CTX->TranslateGuestMemoryAddress(RIP, &HostRIP)) {
    return DecodeStream {
      .InstStream = reinterpret_cast<const uint8_t*>(RIP),
      .AdjustedInstStream = reinterpret_cast<const uint8_t*>(HostRIP),
    };
  }

  return DecodeStream {
    .InstStream = _InstStream - EntryPoint + RIP,
    .AdjustedInstStream = _InstStream - EntryPoint + RIP,
  };
}"""
if "TranslateGuestMemoryAddress(RIP, &HostRIP)" not in s:
    if old not in s:
        raise SystemExit("Frontend instruction-fetch anchor missing")
    s = s.replace(old, new, 1)
p.write_text(s, encoding="utf-8")

# 2) SMC compile-time copy must read translated bytes too.
p = root / "FEXCore/Source/Interface/Core/Core.cpp"
s = p.read_text(encoding="utf-8")
old = "          auto ExistingCodePtr = reinterpret_cast<uint8_t*>(Block.Entry + BlockInstructionsLength);"
new = """          const uint64_t ExistingGuestAddress = Block.Entry + BlockInstructionsLength;
          uint64_t ExistingHostAddress = ExistingGuestAddress;
          (void)TranslateGuestMemoryAddress(ExistingGuestAddress, &ExistingHostAddress);
          auto ExistingCodePtr = reinterpret_cast<uint8_t*>(ExistingHostAddress);"""
if "ExistingGuestAddress = Block.Entry + BlockInstructionsLength" not in s:
    if old not in s:
        raise SystemExit("Core SMC translation anchor missing")
    s = s.replace(old, new, 1)
p.write_text(s, encoding="utf-8")

# 3) The device gate uses LOCK INC, lowered to AtomicFetchAdd. On the iPhone
# host SupportsAtomics is true (LSE), so translate the address immediately
# before LDADDAL. Refuse translated atomics on a non-LSE host rather than
# silently using a scratch-register-conflicting fallback.
p = root / "FEXCore/Source/Interface/Core/JIT/AtomicOps.cpp"
s = p.read_text(encoding="utf-8")
old = """  auto MemSrc = GetReg(Op->Addr);
  auto Src = GetReg(Op->Value);

  if (CTX->HostFeatures.SupportsAtomics) {
    ldaddal(SubEmitSize, Src, GetReg(Node), MemSrc);
"""
new = """  auto MemSrc = GetReg(Op->Addr);
  auto Src = GetReg(Op->Value);

  if (IsGuestMemoryAddressTranslationEnabled()) {
    LOGMAN_THROW_A_FMT(CTX->HostFeatures.SupportsAtomics,
                       "Guest-shadow atomic add requires host LSE atomics");
    MemSrc = ApplyGuestMemoryAddressBias(MemSrc);
  }

  if (CTX->HostFeatures.SupportsAtomics) {
    ldaddal(SubEmitSize, Src, GetReg(Node), MemSrc);
"""
if "Guest-shadow atomic add requires host LSE atomics" not in s:
    if old not in s:
        raise SystemExit("AtomicFetchAdd translation anchor missing")
    s = s.replace(old, new, 1)
p.write_text(s, encoding="utf-8")

print("Installed FEX guest-shadow stage4: fetch + SMC + atomic-add")
