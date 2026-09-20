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
        let sessionURL = docs.appendingPathComponent("madeira-crash-session.txt")
        let session: String
        if let existing = try? String(contentsOf: sessionURL, encoding: .utf8),
           !existing.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            session = existing.trimmingCharacters(in: .whitespacesAndNewlines)
        } else {
            session = String(format: "%.0f-%d", Date().timeIntervalSince1970 * 1000, getpid())
            try? session.write(to: sessionURL, atomically: true, encoding: .utf8)
        }
        let line = String(format: "%.3f | session=%@ | pid=%d | %@\n",
                          Date().timeIntervalSince1970, session, getpid(), stage)
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
        if let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first {
            let sessionURL = docs.appendingPathComponent("madeira-crash-session.txt")
            let session = String(format: "%.0f-%d", Date().timeIntervalSince1970 * 1000, getpid())
            try? session.write(to: sessionURL, atomically: true, encoding: .utf8)
        }
        madeiraEarlyCheckpoint("=== APP_SESSION_BEGIN ===")
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
static char madeira_bridge_checkpoint_path[PATH_MAX];

static void madeira_bridge_checkpoint(const char *stage)
{
    @autoreleasepool {
        NSString *docs = NSSearchPathForDirectoriesInDomains(NSDocumentDirectory,
                                                              NSUserDomainMask, YES).firstObject;
        if (!docs) return;
        NSString *path = [docs stringByAppendingPathComponent:@"madeira-crash-checkpoints.txt"];
        NSString *sessionPath = [docs stringByAppendingPathComponent:@"madeira-crash-session.txt"];
        NSString *session = [NSString stringWithContentsOfFile:sessionPath encoding:NSUTF8StringEncoding error:nil];
        session = [session stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
        if (!session.length) session = [NSString stringWithFormat:@"unknown-%d", getpid()];
        strlcpy(madeira_bridge_checkpoint_path, path.fileSystemRepresentation,
                sizeof(madeira_bridge_checkpoint_path));
        int fd = open(path.fileSystemRepresentation, O_CREAT | O_WRONLY | O_APPEND, 0600);
        if (fd < 0) return;
        char line[256];
        int count = snprintf(line, sizeof(line), "%.3f | session=%s | pid=%d | %s\n",
                             [NSDate date].timeIntervalSince1970, session.UTF8String, getpid(),
                             stage ? stage : "(null)");
        if (count > 0) {
            size_t length = (size_t)count < sizeof(line) ? (size_t)count : sizeof(line) - 1;
            (void)write(fd, line, length);
            (void)fsync(fd);
        }
        (void)close(fd);
    }
}

/* This handler deliberately uses only async-signal-safe operations.  It is
 * installed before Wine startup because FEX's crash checkpoint does not exist
 * yet in the interval we are trying to localise.  FEX may replace it later. */
static void madeira_early_fatal_signal(int signo)
{
    static const char segv[] = "EARLY_FATAL_SIGSEGV\n";
    static const char bus[]  = "EARLY_FATAL_SIGBUS\n";
    static const char abrt[] = "EARLY_FATAL_SIGABRT\n";
    static const char ill[]  = "EARLY_FATAL_SIGILL\n";
    const char *line = abrt;
    size_t length = sizeof(abrt) - 1;

    if (signo == SIGSEGV) { line = segv; length = sizeof(segv) - 1; }
    else if (signo == SIGBUS) { line = bus; length = sizeof(bus) - 1; }
    else if (signo == SIGILL) { line = ill; length = sizeof(ill) - 1; }

    if (madeira_bridge_checkpoint_path[0]) {
        int fd = open(madeira_bridge_checkpoint_path,
                      O_CREAT | O_WRONLY | O_APPEND, 0600);
        if (fd >= 0) {
            (void)write(fd, line, length);
            (void)fsync(fd);
            (void)close(fd);
        }
    }

    struct sigaction action;
    memset(&action, 0, sizeof(action));
    action.sa_handler = SIG_DFL;
    (void)sigemptyset(&action.sa_mask);
    (void)sigaction(signo, &action, NULL);
    (void)kill(getpid(), signo);
    _exit(128 + signo);
}

static void madeira_install_early_fatal_handlers(void)
{
    static int installed;
    if (installed) return;

    struct sigaction action;
    memset(&action, 0, sizeof(action));
    action.sa_handler = madeira_early_fatal_signal;
    (void)sigemptyset(&action.sa_mask);
    action.sa_flags = SA_RESETHAND;
    (void)sigaction(SIGSEGV, &action, NULL);
    (void)sigaction(SIGBUS, &action, NULL);
    (void)sigaction(SIGABRT, &action, NULL);
    (void)sigaction(SIGILL, &action, NULL);
    installed = 1;
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
              signature + '''    madeira_bridge_checkpoint("WINE_PROCESS_START_ENTER");
    madeira_install_early_fatal_handlers();
    madeira_bridge_checkpoint("WPS_FATAL_HANDLERS_INSTALLED");
''',
              1)

startup_replacements = [
    ("    g_prefix_path = strdup(prefix_path);\n",
     "    g_prefix_path = strdup(prefix_path);\n"
     "    madeira_bridge_checkpoint(\"WPS_PREFIX_COPIED\");\n"),
    ("    g_wine_running = 1;\n",
     "    g_wine_running = 1;\n"
     "    madeira_bridge_checkpoint(\"WPS_RUNNING_FLAG_SET\");\n"),
    ("    LOG(\"socketpair created: server_fd=%d, client_fd=%d\", pair[0], pair[1]);\n",
     "    LOG(\"socketpair created: server_fd=%d, client_fd=%d\", pair[0], pair[1]);\n"
     "    madeira_bridge_checkpoint(\"WPS_SOCKETPAIR_OK\");\n"),
    ("    setenv(\"WINESERVERSOCKET\", fd_str, 1);\n",
     "    setenv(\"WINESERVERSOCKET\", fd_str, 1);\n"
     "    madeira_bridge_checkpoint(\"WPS_SETENV_OK\");\n"),
    ("    wineserver_inject_client_fd(pair[0]);\n",
     "    madeira_bridge_checkpoint(\"WPS_WINESERVER_INJECT_BEGIN\");\n"
     "    wineserver_inject_client_fd(pair[0]);\n"
     "    madeira_bridge_checkpoint(\"WPS_WINESERVER_INJECT_RETURNED\");\n"),
    ("    pthread_attr_init(&attr);\n",
     "    pthread_attr_init(&attr);\n"
     "    madeira_bridge_checkpoint(\"WPS_PTHREAD_ATTR_INIT_OK\");\n"),
    ("    pthread_attr_setschedparam(&attr, &sched);\n",
     "    pthread_attr_setschedparam(&attr, &sched);\n"
     "    madeira_bridge_checkpoint(\"WPS_PTHREAD_ATTR_SCHED_OK\");\n"),
    ("    int ret = pthread_create(&g_wine_thread, &attr, wine_process_thread, NULL);\n",
     "    madeira_bridge_checkpoint(\"WPS_PTHREAD_CREATE_BEGIN\");\n"
     "    int ret = pthread_create(&g_wine_thread, &attr, wine_process_thread, NULL);\n"
     "    madeira_bridge_checkpoint(ret == 0 ? \"WPS_PTHREAD_CREATE_OK\" : \"WPS_PTHREAD_CREATE_FAIL\");\n"),
    ("    pthread_detach(g_wine_thread);\n",
     "    pthread_detach(g_wine_thread);\n"
     "    madeira_bridge_checkpoint(\"WPS_PTHREAD_DETACH_OK\");\n"),
    ("static void *wine_process_thread(void *arg) {\n    @autoreleasepool {\n",
     "static void madeira_bridge_checkpoint(const char *stage);\n\n"
     "static void *wine_process_thread(void *arg) {\n"
     "    madeira_bridge_checkpoint(\"WINE_THREAD_ENTER\");\n"
     "    @autoreleasepool {\n"),
    ("        pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);\n",
     "        pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);\n"
     "        madeira_bridge_checkpoint(\"WINE_THREAD_QOS_RETURNED\");\n"),
    ("        LOG(\"Wine process thread started\");\n",
     "        madeira_bridge_checkpoint(\"WINE_THREAD_LOG_BEGIN\");\n"
     "        LOG(\"Wine process thread started\");\n"
     "        madeira_bridge_checkpoint(\"WINE_THREAD_LOG_RETURNED\");\n"),
    ("        madeira_seed_prefix_if_needed(g_prefix_path);\n",
     "        madeira_bridge_checkpoint(\"WINE_THREAD_PREFIX_SEED_BEGIN\");\n"
     "        madeira_seed_prefix_if_needed(g_prefix_path);\n"
     "        madeira_bridge_checkpoint(\"WINE_THREAD_PREFIX_SEED_RETURNED\");\n"),
    ("        setenv(\"WINEPREFIX\", g_prefix_path, 1);\n",
     "        setenv(\"WINEPREFIX\", g_prefix_path, 1);\n"
     "        madeira_bridge_checkpoint(\"WINE_THREAD_WINEPREFIX_SET\");\n"),
    ("        setenv(\"HOME\", g_prefix_path, 1);\n",
     "        setenv(\"HOME\", g_prefix_path, 1);\n"
     "        madeira_bridge_checkpoint(\"WINE_THREAD_HOME_SET\");\n"),
    ("        setenv(\"WINELOADERNOEXEC\", \"1\", 1);\n",
     "        setenv(\"WINELOADERNOEXEC\", \"1\", 1);\n"
     "        madeira_bridge_checkpoint(\"WINE_THREAD_LOADERNOEXEC_SET\");\n"),
    ("            NSString *bundlePath = [[NSBundle mainBundle] bundlePath];\n",
     "            madeira_bridge_checkpoint(\"WINE_THREAD_BUNDLE_PATH_BEGIN\");\n"
     "            NSString *bundlePath = [[NSBundle mainBundle] bundlePath];\n"
     "            madeira_bridge_checkpoint(\"WINE_THREAD_BUNDLE_PATH_RETURNED\");\n"
     "            const char *bundlePathUTF8 = bundlePath.UTF8String;\n"
     "            madeira_bridge_checkpoint(bundlePathUTF8 ? \"WINE_THREAD_BUNDLE_UTF8_OK\" : \"WINE_THREAD_BUNDLE_UTF8_NULL\");\n"),
    ("            setenv(\"WINEDLLPATH\", bundlePath.UTF8String, 1);\n",
     "            setenv(\"WINEDLLPATH\", bundlePathUTF8, 1);\n"
     "            madeira_bridge_checkpoint(\"WINE_THREAD_WINEDLLPATH_SET\");\n"),
    ("            LOG(\"WINEDLLPATH=%{public}s\", bundlePath.UTF8String);\n",
     "            madeira_bridge_checkpoint(\"WINE_THREAD_WINEDLLPATH_LOG_BEGIN\");\n"
     "            LOG(\"WINEDLLPATH=%{public}s\", bundlePathUTF8);\n"
     "            madeira_bridge_checkpoint(\"WINE_THREAD_WINEDLLPATH_LOG_RETURNED\");\n"),
    ("            const char *verbose = getenv(\"MADEIRA_DEBUG_VERBOSE\");\n",
     "            madeira_bridge_checkpoint(\"WINE_THREAD_WINEDEBUG_BEGIN\");\n"
     "            const char *verbose = getenv(\"MADEIRA_DEBUG_VERBOSE\");\n"),
    ("                setenv(\"WINEDEBUG\", \"err+all,fixme+all,warn+module,warn+file,trace+process,trace+module,trace+loaddll,trace+loadorder,trace+win,trace+user32,trace+syscall,trace+file\", 1);\n",
     "                setenv(\"WINEDEBUG\", \"err+all,fixme+all,warn+module,warn+file,trace+process,trace+module,trace+loaddll,trace+loadorder,trace+win,trace+user32,trace+syscall,trace+file\", 1);\n"
     "                madeira_bridge_checkpoint(\"WINE_THREAD_WINEDEBUG_VERBOSE_SET\");\n"),
    ("                setenv(\"WINEDEBUG\", \"err+all,err-virtual\", 1);\n",
     "                setenv(\"WINEDEBUG\", \"err+all,err-virtual\", 1);\n"
     "                madeira_bridge_checkpoint(\"WINE_THREAD_WINEDEBUG_DEFAULT_SET\");\n"),
    ("        setenv(\"MADEIRA_WIN32U\", \"1\", 1);\n",
     "        madeira_bridge_checkpoint(\"WINE_THREAD_WINEDEBUG_DONE\");\n"
     "        setenv(\"MADEIRA_WIN32U\", \"1\", 1);\n"
     "        madeira_bridge_checkpoint(\"WINE_THREAD_WIN32U_SET\");\n"),
    ("        setenv(\"FNA3D_FORCE_DRIVER\", \"D3D11\", 0);\n",
     "        setenv(\"FNA3D_FORCE_DRIVER\", \"D3D11\", 0);\n"
     "        madeira_bridge_checkpoint(\"WINE_THREAD_FNA_DRIVER_SET\");\n"),
    ("        setenv(\"MONO_LOG_LEVEL\", \"warning\", 0);\n",
     "        setenv(\"MONO_LOG_LEVEL\", \"warning\", 0);\n"
     "        madeira_bridge_checkpoint(\"WINE_THREAD_MONO_LEVEL_SET\");\n"),
    ("        setenv(\"MADEIRA_QUIET\", \"1\", 1);\n",
     "        setenv(\"MADEIRA_QUIET\", \"1\", 1);\n"
     "        madeira_bridge_checkpoint(\"WINE_THREAD_QUIET_SET\");\n"),
    ("            NSError *aerr = nil;\n",
     "            madeira_bridge_checkpoint(\"WINE_THREAD_AUDIO_BEGIN\");\n"
     "            NSError *aerr = nil;\n"),
    ("            AVAudioSession *session = [AVAudioSession sharedInstance];\n",
     "            AVAudioSession *session = [AVAudioSession sharedInstance];\n"
     "            madeira_bridge_checkpoint(session ? \"WINE_THREAD_AUDIO_SESSION_OK\" : \"WINE_THREAD_AUDIO_SESSION_NULL\");\n"),
    ("            [session setCategory:AVAudioSessionCategoryPlayback error:&aerr];\n",
     "            [session setCategory:AVAudioSessionCategoryPlayback error:&aerr];\n"
     "            madeira_bridge_checkpoint(aerr ? \"WINE_THREAD_AUDIO_CATEGORY_ERROR\" : \"WINE_THREAD_AUDIO_CATEGORY_OK\");\n"),
    ("            [session setActive:YES error:&aerr];\n",
     "            [session setActive:YES error:&aerr];\n"
     "            madeira_bridge_checkpoint(aerr ? \"WINE_THREAD_AUDIO_ACTIVE_ERROR\" : \"WINE_THREAD_AUDIO_ACTIVE_OK\");\n"),
    ("        setenv(\"SteamAppId\",  \"356400\", 1);\n",
     "        setenv(\"SteamAppId\",  \"356400\", 1);\n"
     "        madeira_bridge_checkpoint(\"WINE_THREAD_STEAM_ENV_SET\");\n"),
    ("            int64_t jit_off = fex_get_jit_write_offset();\n",
     "            madeira_bridge_checkpoint(\"WINE_THREAD_JIT_OFFSET_BEGIN\");\n"
     "            int64_t jit_off = fex_get_jit_write_offset();\n"
     "            madeira_bridge_checkpoint(jit_off ? \"WINE_THREAD_JIT_OFFSET_OK\" : \"WINE_THREAD_JIT_OFFSET_ZERO\");\n"),
    ("        LOG(\"WINEPREFIX=%{public}s\", g_prefix_path);\n",
     "        madeira_bridge_checkpoint(\"WINE_THREAD_PREFIX_LOG_BEGIN\");\n"
     "        LOG(\"WINEPREFIX=%{public}s\", g_prefix_path);\n"
     "        madeira_bridge_checkpoint(\"WINE_THREAD_PREFIX_LOG_RETURNED\");\n"),
    ("            wine_log_set_file(logPath.UTF8String);\n",
     "            madeira_bridge_checkpoint(\"WINE_THREAD_LOG_FILE_BEGIN\");\n"
     "            wine_log_set_file(logPath.UTF8String);\n"
     "            madeira_bridge_checkpoint(\"WINE_THREAD_LOG_FILE_SET\");\n"),
    ("            { extern void winios_freeze_watch_start(void); winios_freeze_watch_start(); }\n",
     "            { extern void winios_freeze_watch_start(void); winios_freeze_watch_start(); }\n"
     "            madeira_bridge_checkpoint(\"WINE_THREAD_FREEZE_WATCH_STARTED\");\n"),
    ("            setenv(\"MADEIRA_DOCS_DIR\", docs.UTF8String, 1);\n",
     "            setenv(\"MADEIRA_DOCS_DIR\", docs.UTF8String, 1);\n"
     "            madeira_bridge_checkpoint(\"WINE_THREAD_DOCS_DIR_SET\");\n"),
    ("            NSString *caPath = [[NSBundle mainBundle] pathForResource:@\"cacert\" ofType:@\"pem\"];\n",
     "            madeira_bridge_checkpoint(\"WINE_THREAD_CA_LOOKUP_BEGIN\");\n"
     "            NSString *caPath = [[NSBundle mainBundle] pathForResource:@\"cacert\" ofType:@\"pem\"];\n"
     "            madeira_bridge_checkpoint(caPath ? \"WINE_THREAD_CA_LOOKUP_OK\" : \"WINE_THREAD_CA_LOOKUP_MISSING\");\n"),
    ("            NSString *logPath2 = [docs stringByAppendingPathComponent:@\"madeira-log.txt\"];\n",
     "            madeira_bridge_checkpoint(\"WINE_THREAD_STDIO_REDIRECT_BEGIN\");\n"
     "            NSString *logPath2 = [docs stringByAppendingPathComponent:@\"madeira-log.txt\"];\n"),
    ("                close(logfd);\n",
     "                close(logfd);\n"
     "                madeira_bridge_checkpoint(\"WINE_THREAD_STDIO_REDIRECT_OK\");\n"),
    ("        const char *madeira_exe = getenv(\"MADEIRA_EXE\");\n",
     "        madeira_bridge_checkpoint(\"WINE_THREAD_EXE_SELECT_BEGIN\");\n"
     "        const char *madeira_exe = getenv(\"MADEIRA_EXE\");\n"),
    ("        const char *bundle_subdir = use_arm64ec ? \"arm64ec-windows\" : \"aarch64-windows\";\n",
     "        const char *bundle_subdir = use_arm64ec ? \"arm64ec-windows\" : \"aarch64-windows\";\n"
     "        madeira_bridge_checkpoint(\"WINE_THREAD_EXE_SELECT_OK\");\n"),
    ("        // Ensure Wine prefix has system32 directory with DLLs from bundle\n",
     "        madeira_bridge_checkpoint(\"WINE_THREAD_DLL_FARM_BEGIN\");\n"
     "        // Ensure Wine prefix has system32 directory with DLLs from bundle\n"),
    ("        // Build the launch path for Wine's PE loader.\n",
     "        madeira_bridge_checkpoint(\"WINE_THREAD_DLL_FARM_RETURNED\");\n"
     "        // Build the launch path for Wine's PE loader.\n"),
    ("        char *argv[24];\n",
     "        madeira_bridge_checkpoint(\"WINE_THREAD_ARGV_BUILD_BEGIN\");\n"
     "        char *argv[24];\n"),
    ("        // Record this thread so wine_ios_exit knows where to longjmp\n",
     "        madeira_bridge_checkpoint(\"WINE_THREAD_PRE_WINE_MAIN_SETUP_OK\");\n"
     "        // Record this thread so wine_ios_exit knows where to longjmp\n"),
    ("        LOG(\"Calling __wine_main...\");\n",
     "        madeira_bridge_checkpoint(\"WINE_THREAD_WINE_MAIN_LOG_BEGIN\");\n"
     "        LOG(\"Calling __wine_main...\");\n"
     "        madeira_bridge_checkpoint(\"WINE_THREAD_WINE_MAIN_LOG_RETURNED\");\n"),
    ("        if (setjmp(wine_ios_exit_jmpbuf) == 0) {\n",
     "        madeira_bridge_checkpoint(\"WINE_THREAD_SETJMP_BEGIN\");\n"
     "        if (setjmp(wine_ios_exit_jmpbuf) == 0) {\n"
     "            madeira_bridge_checkpoint(\"WINE_THREAD_SETJMP_INITIAL\");\n"
     "            madeira_bridge_checkpoint(\"WINE_THREAD_WINE_MAIN_ENTER\");\n"),
    ("            __wine_main(argc, argv);\n",
     "            __wine_main(argc, argv);\n"
     "            madeira_bridge_checkpoint(\"WINE_THREAD_WINE_MAIN_RETURNED\");\n"),
]
for old, new in startup_replacements:
    if old not in s:
        raise SystemExit(f"Wine startup checkpoint anchor missing: {old!r}")
    s = s.replace(old, new, 1)

for include in ("#include <fcntl.h>", "#include <unistd.h>", "#include <signal.h>", "#include <mach/mach.h>"):
    if include not in s:
        s = include + "\n" + s
wine_bridge.write_text(s, encoding="utf-8")
print("Installed v55 checkpoints through CA/log setup, DLL farms, argv, setjmp, and __wine_main entry")
