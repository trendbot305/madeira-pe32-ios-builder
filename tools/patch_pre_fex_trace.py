#!/usr/bin/env python3
from pathlib import Path
import sys

root = Path(sys.argv[1])
content_view = root / "app/Madeira/ContentView.swift"
app_file = root / "app/Madeira/MadeiraApp.swift"
wine_bridge = root / "app/Madeira/WineProcessBridge.m"
wine_header = root / "app/Madeira/WineProcessBridge.h"

s = content_view.read_text(encoding="utf-8")
helper = r'''
func madeiraEarlyCheckpoint(_ stage: String) {
    autoreleasepool {
        guard let docs = FileManager.default.urls(for: .documentDirectory,
                                                   in: .userDomainMask).first else { return }
        let url = docs.appendingPathComponent("madeira-crash-checkpoints.txt")
        let line = String(format: "%.3f | pid=%d | %@\n",
                          Date().timeIntervalSince1970, getpid(), stage)
        guard let data = line.data(using: .utf8) else { return }
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        guard let handle = try? FileHandle(forWritingTo: url) else { return }
        do {
            try handle.seekToEnd()
            try handle.write(contentsOf: data)
            try handle.synchronize()
            try handle.close()
        } catch {
            try? handle.close()
        }
    }
}

'''
anchor = "/// Raw window-level host for the presenting CAMetalLayer."
if "func madeiraEarlyCheckpoint(" not in s:
    if anchor not in s:
        raise SystemExit("ContentView early-checkpoint anchor missing")
    s = s.replace(anchor, helper + anchor, 1)

replacements = [
    ("    private func runFEXTest() {\n",
     "    private func runFEXTest() {\n        madeiraEarlyCheckpoint(\"RUN_FEX_TEST_ENTER\")\n"),
    ("    private func runWineFullSequence() {\n",
     "    private func runWineFullSequence() {\n        madeiraEarlyCheckpoint(\"RUN_WINE_FULL_SEQUENCE_ENTER\")\n"),
    ("            .onAppear {\n",
     "            .onAppear {\n                madeiraEarlyCheckpoint(\"CONTENT_VIEW_ON_APPEAR\")\n"),
]
for old, new in replacements:
    if old not in s:
        raise SystemExit(f"ContentView marker missing: {old!r}")
    s = s.replace(old, new, 1)
# Reserve the PE32 shadow only AFTER Madeira's large JIT pool has selected
# its aliases. The v49 startup reservation grabbed 0x7000000000 first, which is
# the exact band StikJITHelper normally uses for the pool RW alias.
pool_old = """            let pool = StikJITHelper.allocatePool(poolSize: poolSizeMB * 1024 * 1024)
            let elapsed = CFAbsoluteTimeGetCurrent() - t0
            winios_phase("pool-ready")
            logStore.log("BRK suspension lasted \\(String(format: "%.2f", elapsed))s")"""
pool_new = """            madeiraEarlyCheckpoint("WINESEQ_POOL_ALLOC_BEGIN")
            let pool = StikJITHelper.allocatePool(poolSize: poolSizeMB * 1024 * 1024)
            madeiraEarlyCheckpoint(pool != nil ? "WINESEQ_POOL_ALLOC_OK" : "WINESEQ_POOL_ALLOC_FAIL")
            let elapsed = CFAbsoluteTimeGetCurrent() - t0
            winios_phase("pool-ready")
            logStore.log("BRK suspension lasted \\(String(format: "%.2f", elapsed))s")

            if pool != nil {
                let shadowReady = madeira_guest_shadow_ensure()
                madeiraEarlyCheckpoint(shadowReady == 1
                    ? "WINESEQ_GUEST_SHADOW_READY"
                    : "WINESEQ_GUEST_SHADOW_FAIL")
            }"""
if pool_old not in s:
    raise SystemExit("ContentView JIT-pool anchor missing")
s = s.replace(pool_old, pool_new, 1)

server_old = """            // Step 2: Start wineserver
            self.startWineserver()
            winios_phase("wineserver-up")"""
server_new = """            // Step 2: Start wineserver
            madeiraEarlyCheckpoint("WINESEQ_WINESERVER_START")
            self.startWineserver()
            madeiraEarlyCheckpoint("WINESEQ_WINESERVER_RETURN")
            winios_phase("wineserver-up")"""
if server_old not in s:
    raise SystemExit("ContentView wineserver anchor missing")
s = s.replace(server_old, server_new, 1)

wine_old = """            Thread.sleep(forTimeInterval: 2.0)
            winios_phase("wine-start")
            self.startWineProcess()"""
wine_new = """            Thread.sleep(forTimeInterval: 2.0)
            winios_phase("wine-start")
            madeiraEarlyCheckpoint("WINESEQ_WINE_PROCESS_START")
            self.startWineProcess()
            madeiraEarlyCheckpoint("WINESEQ_WINE_PROCESS_RETURN")"""
if wine_old not in s:
    raise SystemExit("ContentView Wine-process anchor missing")
s = s.replace(wine_old, wine_new, 1)

content_view.write_text(s, encoding="utf-8")

# Instrument the actual StikJIT pool allocator. Run 35 now dies at
# WINESEQ_POOL_ALLOC_FAIL, so record the debugger RX placement, why each
# placement is rejected, deallocation result, and RW remap/protect results.
stik = root / "app/Madeira/StikJITHelper.swift"
j = stik.read_text(encoding="utf-8")
if "import Darwin\n" not in j:
    j = "import Darwin\n" + j
pool_replacements = [
    (
        '        LogStore.shared.log("Allocating \\(poolSize / 1024 / 1024)MB JIT pool via debugger...")',
        '        LogStore.shared.log("Allocating \\(poolSize / 1024 / 1024)MB JIT pool via debugger...")\n'
        '        madeiraEarlyCheckpoint("JITPOOL_ENTER_SIZE_\\(poolSize)")',
    ),
    (
        '        for attempt in 0..<3 {\n'
        '            guard let p = jit26_prepare_region(nil, poolSize), p != UnsafeMutableRawPointer(bitPattern: 0) else {\n'
        '                LogStore.shared.log("Debugger failed to allocate RX memory (attempt \\(attempt))", level: .error)\n'
        '                break\n'
        '            }',
        '        // Keep the original unconstrained request first, then try explicit\n'
        '        // high-VA hints if the debugger-backed allocator rejects it.\n'
        '        let poolHints: [UInt64?] = [nil, 0x200000000, 0x400000000, 0x600000000]\n'
        '        for attempt in 0..<poolHints.count {\n'
        '            let hintValue = poolHints[attempt]\n'
        '            let hint = hintValue.flatMap { UnsafeMutableRawPointer(bitPattern: UInt($0)) }\n'
        '            let hintText = hintValue.map { String(format: "0x%llx", $0) } ?? "ANYWHERE"\n'
        '            errno = 0\n'
        '            madeiraEarlyCheckpoint(String(format: "JITPOOL_REQ_ATTEMPT_%d_BASE_%@_SIZE_0x%llx_PROT_RX_FLAGS_BRK_M", attempt, hintText, UInt64(poolSize)))\n'
        '            guard let p = jit26_prepare_region(hint, poolSize), p != UnsafeMutableRawPointer(bitPattern: 0) else {\n'
        '                madeiraEarlyCheckpoint(String(format: "JITPOOL_REQ_ATTEMPT_%d_FAIL_BASE_%@_SIZE_0x%llx_PROT_RX_FLAGS_BRK_M_ERRNO_%d", attempt, hintText, UInt64(poolSize), errno))\n'
        '                continue\n'
        '            }\n'
        '            madeiraEarlyCheckpoint(String(format: "JITPOOL_REQ_ATTEMPT_%d_OK_ADDR_0x%llx_ERRNO_%d", attempt, UInt64(UInt(bitPattern: p)), errno))',
    ),
    (
        '            let a = Int(bitPattern: p)\n'
        '            let inGuestWindow = a + poolSize > guestLo && a < guestHi',
        '            let a = Int(bitPattern: p)\n'
        '            madeiraEarlyCheckpoint(String(format: "JITPOOL_RX_ATTEMPT_%d_ADDR_0x%llx_SIZE_0x%llx", attempt, UInt64(a), UInt64(poolSize)))\n'
        '            let inGuestWindow = a + poolSize > guestLo && a < guestHi',
    ),
    (
        '            if a >= goodLow && !inGuestWindow {\n'
        '                rxPtrOpt = p',
        '            if a >= goodLow && !inGuestWindow {\n'
        '                madeiraEarlyCheckpoint("JITPOOL_RX_ATTEMPT_\\(attempt)_ACCEPT")\n'
        '                rxPtrOpt = p',
    ),
    (
        '            LogStore.shared.log(String(format: "BAD POOL placement 0x%lx (%@) — re-rolling (attempt %d)",',
        '            madeiraEarlyCheckpoint("JITPOOL_RX_ATTEMPT_\\(attempt)_REJECT_\\(a < goodLow ? "LOW" : "GUEST_WINDOW")")\n'
        '            LogStore.shared.log(String(format: "BAD POOL placement 0x%lx (%@) — re-rolling (attempt %d)",',
    ),
    (
        '            let dkr = vm_deallocate(mach_task_self_, vm_address_t(a), vm_size_t(poolSize))',
        '            let dkr = vm_deallocate(mach_task_self_, vm_address_t(a), vm_size_t(poolSize))\n'
        '            madeiraEarlyCheckpoint("JITPOOL_RX_ATTEMPT_\\(attempt)_DEALLOC_KR_\\(dkr)")',
    ),
    (
        '        guard let rxPtr = rxPtrOpt else {',
        '        guard let rxPtr = rxPtrOpt else {\n'
        '            madeiraEarlyCheckpoint("JITPOOL_NO_VALID_RX_PLACEMENT")',
    ),
    (
        '        let kr1 = vm_remap(',
        '        madeiraEarlyCheckpoint("JITPOOL_RW_REMAP_BEGIN")\n'
        '        let kr1 = vm_remap(',
    ),
    (
        '        guard kr1 == KERN_SUCCESS else {',
        '        madeiraEarlyCheckpoint("JITPOOL_RW_REMAP_KR_\\(kr1)_ADDR_\\(String(format: "0x%llx", UInt64(rwAddr)))")\n'
        '        guard kr1 == KERN_SUCCESS else {',
    ),
    (
        '        let kr2 = vm_protect(mach_task_self_, rwAddr, vm_size_t(poolSize), 0, VM_PROT_READ | VM_PROT_WRITE)',
        '        let kr2 = vm_protect(mach_task_self_, rwAddr, vm_size_t(poolSize), 0, VM_PROT_READ | VM_PROT_WRITE)\n'
        '        madeiraEarlyCheckpoint("JITPOOL_RW_PROTECT_KR_\\(kr2)")',
    ),
    (
        '        LogStore.shared.log("JIT pool ready (debugger still attached).", level: .success)',
        '        madeiraEarlyCheckpoint("JITPOOL_READY")\n'
        '        LogStore.shared.log("JIT pool ready (debugger still attached).", level: .success)',
    ),
]
for old, new in pool_replacements:
    if old not in j:
        raise SystemExit(f"StikJIT pool instrumentation anchor missing: {old[:100]!r}")
    j = j.replace(old, new, 1)
stik.write_text(j, encoding="utf-8")

s = app_file.read_text(encoding="utf-8")
old = "struct MadeiraApp: App {\n    var body: some Scene {"
new = '''struct MadeiraApp: App {
    init() {
        madeiraEarlyCheckpoint("APP_INIT_ENTER")
        let lowVAResult = madeira_low_va_probe()
        madeiraEarlyCheckpoint(lowVAResult == 1 ? "LOW_VA_PROBE_OK" : "LOW_VA_PROBE_FAIL_\\(lowVAResult)")
    }

    var body: some Scene {'''
if old not in s:
    raise SystemExit("MadeiraApp init marker missing")
app_file.write_text(s.replace(old, new, 1), encoding="utf-8")

header = wine_header.read_text(encoding="utf-8")
declarations = """int madeira_low_va_probe(void);
int madeira_guest_shadow_ensure(void);"""
if "int madeira_guest_shadow_ensure(void);" not in header:
    anchor = "int wine_process_start(const char *prefix_path);"
    if anchor not in header:
        raise SystemExit("WineProcessBridge header anchor missing")
    header = header.replace(anchor, declarations + "\n" + anchor, 1)
wine_header.write_text(header, encoding="utf-8")

s = wine_bridge.read_text(encoding="utf-8")

# Keep the shared Wine prefix in process-lifetime storage. LiveContainer can
# relaunch the embedded app without giving us a conventional fresh process
# lifetime for every attempt, so a malloc-owned global that is later free()'d
# is a poor fit for this boundary.
prefix_global_old = "static char *g_prefix_path = NULL;"
prefix_global_new = """static char g_prefix_path_storage[PATH_MAX];
static char *g_prefix_path = NULL;"""
if prefix_global_old not in s:
    raise SystemExit("Wine prefix global anchor missing")
s = s.replace(prefix_global_old, prefix_global_new, 1)
bridge_helper = r'''
static void madeira_bridge_checkpoint(const char *stage)
{
    @autoreleasepool {
        NSString *docs = NSSearchPathForDirectoriesInDomains(NSDocumentDirectory,
                                                              NSUserDomainMask, YES).firstObject;
        if (!docs) return;
        NSString *path = [docs stringByAppendingPathComponent:@"madeira-crash-checkpoints.txt"];
        int fd = open(path.fileSystemRepresentation, O_CREAT | O_WRONLY | O_APPEND, 0600);
        if (fd < 0) return;
