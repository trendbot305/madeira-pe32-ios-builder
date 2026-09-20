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
        char line[256];
        int count = snprintf(line, sizeof(line), "%.3f | pid=%d | %s\n",
                             [NSDate date].timeIntervalSince1970, getpid(),
                             stage ? stage : "(null)");
        if (count > 0) {
            size_t length = (size_t)count < sizeof(line) ? (size_t)count : sizeof(line) - 1;
            (void)write(fd, line, length);
            (void)fsync(fd);
        }
        (void)close(fd);
    }
}

'''
probe_function = r'''
static vm_address_t g_madeira_guest_shadow_base = 0;
static vm_size_t g_madeira_guest_shadow_size = 0;

static void madeira_publish_hex_env(const char *name, unsigned long long value)
{
    char buffer[32];
    snprintf(buffer, sizeof(buffer), "%llx", value);
    setenv(name, buffer, 1);
}

static void madeira_publish_guest_shadow_arena(vm_address_t address, vm_size_t size, const char *name)
{
    if (!address || size != (vm_size_t)0x100000000ULL) return;
    g_madeira_guest_shadow_base = address;
    g_madeira_guest_shadow_size = size;
    madeira_publish_hex_env("WINE_IOS_FEX_GUEST_BIAS", (unsigned long long)address);
    madeira_publish_hex_env("WINE_IOS_FEX_GUEST_LIMIT", 0x100000000ULL);
    madeira_publish_hex_env("WINE_IOS_FEX_REDIRECT_GUEST", 0ULL);
    madeira_publish_hex_env("WINE_IOS_FEX_REDIRECT_HOST", 0ULL);

    char stage[192];
    snprintf(stage, sizeof(stage), "GUEST_SHADOW_PUBLISHED_%s_BASE_0x%llx_SIZE_0x%llx",
             name, (unsigned long long)address, (unsigned long long)size);
    madeira_bridge_checkpoint(stage);
}

static kern_return_t madeira_probe_fixed_window(vm_address_t requested,
                                                   vm_size_t size,
                                                   const char *name)
{
    vm_address_t address = requested;
    kern_return_t result = vm_allocate(mach_task_self(), &address, size, VM_FLAGS_FIXED);
    char stage[160];
    if (result == KERN_SUCCESS) {
        snprintf(stage, sizeof(stage), "%s_OK_0x%llx_0x%llx",
                 name, (unsigned long long)address, (unsigned long long)size);
        madeira_bridge_checkpoint(stage);
        (void)vm_deallocate(mach_task_self(), address, size);
    } else {
        snprintf(stage, sizeof(stage), "%s_FAIL_KR_%d",
                 name, (int)result);
        madeira_bridge_checkpoint(stage);
    }
    return result;
}

static kern_return_t madeira_probe_anywhere(vm_size_t size, const char *name)
{
    vm_address_t address = 0;
    kern_return_t result = vm_allocate(mach_task_self(), &address, size, VM_FLAGS_ANYWHERE);
    char stage[160];
    if (result == KERN_SUCCESS) {
        snprintf(stage, sizeof(stage), "%s_OK_ADDR_0x%llx_SIZE_0x%llx",
                 name, (unsigned long long)address, (unsigned long long)size);
        madeira_bridge_checkpoint(stage);
        (void)vm_deallocate(mach_task_self(), address, size);
    } else {
        snprintf(stage, sizeof(stage), "%s_FAIL_KR_%d", name, (int)result);
        madeira_bridge_checkpoint(stage);
    }
    return result;
}

static kern_return_t madeira_reserve_guest_shadow_fixed(vm_address_t requested,
                                                            vm_size_t size,
                                                            const char *name)
{
    vm_address_t address = requested;
    kern_return_t result = vm_allocate(mach_task_self(), &address, size, VM_FLAGS_FIXED);
    char stage[224];
    if (result == KERN_SUCCESS) {
        snprintf(stage, sizeof(stage), "%s_FIXED_OK_ADDR_0x%llx_SIZE_0x%llx",
                 name, (unsigned long long)address, (unsigned long long)size);
        madeira_bridge_checkpoint(stage);
        madeira_publish_guest_shadow_arena(address, size, name);
    } else {
        snprintf(stage, sizeof(stage), "%s_FIXED_FAIL_ADDR_0x%llx_KR_%d",
                 name, (unsigned long long)requested, (int)result);
        madeira_bridge_checkpoint(stage);
    }
    return result;
}

static kern_return_t madeira_reserve_guest_shadow_anywhere(vm_size_t size, const char *name)
{
    vm_address_t address = 0;
    kern_return_t result = vm_allocate(mach_task_self(), &address, size, VM_FLAGS_ANYWHERE);
    char stage[192];
    if (result == KERN_SUCCESS) {
        snprintf(stage, sizeof(stage), "%s_ANYWHERE_OK_ADDR_0x%llx_SIZE_0x%llx",
                 name, (unsigned long long)address, (unsigned long long)size);
        madeira_bridge_checkpoint(stage);
        if (size == (vm_size_t)0x100000000ULL) {
            madeira_publish_guest_shadow_arena(address, size, name);
        } else {
            (void)vm_deallocate(mach_task_self(), address, size);
        }
    } else {
        snprintf(stage, sizeof(stage), "%s_ANYWHERE_FAIL_KR_%d", name, (int)result);
        madeira_bridge_checkpoint(stage);
    }
    return result;
}

static void madeira_configure_guest_shadow_arena(void)
{
    if (g_madeira_guest_shadow_base || getenv("WINE_IOS_FEX_GUEST_BIAS")) return;

    /*
     * Madeira's x64 guest/host window is 0x7000000000..0x8000000000.
     * After the JIT pool is created its RW alias commonly begins at
     * 0x7000000000 and ends around 0x7038000000. VM_FLAGS_ANYWHERE therefore
     * hands a 4GiB request 0x7038000000 -- exactly the lowest remaining x64
     * guest space. The native PE32 gate tolerates that, but full Wine/FEX
     * subsequently needs the same low part of the window.
     *
     * Prefer the final 4GiB of the 480GiB slot (0x7b..0x7c). This keeps the
     * low guest band available and stays below the 496GiB slot where Madeira
     * documents Wine furniture clustering. If occupied, try the analogous
     * tail of the 464GiB and 448GiB slots before falling back to ANYWHERE.
     */
    static const vm_address_t candidates[] = {
        (vm_address_t)0x7b00000000ULL,
        (vm_address_t)0x7700000000ULL,
        (vm_address_t)0x7300000000ULL,
    };

    for (unsigned i = 0; i < sizeof(candidates) / sizeof(candidates[0]); ++i) {
        char name[64];
        snprintf(name, sizeof(name), "GUEST_SHADOW_4G_CANDIDATE_%u", i);
        if (madeira_reserve_guest_shadow_fixed(candidates[i],
                                               (vm_size_t)0x100000000ULL,
                                               name) == KERN_SUCCESS
            && g_madeira_guest_shadow_base) {
            return;
        }
    }

    if (madeira_reserve_guest_shadow_anywhere((vm_size_t)0x100000000ULL,
                                              "GUEST_SHADOW_4G_FALLBACK") == KERN_SUCCESS
        && g_madeira_guest_shadow_base) {
        return;
    }

    /* Probe-only fallbacks. These prove capacity but are not published until
     * sparse guest-page mapping is fully wired. */
    (void)madeira_reserve_guest_shadow_anywhere((vm_size_t)0xC0000000ULL,
                                                "GUEST_SHADOW_3G_PROBE_ONLY");
    (void)madeira_reserve_guest_shadow_anywhere((vm_size_t)0x80000000ULL,
                                                "GUEST_SHADOW_2G_PROBE_ONLY");
    madeira_bridge_checkpoint("GUEST_SHADOW_LINEAR_4G_UNAVAILABLE_SPARSE_REQUIRED");
}

int madeira_guest_shadow_ensure(void)
{
    madeira_bridge_checkpoint("GUEST_SHADOW_ENSURE_BEGIN");
    madeira_configure_guest_shadow_arena();
    if (g_madeira_guest_shadow_base && getenv("WINE_IOS_FEX_GUEST_BIAS")) {
        madeira_bridge_checkpoint("GUEST_SHADOW_ENSURE_OK");
        return 1;
    }
    madeira_bridge_checkpoint("GUEST_SHADOW_ENSURE_FAIL");
    return 0;
}

int madeira_low_va_probe(void)
{
    const kern_return_t low = madeira_probe_fixed_window(
        (vm_address_t)0x70000000ULL, (vm_size_t)0x4000, "LOW_VA_1792M");

    (void)madeira_probe_fixed_window(
        (vm_address_t)0x200000000ULL, (vm_size_t)0x4000, "BIAS_8G_PAGE");
    (void)madeira_probe_fixed_window(
        (vm_address_t)0x280000000ULL, (vm_size_t)0x4000, "BIAS_10G_PAGE");
    (void)madeira_probe_fixed_window(
        (vm_address_t)0x300000000ULL, (vm_size_t)0x4000, "BIAS_12G_PAGE");
    (void)madeira_probe_fixed_window(
        (vm_address_t)0x300000000ULL, (vm_size_t)0x80000000ULL, "BIAS_12G_2G");
    (void)madeira_probe_fixed_window(
        (vm_address_t)0x400000000ULL, (vm_size_t)0x4000, "BIAS_16G_PAGE");

    /* Capacity probes only. The real 4GiB shadow is held later, after the
     * JIT pool has selected its RX/RW aliases. */
    (void)madeira_probe_anywhere((vm_size_t)0x100000000ULL, "ANYWHERE_4G");
    (void)madeira_probe_anywhere((vm_size_t)0xC0000000ULL, "ANYWHERE_3G");
    (void)madeira_probe_anywhere((vm_size_t)0x80000000ULL, "ANYWHERE_2G");
    (void)madeira_probe_anywhere((vm_size_t)0x40000000ULL, "ANYWHERE_1G");
    (void)madeira_probe_anywhere((vm_size_t)0x20000000ULL, "ANYWHERE_512M");
    return low == KERN_SUCCESS ? 1 : -(int)low;
}

'''
signature = "int wine_process_start(const char *prefix_path) {\n"
if signature not in s:
    raise SystemExit("wine_process_start marker missing")
if "static void madeira_bridge_checkpoint(" not in s:
    s = s.replace(signature, bridge_helper + probe_function + signature, 1)
s = s.replace(signature,
              signature + '    madeira_bridge_checkpoint("WINE_PROCESS_START_ENTER");\n',
              1)
for include in ("#include <fcntl.h>", "#include <unistd.h>", "#include <mach/mach.h>", "#include <stdlib.h>"):
    if include not in s:
        s = include + "\n" + s
wine_bridge.write_text(s, encoding="utf-8")
print("Installed pre-FEX launch, sequence, Wine bridge checkpoints, and guest-shadow arena publisher")
