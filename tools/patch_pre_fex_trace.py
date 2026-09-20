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

# Narrow the crash after WINE_PROCESS_START_ENTER. These checkpoints surround
# the major statements in wine_process_start without changing their behavior.
# Keep this patch source-driven so we can iterate without rebuilding Wine/FEX.
start = s.index(signature)
if start < 0:
    raise SystemExit("wine_process_start disappeared after instrumentation")
brace = s.index("{", start)
depth = 0
end = None
for i in range(brace, len(s)):
    if s[i] == "{":
        depth += 1
    elif s[i] == "}":
        depth -= 1
        if depth == 0:
            end = i + 1
            break
if end is None:
    raise SystemExit("could not bound wine_process_start")
body = s[start:end]
lines = body.splitlines(True)
out = []
statement_no = 0
for line in lines:
    stripped = line.strip()
    # Instrument only top-level-looking executable statements. Avoid labels,
    # braces, declarations, logging/checkpoint calls and control-flow headers.
    indent = len(line) - len(line.lstrip())
    eligible = (
        indent == 4 and stripped and stripped.endswith(";")
        and not stripped.startswith(("//", "#", "return ", "madeira_bridge_checkpoint("))
        and not stripped.startswith(("char ", "int ", "BOOL ", "NSString ", "NSURL ", "NSError ", "dispatch_", "const "))
    )
    if eligible:
        statement_no += 1
        out.append(f'    madeira_bridge_checkpoint("WINEPROC_STEP_{statement_no:02d}_BEGIN");\n')
        out.append(line)
        out.append(f'    madeira_bridge_checkpoint("WINEPROC_STEP_{statement_no:02d}_OK");\n')
    else:
        out.append(line)
newbody = "".join(out)
s = s[:start] + newbody + s[end:]
print(f"Installed {statement_no} Wine-process statement checkpoints")

# The device reached WINEPROC_STEP_03_BEGIN but the live wineserver log also
# showed create_process/create_thread activity. Make the launcher-side logging
# robust (use the owned duplicate, not the caller's temporary pointer) and add
# descriptive checkpoints on the actual Wine worker thread so the next crash
# cannot be mis-attributed to the handoff thread.
owned_old = '''    madeira_bridge_checkpoint("WINEPROC_STEP_01_BEGIN");
    g_prefix_path = strdup(prefix_path);
    madeira_bridge_checkpoint("WINEPROC_STEP_01_OK");
'''
owned_new = '''    madeira_bridge_checkpoint("WINEPROC_STEP_01_BEGIN");
    g_prefix_path = strdup(prefix_path);
    if (!g_prefix_path) {
        madeira_bridge_checkpoint("WINEPROC_PREFIX_DUP_FAIL");
        return -1;
    }
    madeira_bridge_checkpoint("WINEPROC_STEP_01_OK");
'''
if owned_old not in s:
    raise SystemExit("owned-prefix launcher anchor missing")
s = s.replace(owned_old, owned_new, 1)

log_old = '    LOG("Starting Wine process with prefix: %{public}s", prefix_path);'
log_new = '    LOG("Starting Wine process with prefix: %{public}s", g_prefix_path);'
if log_old not in s:
    raise SystemExit("owned-prefix LOG anchor missing")
s = s.replace(log_old, log_new, 1)

# The worker is defined earlier than madeira_bridge_checkpoint's definition.
# Give it a forward declaration before inserting calls into that worker.
worker_sig = 'static void *wine_process_thread(void *arg) {'
if 'static void madeira_bridge_checkpoint(const char *stage);\n\n' + worker_sig not in s:
    if worker_sig not in s:
        raise SystemExit("Wine worker signature missing")
    s = s.replace(worker_sig,
                  'static void madeira_bridge_checkpoint(const char *stage);\n\n' + worker_sig,
                  1)

worker_replacements = [
    (
        'static void *wine_process_thread(void *arg) {\n    @autoreleasepool {',
        'static void *wine_process_thread(void *arg) {\n    madeira_bridge_checkpoint("WINE_THREAD_ENTER");\n    @autoreleasepool {',
    ),
    (
        '        pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);',
        '        pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);\n        madeira_bridge_checkpoint("WINE_THREAD_QOS_OK");',
    ),
    (
        '        madeira_seed_prefix_if_needed(g_prefix_path);',
        '        madeira_bridge_checkpoint("WINE_THREAD_PREFIX_SEED_BEGIN");\n        madeira_seed_prefix_if_needed(g_prefix_path);\n        madeira_bridge_checkpoint("WINE_THREAD_PREFIX_SEED_OK");',
    ),
    (
        '        setenv("WINELOADERNOEXEC", "1", 1);',
        '        setenv("WINELOADERNOEXEC", "1", 1);\n        madeira_bridge_checkpoint("WINE_THREAD_BASE_ENV_READY");',
    ),
    (
        '        LOG("Target exe: %{public}s (bundle=%{public}s)", madeira_exe, bundle_subdir);',
        '        LOG("Target exe: %{public}s (bundle=%{public}s)", madeira_exe, bundle_subdir);\n        madeira_bridge_checkpoint("WINE_THREAD_TARGET_SELECTED");',
    ),
    (
        '        wine_ios_exit_initialized = 1;',
        '        wine_ios_exit_initialized = 1;\n        madeira_bridge_checkpoint("WINE_THREAD_EXIT_GUARD_READY");',
    ),
    (
        '        LOG("Calling __wine_main...");',
        '        madeira_bridge_checkpoint("WINE_THREAD_BEFORE_WINE_MAIN");\n        LOG("Calling __wine_main...");',
    ),
    (
        '        if (setjmp(wine_ios_exit_jmpbuf) == 0) {\n            __wine_main(argc, argv);',
        '        if (setjmp(wine_ios_exit_jmpbuf) == 0) {\n            madeira_bridge_checkpoint("WINE_THREAD_WINE_MAIN_ENTER");\n            __wine_main(argc, argv);\n            madeira_bridge_checkpoint("WINE_THREAD_WINE_MAIN_RETURN");',
    ),
]
for old, new in worker_replacements:
    if old not in s:
        raise SystemExit(f"Wine worker checkpoint anchor missing: {old[:80]!r}")
    s = s.replace(old, new, 1)

for include in ("#include <fcntl.h>", "#include <unistd.h>", "#include <mach/mach.h>", "#include <stdlib.h>"):
    if include not in s:
        s = include + "\n" + s
wine_bridge.write_text(s, encoding="utf-8")
print("Installed pre-FEX launch, sequence, Wine bridge checkpoints, and guest-shadow arena publisher")
