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

static int g_fex_checkpoint_fd = -1;

static void fex_fatal_signal_handler(int sig) {
    const char *line = "FATAL_SIGNAL_UNKNOWN\n";
    size_t len = sizeof("FATAL_SIGNAL_UNKNOWN\n") - 1;
    switch (sig) {
        case SIGSEGV: line = "FATAL_SIGNAL_SIGSEGV\n"; len = sizeof("FATAL_SIGNAL_SIGSEGV\n") - 1; break;
        case SIGBUS:  line = "FATAL_SIGNAL_SIGBUS\n";  len = sizeof("FATAL_SIGNAL_SIGBUS\n") - 1; break;
        case SIGILL:  line = "FATAL_SIGNAL_SIGILL\n";  len = sizeof("FATAL_SIGNAL_SIGILL\n") - 1; break;
        case SIGABRT: line = "FATAL_SIGNAL_SIGABRT\n"; len = sizeof("FATAL_SIGNAL_SIGABRT\n") - 1; break;
        case SIGTRAP: line = "FATAL_SIGNAL_SIGTRAP\n"; len = sizeof("FATAL_SIGNAL_SIGTRAP\n") - 1; break;
    }
    if (g_fex_checkpoint_fd >= 0) {
        (void)write(g_fex_checkpoint_fd, line, len);
        (void)fsync(g_fex_checkpoint_fd);
    }
    _exit(128 + sig);
}

static void fex_install_fatal_signal_handlers(void) {
    struct sigaction action = {};
    action.sa_handler = fex_fatal_signal_handler;
    sigemptyset(&action.sa_mask);
    action.sa_flags = SA_RESETHAND;
    const int signals[] = {SIGSEGV, SIGBUS, SIGILL, SIGABRT, SIGTRAP};
    for (int sig : signals) (void)sigaction(sig, &action, nullptr);
}

static void fex_crash_checkpoint(const char *stage) {
    @autoreleasepool {
        NSArray<NSURL *> *urls = [[NSFileManager defaultManager] URLsForDirectory:NSDocumentDirectory
                                                                        inDomains:NSUserDomainMask];
        NSURL *docs = urls.firstObject;
        if (!docs) return;
        NSURL *url = [docs URLByAppendingPathComponent:@"madeira-crash-checkpoints.txt"];
        if (g_fex_checkpoint_fd < 0) {
            g_fex_checkpoint_fd = open(url.fileSystemRepresentation,
                                       O_CREAT | O_WRONLY | O_APPEND, 0600);
            fex_install_fatal_signal_handlers();
        }
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
s = s.replace(include, include + '\n#include <fcntl.h>\n#include <unistd.h>', 1)

p.write_text(s)
print("Installed persistent FEX runtime checkpoints")
