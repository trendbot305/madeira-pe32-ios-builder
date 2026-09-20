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
declaration = "int madeira_low_va_probe(void);"
if declaration not in header:
    anchor = "int wine_process_start(const char *prefix_path);"
    if anchor not in header:
        raise SystemExit("WineProcessBridge header anchor missing")
    header = header.replace(anchor, declaration + "\n" + anchor, 1)
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

int madeira_low_va_probe(void)
{
    const kern_return_t low = madeira_probe_fixed_window(
        (vm_address_t)0x70000000ULL, (vm_size_t)0x4000, "LOW_VA_1792M");

    /*
     * Fixed high-address guesses were diagnostic only. The device already
     * proved that iOS rejects those exact placements while VM_FLAGS_ANYWHERE
     * succeeds. Test the architecture we can actually use: a relocatable,
     * contiguous high backing arena. Try the full 32-bit 4 GiB span first,
     * then measured fallbacks for sparse-region mode.
     */
    (void)madeira_probe_anywhere((vm_size_t)0x100000000ULL, "ANYWHERE_4G");
    (void)madeira_probe_anywhere((vm_size_t)0xC0000000ULL, "ANYWHERE_3G");
    (void)madeira_probe_anywhere((vm_size_t)0x80000000ULL, "ANYWHERE_2G");
    (void)madeira_probe_anywhere((vm_size_t)0x40000000ULL, "ANYWHERE_1G");

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
for include in ("#include <fcntl.h>", "#include <unistd.h>", "#include <mach/mach.h>"):
    if include not in s:
        s = include + "\n" + s
wine_bridge.write_text(s, encoding="utf-8")
print("Installed pre-FEX launch, sequence, and Wine bridge checkpoints")
