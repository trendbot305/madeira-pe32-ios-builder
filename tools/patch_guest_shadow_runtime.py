#!/usr/bin/env python3
from pathlib import Path
import sys

root = Path(sys.argv[1])
bridge = root / "app/Madeira/FEXBridge.mm"
header = root / "app/Madeira/FEXBridge.h"
content = root / "app/Madeira/ContentView.swift"

# --- Header API ---
h = header.read_text(encoding="utf-8")
decl = """// Run the 32-bit guest-shadow translation gate. Returns 42 on success.
// This validates translated instruction fetch, stack push/pop, ordinary store,
// an LSE-backed LOCK INC, and host->guest reverse translation.
int64_t fex_test_guest_shadow32(void);

"""
anchor = "// Log callback type (same as JIT allocator)"
if "fex_test_guest_shadow32" not in h:
    if anchor not in h:
        raise SystemExit("FEXBridge.h insertion anchor missing")
    h = h.replace(anchor, decl + anchor, 1)
header.write_text(h, encoding="utf-8")

# --- Native FEX guest-shadow gate ---
s = bridge.read_text(encoding="utf-8")

# Improve the existing fault handler so a translated high host fault names the
# logical 32-bit guest address before normal fatal handling.
old = """static FEXCore::Core::InternalThreadState *g_current_thread = nullptr;

static void ios_sigsegv_handler(int sig, siginfo_t *info, void *ucontext) {
    if (!g_current_thread) return;

    auto *Thread = g_current_thread;
"""
new = """static FEXCore::Core::InternalThreadState *g_current_thread = nullptr;
static FEXCore::Context::Context *g_guest_shadow_fault_context = nullptr;

static void ios_sigsegv_handler(int sig, siginfo_t *info, void *ucontext) {
    if (!g_current_thread) return;

    auto *Thread = g_current_thread;
"""
if "g_guest_shadow_fault_context" not in s:
    if old not in s:
        raise SystemExit("FEXBridge fault-handler anchor missing")
    s = s.replace(old, new, 1)

fault_anchor = """    // Not our fault — re-raise with default handler
    fex_log("SIGSEGV at %p (not InterruptFaultPage %p)", fault_addr, page_addr);
"""
fault_repl = """    // If the host fault landed inside a translated guest-shadow range, report
    // the architectural x86 address as well. This is the reverse-translation
    // half of the guest-base design and makes device faults actionable.
    if (g_guest_shadow_fault_context) {
        uint64_t guest_fault = 0;
        if (g_guest_shadow_fault_context->TranslateHostMemoryAddress(
                reinterpret_cast<uint64_t>(fault_addr), &guest_fault)) {
            fex_log("GUEST_SHADOW_FAULT host=%p guest=0x%llx",
                    fault_addr, (unsigned long long)guest_fault);
        }
    }

    // Not our fault — re-raise with default handler
    fex_log("SIGSEGV at %p (not InterruptFaultPage %p)", fault_addr, page_addr);
"""
if "GUEST_SHADOW_FAULT" not in s:
    if fault_anchor not in s:
        raise SystemExit("FEXBridge reverse-fault anchor missing")
    s = s.replace(fault_anchor, fault_repl, 1)

func_anchor = "int64_t fex_test_execute(void) {"
if "int64_t fex_test_guest_shadow32(void)" not in s:
    if func_anchor not in s:
        raise SystemExit("FEXBridge test insertion anchor missing")

    func = r'''
int64_t fex_test_guest_shadow32(void) {
    static std::atomic<bool> running {false};
    if (running.exchange(true)) {
        fex_log("PE32 shadow gate already running");
        return -90;
    }

    auto finish = [&](int64_t result) {
        g_guest_shadow_fault_context = nullptr;
        g_current_thread = nullptr;
        // Leave the next normal FEX initialization in its original 64-bit mode.
        FEXCore::Config::Set(FEXCore::Config::ConfigOption::CONFIG_IS64BIT_MODE, "1");
        running.store(false);
        return result;
    };

    fex_log("=== PE32 GUEST-SHADOW GATE v49 ===");

    // The global demo context is 64-bit. Do not mix operating modes in one
    // context; tear it down before creating the isolated 32-bit gate.
    if (g_initialized.load()) {
        fex_log("Guest-shadow gate: shutting down existing 64-bit FEX context");
        fex_shutdown();
    }

    if (!jit_pool_init()) {
        fex_log("SHADOW_FAIL: JIT pool unavailable");
        return finish(-1);
    }

    FEXCore::Allocator::mmap = fex_mmap_hook;
    FEXCore::Allocator::munmap = fex_munmap_hook;
    LogMan::Msg::InstallHandler(FEXLogHandler);
    LogMan::Throw::InstallHandler(FEXThrowHandler);

    try {
        FEXCore::Config::Initialize();
        FEXCore::Config::Set(FEXCore::Config::ConfigOption::CONFIG_IS64BIT_MODE, "0");
    } catch (const std::exception& e) {
        fex_log("SHADOW_FAIL: Config init: %s", e.what());
        return finish(-2);
    }

    FEXCore::HostFeatures Features {};
    Features.DCacheLineSize = 64;
    Features.ICacheLineSize = 64;
    Features.SupportsCacheMaintenanceOps = true;
    Features.SupportsAES = true;
    Features.SupportsCRC = true;
    Features.SupportsAtomics = true;
    Features.SupportsRCPC = true;
    Features.SupportsTSOImm9 = true;
    Features.SupportsSHA = true;
    Features.SupportsPMULL_128Bit = true;
    Features.SupportsFCMA = true;
    Features.SupportsFlagM = true;
    Features.SupportsFlagM2 = true;
    Features.SupportsAVX = false;
    Features.SupportsSVE128 = false;
    Features.SupportsSVE256 = false;
    Features.CPUMIDRs.resize(8, 0x611F0250);

    auto Ctx = FEXCore::Context::Context::CreateNewContext(Features);
    if (!Ctx) {
        fex_log("SHADOW_FAIL: CreateNewContext returned null");
        return finish(-3);
    }

    constexpr uint64_t GuestLimit = 1ULL << 32;
    constexpr size_t HostPage = 0x4000;
    constexpr uint64_t CodeGuest = 0x00400000ULL;
    constexpr uint64_t DataGuest = 0x00500000ULL;
    constexpr uint64_t StackGuestPage = 0x0060C000ULL;
    constexpr uint64_t StackGuestTop = 0x0060FFF0ULL;

    void *LinearShadow = ::mmap(nullptr, (size_t)GuestLimit, PROT_NONE,
                                MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    const bool Linear = LinearShadow != MAP_FAILED;
    FEXCore::Context::GuestMemoryAddressRegion Regions[3] {};
    void *Sparse[3] {MAP_FAILED, MAP_FAILED, MAP_FAILED};

    auto cleanup_backing = [&]() {
        if (Linear) {
            ::munmap(LinearShadow, (size_t)GuestLimit);
        } else {
            for (void *P : Sparse) if (P != MAP_FAILED) ::munmap(P, HostPage);
        }
    };

    auto host_for = [&](uint64_t Guest) -> uint8_t * {
        uint64_t Host = 0;
        if (!Ctx->TranslateGuestMemoryAddress(Guest, &Host)) return nullptr;
        return reinterpret_cast<uint8_t*>(Host);
    };

    if (Linear) {
        fex_log("SHADOW_LINEAR_4G_OK base=%p size=0x%llx",
                LinearShadow, (unsigned long long)GuestLimit);
        for (uint64_t GuestPage : {CodeGuest, DataGuest, StackGuestPage}) {
            void *Host = static_cast<uint8_t*>(LinearShadow) + GuestPage;
            if (::mprotect(Host, HostPage, PROT_READ | PROT_WRITE) != 0) {
                fex_log("SHADOW_FAIL: mprotect guest=0x%llx host=%p errno=%d",
                        (unsigned long long)GuestPage, Host, errno);
                cleanup_backing();
                return finish(-4);
            }
        }
        Ctx->SetGuestMemoryAddressBias(
            reinterpret_cast<uint64_t>(LinearShadow), GuestLimit);
    } else {
        fex_log("SHADOW_LINEAR_4G_FAIL errno=%d -- using sparse guest regions", errno);
        const uint64_t GuestPages[3] = {CodeGuest, DataGuest, StackGuestPage};
        for (size_t I = 0; I < 3; ++I) {
            Sparse[I] = ::mmap(nullptr, HostPage, PROT_READ | PROT_WRITE,
                               MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
            if (Sparse[I] == MAP_FAILED) {
                fex_log("SHADOW_FAIL: sparse page %zu allocation errno=%d", I, errno);
                cleanup_backing();
                return finish(-5);
            }
            Regions[I].GuestBase = GuestPages[I];
            Regions[I].HostBase = reinterpret_cast<uint64_t>(Sparse[I]);
            Regions[I].Size = HostPage;
        }
        if (!Ctx->SetGuestMemoryAddressRegions(Regions, 3)) {
            fex_log("SHADOW_FAIL: FEX rejected sparse guest regions");
            cleanup_backing();
            return finish(-6);
        }
        fex_log("SHADOW_SPARSE_OK code=%p data=%p stack=%p",
                Sparse[0], Sparse[1], Sparse[2]);
    }

    uint8_t *CodeHost = host_for(CodeGuest);
    uint8_t *DataHost = host_for(DataGuest);
    uint8_t *StackHost = host_for(StackGuestPage);
    if (!CodeHost || !DataHost || !StackHost) {
        fex_log("SHADOW_FAIL: forward translation missing");
        cleanup_backing();
        return finish(-7);
    }

    // i386 gate:
    //   mov esp,0060fff0h
    //   mov eax,11223344h
    //   push eax
    //   pop ebx
    //   mov [00500000h],ebx
    //   lock inc dword ptr [00500004h]
    //   hlt
    //
    // This deliberately exercises translated instruction fetch, stack memory,
    // ordinary data stores and an atomic RMW before terminating through HLT.
    static const uint8_t GuestCode[] = {
        0xBC, 0xF0, 0xFF, 0x60, 0x00,
        0xB8, 0x44, 0x33, 0x22, 0x11,
        0x50,
        0x5B,
        0x89, 0x1D, 0x00, 0x00, 0x50, 0x00,
        0xF0, 0xFF, 0x05, 0x04, 0x00, 0x50, 0x00,
        0xF4,
    };
    std::memcpy(CodeHost, GuestCode, sizeof(GuestCode));
    *reinterpret_cast<uint32_t*>(DataHost + 0) = 0;
    *reinterpret_cast<uint32_t*>(DataHost + 4) = 41;
    std::memset(StackHost, 0, HostPage);

    uint64_t ReverseCode = 0, ReverseData = 0, ReverseStack = 0;
    const bool ReverseOK =
        Ctx->TranslateHostMemoryAddress(reinterpret_cast<uint64_t>(CodeHost), &ReverseCode) &&
        Ctx->TranslateHostMemoryAddress(reinterpret_cast<uint64_t>(DataHost), &ReverseData) &&
        Ctx->TranslateHostMemoryAddress(reinterpret_cast<uint64_t>(StackHost), &ReverseStack) &&
        ReverseCode == CodeGuest && ReverseData == DataGuest &&
        ReverseStack == StackGuestPage;
    if (!ReverseOK) {
        fex_log("SHADOW_FAIL: reverse translation code=0x%llx data=0x%llx stack=0x%llx",
                (unsigned long long)ReverseCode,
                (unsigned long long)ReverseData,
                (unsigned long long)ReverseStack);
        cleanup_backing();
        return finish(-8);
    }
    fex_log("SHADOW_REVERSE_OK code=0x%llx data=0x%llx stack=0x%llx",
            (unsigned long long)ReverseCode,
            (unsigned long long)ReverseData,
            (unsigned long long)ReverseStack);

    iOSSyscallHandler Syscalls;
    iOSSignalDelegator Signals;
    Ctx->EnableExitOnHLT();
    Ctx->SetSignalDelegator(&Signals);
    Ctx->SetSyscallHandler(&Syscalls);
    Ctx->SetHardwareTSOSupport(true);

    try {
        if (!Ctx->InitCore()) {
            fex_log("SHADOW_FAIL: InitCore returned false");
            cleanup_backing();
            return finish(-9);
        }
    } catch (const std::exception& e) {
        fex_log("SHADOW_FAIL: InitCore exception: %s", e.what());
        cleanup_backing();
        return finish(-10);
    }

    auto *Thread = Ctx->CreateThread(CodeGuest, StackGuestTop);
    if (!Thread) {
        fex_log("SHADOW_FAIL: CreateThread returned null");
        cleanup_backing();
        return finish(-11);
    }

    // Explicit legacy 32-bit code segment. This is also a guard against an
    // accidental regression back to the x86-64 demo context.
    FEXCore::Core::CPUState::gdt_segment GDT[1] {};
    GDT[0].L = 0;
    GDT[0].D = 1;
    GDT[0].P = 1;
    GDT[0].S = 1;
    GDT[0].Type = 0b1011;
    Thread->CurrentFrame->State.segment_arrays[0] = GDT;
    Thread->CurrentFrame->State.cs_idx = 0;

    // FEX keeps this host-only; it is not a guest pointer and therefore must
    // not be placed in the translated 4GiB arena.
    constexpr size_t CallRetSize = FEXCore::Core::InternalThreadState::CALLRET_STACK_SIZE;
    constexpr size_t CallRetAlloc = CallRetSize + 2 * HostPage;
    void *CallRet = ::mmap(nullptr, CallRetAlloc, PROT_NONE,
                           MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (CallRet == MAP_FAILED ||
        ::mprotect(static_cast<uint8_t*>(CallRet) + HostPage,
                   CallRetSize, PROT_READ | PROT_WRITE) != 0) {
        fex_log("SHADOW_FAIL: call-ret stack allocation");
        if (CallRet != MAP_FAILED) ::munmap(CallRet, CallRetAlloc);
        Ctx->DestroyThread(Thread);
        cleanup_backing();
        return finish(-12);
    }
    Thread->CallRetStackBase = static_cast<uint8_t*>(CallRet) + HostPage;
    Thread->CurrentFrame->State.callret_sp =
        reinterpret_cast<uint64_t>(Thread->CallRetStackBase) + CallRetSize / 4;

    g_current_thread = Thread;
    g_guest_shadow_fault_context = Ctx.get();

    fex_log("SHADOW_EXEC_BEGIN rip=0x%llx esp=0x%llx code_host=%p data_host=%p",
            (unsigned long long)CodeGuest,
            (unsigned long long)StackGuestTop,
            CodeHost, DataHost);

    bool ExecuteOK = true;
    try {
        Ctx->ExecuteThread(Thread);
    } catch (const std::exception& e) {
        fex_log("SHADOW_FAIL: ExecuteThread exception: %s", e.what());
        ExecuteOK = false;
    } catch (...) {
        fex_log("SHADOW_FAIL: ExecuteThread unknown exception");
        ExecuteOK = false;
    }

    g_guest_shadow_fault_context = nullptr;
    g_current_thread = nullptr;

    const uint32_t StoreValue = *reinterpret_cast<uint32_t*>(DataHost + 0);
    const uint32_t AtomicValue = *reinterpret_cast<uint32_t*>(DataHost + 4);
    const uint32_t GuestESP =
        static_cast<uint32_t>(Thread->CurrentFrame->State.gregs[FEXCore::X86State::REG_RSP]);
    const uint32_t GuestEBX =
        static_cast<uint32_t>(Thread->CurrentFrame->State.gregs[FEXCore::X86State::REG_RBX]);

    fex_log("SHADOW_EXEC_END store=0x%08x atomic=%u ebx=0x%08x esp=0x%08x",
            StoreValue, AtomicValue, GuestEBX, GuestESP);

    Ctx->DestroyThread(Thread);
    ::munmap(CallRet, CallRetAlloc);
    cleanup_backing();

    if (!ExecuteOK) return finish(-13);
    if (StoreValue != 0x11223344U) {
        fex_log("SHADOW_FAIL: ordinary translated store mismatch");
        return finish(-14);
    }
    if (AtomicValue != 42U) {
        fex_log("SHADOW_FAIL: translated atomic mismatch");
        return finish(-15);
    }
    if (GuestEBX != 0x11223344U || GuestESP != (uint32_t)StackGuestTop) {
        fex_log("SHADOW_FAIL: translated stack push/pop mismatch");
        return finish(-16);
    }

    fex_log("=== PE32 GUEST-SHADOW PASS: fetch + stack + store + atomic + reverse xlate ===");
    return finish(42);
}

'''
    s = s.replace(func_anchor, func + func_anchor, 1)

bridge.write_text(s, encoding="utf-8")

# --- SwiftUI button and runner ---
v = content.read_text(encoding="utf-8")

runner_anchor = "    private func runFEXTest() {"
runner = r'''    private func runPE32ShadowTest() {
        logStore.log("Starting PE32 guest-shadow memory gate…")
        jitStatus = .testing

        fex_set_log_callback { msg in
            if let msg = msg {
                let str = String(cString: msg)
                DispatchQueue.main.async {
                    LogStore.shared.log(str, level: .debug)
                }
            }
        }

        DispatchQueue.global(qos: .userInitiated).async {
            let result = fex_test_guest_shadow32()
            DispatchQueue.main.async {
                if result == 42 {
                    jitStatus = .available
                    logStore.log("PE32 SHADOW PASS — translated fetch/stack/store/atomic/reverse map all passed.", level: .success)
                } else {
                    jitStatus = .unavailable
                    logStore.log("PE32 SHADOW FAIL — code \(result). Check FEX log above.", level: .error)
                }
            }
        }
    }

'''
if "private func runPE32ShadowTest()" not in v:
    if runner_anchor not in v:
        raise SystemExit("ContentView runFEXTest anchor missing")
    v = v.replace(runner_anchor, runner + runner_anchor, 1)

button_anchor = '                Button("PE32 WoW64 Test") {'
button = r'''                Button("PE32 Shadow Test") {
                    runPE32ShadowTest()
                }
                .buttonStyle(.borderedProminent)
                .tint(.purple)

'''
if 'Button("PE32 Shadow Test")' not in v:
    if button_anchor not in v:
        raise SystemExit("ContentView PE32 WoW64 button anchor missing; run prepare_pe32_probe first")
    v = v.replace(button_anchor, button + button_anchor, 1)

content.write_text(v, encoding="utf-8")
print("Installed PE32 guest-shadow FEX gate")
