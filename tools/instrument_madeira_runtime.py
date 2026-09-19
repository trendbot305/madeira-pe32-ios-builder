from pathlib import Path
import sys

root = Path(sys.argv[1])
p = root / "app/Madeira/FEXBridge.mm"
s = p.read_text()

foundation = '#import <Foundation/Foundation.h>'
if foundation not in s:
    s = foundation + '\\n' + s
marker = "static fex_log_callback_t g_fex_log_callback = nullptr;"
helper = r'''static fex_log_callback_t g_fex_log_callback = nullptr;

static void fex_crash_checkpoint(const char *stage) {
    @autoreleasepool {
        NSArray<NSURL *> *urls = [[NSFileManager defaultManager] URLsForDirectory:NSDocumentDirectory
                                                                        inDomains:NSUserDomainMask];
        NSURL *docs = urls.firstObject;
        if (!docs) return;
        NSURL *url = [docs URLByAppendingPathComponent:@"madeira-crash-checkpoints.txt"];
        NSString *line = [NSString stringWithFormat:@"%.3f | pid=%d | %@\n",
                          [[NSDate date] timeIntervalSince1970],
                          getpid(),
                          [NSString stringWithUTF8String:stage ?: "(null)"]];
        NSData *data = [line dataUsingEncoding:NSUTF8StringEncoding];
        if (![[NSFileManager defaultManager] fileExistsAtPath:url.path]) {
            [[NSFileManager defaultManager] createFileAtPath:url.path contents:nil attributes:nil];
        }
        NSFileHandle *h = [NSFileHandle fileHandleForWritingAtPath:url.path];
        if (h) {
            [h seekToEndOfFile];
            [h writeData:data];
            [h synchronizeFile];
            [h closeFile];
        }
    }
}'''
if marker not in s:
    raise SystemExit("FEX log marker not found")
s = s.replace(marker, helper, 1)

replacements = [
    ('    fex_log("=== FEXCore Initialization ===");',
     '    fex_crash_checkpoint("FEX_INIT_ENTER");\n    fex_log("=== FEXCore Initialization ===");'),
    ('    if (!jit_pool_init()) {',
     '    fex_crash_checkpoint("JIT_POOL_INIT_BEGIN");\n    if (!jit_pool_init()) {'),
    ('    // Step 2: Install mmap hooks BEFORE FEXCore does any allocations',
     '    fex_crash_checkpoint("JIT_POOL_INIT_OK");\n\n    // Step 2: Install mmap hooks BEFORE FEXCore does any allocations'),
    ('    fex_log("Installing mmap hooks...");',
     '    fex_crash_checkpoint("FEX_MMAP_HOOKS_BEGIN");\n    fex_log("Installing mmap hooks...");'),
    ('        fex_log("  Calling FEXCore::Config::Initialize()...");',
     '        fex_crash_checkpoint("FEX_CONFIG_INIT_BEGIN");\n        fex_log("  Calling FEXCore::Config::Initialize()...");'),
    ('        fex_log("  Config::Initialize() returned OK");',
     '        fex_log("  Config::Initialize() returned OK");\n        fex_crash_checkpoint("FEX_CONFIG_INIT_OK");'),
    ('    fex_log("Creating FEXCore context...");',
     '    fex_crash_checkpoint("FEX_CONTEXT_CREATE_BEGIN");\n    fex_log("Creating FEXCore context...");'),
    ('    // Step 7: Set handlers',
     '    fex_crash_checkpoint("FEX_CONTEXT_CREATE_OK");\n\n    // Step 7: Set handlers'),
    ('    fex_log("Initializing FEXCore core (creates Dispatcher)...");',
     '    fex_crash_checkpoint("FEX_INITCORE_BEGIN");\n    fex_log("Initializing FEXCore core (creates Dispatcher)...");'),
    ('    g_initialized.store(true);',
     '    fex_crash_checkpoint("FEX_INITCORE_OK");\n    g_initialized.store(true);'),
    ('    fex_log("=== FEX Execution Test ===");',
     '    fex_crash_checkpoint("FEX_EXEC_TEST_ENTER");\n    fex_log("=== FEX Execution Test ===");'),
]
for old, new in replacements:
    if old not in s:
        raise SystemExit(f"checkpoint insertion marker missing: {old}")
    s = s.replace(old, new, 1)

include = '#include <signal.h>'
if include not in s:
    raise SystemExit("signal include missing")
s = s.replace(include, include + '\n#include <unistd.h>', 1)

p.write_text(s)
print("Installed persistent FEX runtime checkpoints")
