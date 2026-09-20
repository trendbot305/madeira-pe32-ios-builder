#!/usr/bin/env python3
from pathlib import Path
import sys

root = Path(sys.argv[1] if len(sys.argv) > 1 else "external/Madeira/FEX")

def replace_once(path: Path, old: str, new: str, label: str):
    text = path.read_text(encoding="utf-8")
    if new in text:
        return
    if old not in text:
        raise SystemExit(f"{label}: anchor not found in {path}")
    text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")

# 1) Instruction fetch: guest RIP remains logical, byte reads come from translated host backing.
core = root / "FEXCore/Source/Interface/Core/Core.cpp"
replace_once(
    core,
    """bool ContextImpl::CheckIfBlockIsCacheable(FEXCore::Core::InternalThreadState& Thread, uint64_t GuestRIP, uint64_t MaxInst) {
  return Thread.FrontendDecoder->CheckIfCacheable(Thread, reinterpret_cast<const uint8_t*>(GuestRIP), GuestRIP, MaxInst);
}""",
    """bool ContextImpl::CheckIfBlockIsCacheable(FEXCore::Core::InternalThreadState& Thread, uint64_t GuestRIP, uint64_t MaxInst) {
  uint64_t HostRIP = GuestRIP;
  (void)TranslateGuestMemoryAddress(GuestRIP, &HostRIP);
  return Thread.FrontendDecoder->CheckIfCacheable(Thread, reinterpret_cast<const uint8_t*>(HostRIP), GuestRIP, MaxInst);
}""",
    "cacheable translated instruction fetch",
)
replace_once(
    core,
    """    const uint8_t* GuestCode {};
    GuestCode = reinterpret_cast<const uint8_t*>(GuestRIP);

    /* perf-silenced GenerateIR GuestCode log */""",
    """    const uint8_t* GuestCode {};
    uint64_t HostRIP = GuestRIP;
    (void)TranslateGuestMemoryAddress(GuestRIP, &HostRIP);
    GuestCode = reinterpret_cast<const uint8_t*>(HostRIP);

    /* perf-silenced GenerateIR GuestCode log */""",
    "GenerateIR translated instruction fetch",
)

frontend = root / "FEXCore/Source/Interface/Core/Frontend.cpp"
replace_once(
    frontend,
    """  return DecodeStream {
    .InstStream = _InstStream - EntryPoint + RIP,
    .AdjustedInstStream = _InstStream - EntryPoint + RIP,
  };""",
    """  return DecodeStream {
    // InstStream is the guest-visible logical PC used by executable-range
    // checks and relocation bookkeeping. AdjustedInstStream is the actual
    // host pointer used for instruction-byte reads.
    .InstStream = reinterpret_cast<const uint8_t*>(RIP),
    .AdjustedInstStream = _InstStream - EntryPoint + RIP,
  };""",
    "logical/frontend decode stream split",
)

# 2) WoW64 frontend: configure the 4-GiB-aligned high backing cage and translate
# guest pointers at every host dereference boundary.
mod = root / "Source/Windows/WOW64/Module.cpp"
text = mod.read_text(encoding="utf-8")
if "GuestMemoryBias" not in text:
    anchor = """uint64_t GetWowTEB(void* TEB) {
  static constexpr size_t WowTEBOffsetMemberOffset {0x180c};
  return static_cast<uint64_t>(
    *reinterpret_cast<LONG*>(reinterpret_cast<uintptr_t>(TEB) + WowTEBOffsetMemberOffset) + reinterpret_cast<uint64_t>(TEB));
}
"""
    insert = anchor + """
uint64_t GuestMemoryBias {};

uint64_t GuestToHostAddress(uint64_t Address) {
  uint64_t Host = Address;
  if (CTX && CTX->TranslateGuestMemoryAddress(Address, &Host)) {
    return Host;
  }
  return Address;
}

uint64_t HostToGuestAddress(uint64_t Address) {
  uint64_t Guest = Address;
  if (CTX && CTX->TranslateHostMemoryAddress(Address, &Guest)) {
    return Guest;
  }
  // Include the one-past-the-end boundary for section/range metadata.
  if (GuestMemoryBias && Address >= GuestMemoryBias && Address <= GuestMemoryBias + (1ULL << 32)) {
    return Address - GuestMemoryBias;
  }
  return Address;
}

template<typename T>
T* GuestToHostPointer(uint64_t Address) {
  return reinterpret_cast<T*>(GuestToHostAddress(Address));
}
"""
    if anchor not in text:
        raise SystemExit("WoW64 guest translation helper anchor not found")
    text = text.replace(anchor, insert, 1)

old = """  // The TEB is the only populated GDT entry by default
  auto GDT = State.GetSegmentFromIndex(State, (Context->SegFs & 0xffff));
  State.SetGDTBase(GDT, WowTEB);
  State.SetGDTLimit(GDT, 0xF'FFFFU);
  State.fs_cached = WowTEB;"""
new = """  // The host TEB lives inside the high iOS backing cage. x86 FS must see
  // only the logical 32-bit guest address.
  const uint64_t GuestWowTEB = HostToGuestAddress(WowTEB);
  auto GDT = State.GetSegmentFromIndex(State, (Context->SegFs & 0xffff));
  State.SetGDTBase(GDT, GuestWowTEB);
  State.SetGDTLimit(GDT, 0xF'FFFFU);
  State.fs_cached = GuestWowTEB;"""
if new not in text:
    if old not in text: raise SystemExit("WoW64 FS/TEB anchor not found")
    text = text.replace(old, new, 1)

old = """    const uint64_t ReturnRIP = *(uint32_t*)(Frame->State.gregs[FEXCore::X86State::REG_RSP]); // Return address from the stack
    uint64_t ReturnRSP = Frame->State.gregs[FEXCore::X86State::REG_RSP] + 4;                 // Stack pointer after popping return address"""
new = """    const uint64_t GuestRSP = Frame->State.gregs[FEXCore::X86State::REG_RSP];
    const uint64_t ReturnRIP = *GuestToHostPointer<uint32_t>(GuestRSP); // Return address from translated guest stack
    uint64_t ReturnRSP = GuestRSP + 4;                                  // Logical guest SP after popping return address"""
if new not in text:
    if old not in text: raise SystemExit("WoW64 syscall stack anchor not found")
    text = text.replace(old, new, 1)

text = text.replace(
"""      }* StackArgs = reinterpret_cast<StackLayout*>(ReturnRSP);""",
"""      }* StackArgs = GuestToHostPointer<StackLayout>(ReturnRSP);""",
1)
text = text.replace(
"""      ReturnRAX = static_cast<uint64_t>(WineUnixCall(StackArgs->Handle, StackArgs->ID, ULongToPtr(StackArgs->Args)));""",
"""      ReturnRAX = static_cast<uint64_t>(WineUnixCall(StackArgs->Handle, StackArgs->ID, GuestToHostPointer<void>(StackArgs->Args)));""",
1)
text = text.replace(
"""      ReturnRAX = static_cast<uint64_t>(Wow64SystemServiceEx(static_cast<UINT>(EntryRAX), reinterpret_cast<UINT*>(ReturnRSP + 4)));""",
"""      ReturnRAX = static_cast<uint64_t>(Wow64SystemServiceEx(static_cast<UINT>(EntryRAX), GuestToHostPointer<UINT>(ReturnRSP + 4)));""",
1)

old = """  std::optional<FEXCore::ExecutableFileSectionInfo> LookupExecutableFileSection(FEXCore::Core::InternalThreadState*, uint64_t Address) override {
    return ImageTracker->LookupExecutableFileSection(Address);
  }

  void MarkGuestExecutableRange(FEXCore::Core::InternalThreadState* Thread, uint64_t Start, uint64_t Length) override {
    InvalidationTracker->ReprotectRWXIntervals(Start, Length);
  }

  void InvalidateGuestCodeRange(FEXCore::Core::InternalThreadState* Thread, uint64_t Start, uint64_t Length) override {
    InvalidationTracker->InvalidateAlignedInterval(Start, Length, false);
  }

  void MarkOvercommitRange(uint64_t Start, uint64_t Length) override {
    OvercommitTracker->MarkRange(Start, Length);
  }

  void UnmarkOvercommitRange(uint64_t Start, uint64_t Length) override {
    OvercommitTracker->UnmarkRange(Start, Length);
  }

  FEXCore::HLE::ExecutableRangeInfo QueryGuestExecutableRange(FEXCore::Core::InternalThreadState* Thread, uint64_t Address) override {
    return InvalidationTracker->QueryExecutableRange(Address);
  }"""
new = """  std::optional<FEXCore::ExecutableFileSectionInfo> LookupExecutableFileSection(FEXCore::Core::InternalThreadState*, uint64_t Address) override {
    const uint64_t HostAddress = GuestToHostAddress(Address);
    auto Region = ImageTracker->LookupExecutableFileSection(HostAddress);
    if (!Region) {
      return std::nullopt;
    }
    return FEXCore::ExecutableFileSectionInfo {
      Region->FileInfo,
      HostToGuestAddress(Region->FileStartVA),
      HostToGuestAddress(Region->BeginVA),
      HostToGuestAddress(Region->EndVA),
    };
  }

  void MarkGuestExecutableRange(FEXCore::Core::InternalThreadState* Thread, uint64_t Start, uint64_t Length) override {
    InvalidationTracker->ReprotectRWXIntervals(GuestToHostAddress(Start), Length);
  }

  void InvalidateGuestCodeRange(FEXCore::Core::InternalThreadState* Thread, uint64_t Start, uint64_t Length) override {
    InvalidationTracker->InvalidateAlignedInterval(GuestToHostAddress(Start), Length, false);
  }

  void MarkOvercommitRange(uint64_t Start, uint64_t Length) override {
    OvercommitTracker->MarkRange(GuestToHostAddress(Start), Length);
  }

  void UnmarkOvercommitRange(uint64_t Start, uint64_t Length) override {
    OvercommitTracker->UnmarkRange(GuestToHostAddress(Start), Length);
  }

  FEXCore::HLE::ExecutableRangeInfo QueryGuestExecutableRange(FEXCore::Core::InternalThreadState* Thread, uint64_t Address) override {
    auto Range = InvalidationTracker->QueryExecutableRange(GuestToHostAddress(Address));
    Range.Base = HostToGuestAddress(Range.Base);
    return Range;
  }"""
if new not in text:
    if old not in text: raise SystemExit("WoW64 executable-range boundary anchor not found")
    text = text.replace(old, new, 1)

old = """  CTX->SetSignalDelegator(SignalDelegator.get());
  CTX->SetSyscallHandler(SyscallHandler.get());
  CTX->InitCore();"""
new = """  // Madeira maps the 32-bit Windows address space into a 4-GiB-aligned
  // high host cage. Low 32 bits are therefore the exact x86 guest pointer.
  const uint64_t WowTEBHost = GetWowTEB(NtCurrentTeb());
  GuestMemoryBias = WowTEBHost & ~0xFFFF'FFFFULL;
  if (!GuestMemoryBias) {
    LogMan::Msg::EFmt("[Madeira-GuestVA] WoW64 TEB is not in a high 4-GiB cage: {:#x}", WowTEBHost);
  } else {
    CTX->SetGuestMemoryAddressBias(GuestMemoryBias, 1ULL << 32);
    LogMan::Msg::IFmt("[Madeira-GuestVA] bias={:#x} wowteb_host={:#x} wowteb_guest={:#x}",
                      GuestMemoryBias, WowTEBHost, WowTEBHost - GuestMemoryBias);
  }

  CTX->SetSignalDelegator(SignalDelegator.get());
  CTX->SetSyscallHandler(SyscallHandler.get());
  CTX->InitCore();"""
if new not in text:
    if old not in text: raise SystemExit("WoW64 context init anchor not found")
    text = text.replace(old, new, 1)

old = """  NtAllocateVirtualMemory(NtCurrentProcess(), &Addr, (1U << 31) - 1, &Size, MEM_RESERVE | MEM_COMMIT, PAGE_EXECUTE_READWRITE);
  InvalidationTracker->HandleMemoryProtectionNotification(reinterpret_cast<uint64_t>(Addr), Size, PAGE_EXECUTE);
  *reinterpret_cast<uint32_t*>(Addr) = 0x2ecd2ecd;
  BridgeInstrs::Syscall = Addr;
  BridgeInstrs::UnixCall = reinterpret_cast<void*>(reinterpret_cast<uintptr_t>(Addr) + 2);"""
new = """  const NTSTATUS BridgeStatus =
    NtAllocateVirtualMemory(NtCurrentProcess(), &Addr, (1U << 31) - 1, &Size, MEM_RESERVE | MEM_COMMIT, PAGE_EXECUTE_READWRITE);
  if (BridgeStatus || !Addr) {
    LogMan::Msg::EFmt("[Madeira-GuestVA] bridge allocation failed: {:#x}", static_cast<uint32_t>(BridgeStatus));
    return;
  }
  InvalidationTracker->HandleMemoryProtectionNotification(reinterpret_cast<uint64_t>(Addr), Size, PAGE_EXECUTE);
  *reinterpret_cast<uint32_t*>(Addr) = 0x2ecd2ecd;
  const uint64_t GuestBridge = HostToGuestAddress(reinterpret_cast<uint64_t>(Addr));
  BridgeInstrs::Syscall = reinterpret_cast<void*>(GuestBridge);
  BridgeInstrs::UnixCall = reinterpret_cast<void*>(GuestBridge + 2);
  LogMan::Msg::IFmt("[Madeira-GuestVA] bridge host={:#x} guest={:#x}", reinterpret_cast<uint64_t>(Addr), GuestBridge);"""
if new not in text:
    if old not in text: raise SystemExit("WoW64 bridge allocation anchor not found")
    text = text.replace(old, new, 1)

mod.write_text(text, encoding="utf-8")

# Fail closed if any critical contract piece was missed.
checks = {
    core: ["TranslateGuestMemoryAddress(GuestRIP", "HostRIP"],
    frontend: ["AdjustedInstStream = _InstStream - EntryPoint + RIP", "reinterpret_cast<const uint8_t*>(RIP)"],
    mod: ["GuestMemoryBias", "SetGuestMemoryAddressBias", "GuestToHostPointer<uint32_t>", "GuestBridge", "HostToGuestAddress(Region->BeginVA)"],
}
for path, needles in checks.items():
    data = path.read_text(encoding="utf-8")
    for needle in needles:
        if needle not in data:
            raise SystemExit(f"validation failed: {needle!r} missing from {path}")

print("Installed FEX WoW64 guest/host address translation boundaries")
