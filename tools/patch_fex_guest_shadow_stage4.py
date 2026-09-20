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

# 4) Make the WoW64 BT module consume the high 4GiB shadow arena published by
# Madeira's app startup probes. This is the production path for PE32/WoW64, not
# just the standalone native FEX gate.
p = root / "Source/Windows/WOW64/Module.cpp"
s = p.read_text(encoding="utf-8")
if "ReadHexEnvironmentU64" not in s:
    inc = "#include <cstdint>\n"
    if inc not in s:
        raise SystemExit("WOW64 include anchor missing")
    s = s.replace(inc, inc + "#include <cstdlib>\n", 1)

    helper_anchor = "decltype(__wine_unix_call_dispatcher) WineUnixCall;\n"
    helper = r'''
uint64_t ReadHexEnvironmentU64(const char* Name) {
  char Buffer[64] {};
  const DWORD Count = GetEnvironmentVariableA(Name, Buffer, sizeof(Buffer));
  if (Count == 0 || Count >= sizeof(Buffer)) {
    return 0;
  }
  char* End {};
  return static_cast<uint64_t>(strtoull(Buffer, &End, 16));
}

void ConfigureGuestShadowMemory() {
  const uint64_t Bias = ReadHexEnvironmentU64("WINE_IOS_FEX_GUEST_BIAS");
  const uint64_t Limit = ReadHexEnvironmentU64("WINE_IOS_FEX_GUEST_LIMIT");
  const uint64_t RedirectGuest = ReadHexEnvironmentU64("WINE_IOS_FEX_REDIRECT_GUEST");
  const uint64_t RedirectHost = ReadHexEnvironmentU64("WINE_IOS_FEX_REDIRECT_HOST");

  if (!Bias || !Limit) {
    LogMan::Msg::IFmt("[guest-shadow] disabled: WINE_IOS_FEX_GUEST_BIAS/LIMIT not present");
    return;
  }
  if ((Bias & 0xfff) || (Limit & 0xfff)) {
    LogMan::Msg::EFmt("[guest-shadow] refused unaligned bias={:#x} limit={:#x}", Bias, Limit);
    return;
  }
  if (Limit != (1ULL << 32)) {
    LogMan::Msg::EFmt("[guest-shadow] refused non-4GiB linear window limit={:#x}; sparse fallback not wired yet", Limit);
    return;
  }

  CTX->SetGuestMemoryAddressBias(Bias, Limit, RedirectGuest, RedirectHost);
  uint64_t ProbeHost {};
  const bool ProbeOK = CTX->TranslateGuestMemoryAddress(0x00400000ULL, &ProbeHost);
  LogMan::Msg::IFmt(
    "[guest-shadow] enabled bias={:#x} limit={:#x} redirect_guest={:#x} redirect_host={:#x} probe_guest=0x00400000 -> host={:#x} ok={}",
    Bias, Limit, RedirectGuest, RedirectHost, ProbeHost, ProbeOK ? "yes" : "no");
}

uint64_t TranslateHostFaultToGuest(uint64_t HostAddress) {
  if (!CTX) return HostAddress;
  uint64_t GuestAddress {};
  if (CTX->TranslateHostMemoryAddress(HostAddress, &GuestAddress)) {
    LogMan::Msg::DFmt("[guest-shadow] translated host fault {:#x} -> guest {:#x}", HostAddress, GuestAddress);
    return GuestAddress;
  }
  return HostAddress;
}

'''
    if helper_anchor not in s:
        raise SystemExit("WOW64 helper anchor missing")
    s = s.replace(helper_anchor, helper_anchor + helper, 1)

create_ctx = """  {
    auto HostFeatures = FEX::Windows::CPUFeatures::FetchHostFeatures(IsWine, FEXCore::HostFeatures::HostTypeEnum::Wow64);
    CTX = FEXCore::Context::Context::CreateNewContext(HostFeatures);
  }

  CTX->SetSignalDelegator(SignalDelegator.get());"""
with_config = """  {
    auto HostFeatures = FEX::Windows::CPUFeatures::FetchHostFeatures(IsWine, FEXCore::HostFeatures::HostTypeEnum::Wow64);
    CTX = FEXCore::Context::Context::CreateNewContext(HostFeatures);
  }

  ConfigureGuestShadowMemory();
  CTX->SetSignalDelegator(SignalDelegator.get());"""
if "ConfigureGuestShadowMemory();" not in s:
    if create_ctx not in s:
        raise SystemExit("WOW64 context creation anchor missing")
    s = s.replace(create_ctx, with_config, 1)

fault_old = """  if (Exception->ExceptionCode == EXCEPTION_ACCESS_VIOLATION) {
    const auto FaultAddress = static_cast<uint64_t>(Exception->ExceptionInformation[1]);

    if (FEX::Windows::CallRetStack::HandleAccessViolation(Thread, FaultAddress, Context->X25)) {"""
fault_new = """  if (Exception->ExceptionCode == EXCEPTION_ACCESS_VIOLATION) {
    const auto FaultAddress = static_cast<uint64_t>(Exception->ExceptionInformation[1]);
    const uint64_t GuestFaultAddress = TranslateHostFaultToGuest(FaultAddress);
    if (GuestFaultAddress != FaultAddress && Exception->NumberParameters > 1) {
      Exception->ExceptionInformation[1] = GuestFaultAddress;
      LogMan::Msg::DFmt(
        "[guest-shadow] access violation fault host {:#x} published as guest {:#x}",
        FaultAddress,
        GuestFaultAddress);
    }

    if (FEX::Windows::CallRetStack::HandleAccessViolation(Thread, FaultAddress, Context->X25)) {"""
if "GuestFaultAddress = TranslateHostFaultToGuest" not in s:
    if fault_old not in s:
        raise SystemExit("WOW64 fault translation anchor missing")
    s = s.replace(fault_old, fault_new, 1)

p.write_text(s, encoding="utf-8")

print("Installed FEX guest-shadow stage4/5: fetch + SMC + atomic-add + WoW64 env/fault wiring")
