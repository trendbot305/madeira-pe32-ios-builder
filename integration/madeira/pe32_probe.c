#include <windows.h>
#include <stdint.h>

/*
 * Deliberately CRT-free PE32 fixture. This keeps the iPhone WoW64 test focused
 * on Wine/FEX + kernel32 instead of depending on a 32-bit MSVCRT.
 *
 * The guest-shadow gate now checks the important low-VA behaviour:
 *  - a fixed 32-bit guest allocation below 4GiB,
 *  - ordinary translated read/write,
 *  - a LOCK-style atomic through InterlockedIncrement,
 *  - then the normal Win32 calls.
 */
static const char ok_marker[] = "MADEIRA_PE32_OK\r\n";
static const char shadow_marker[] = "MADEIRA_PE32_SHADOW_OK\r\n";
static const char shadow_fail[] = "MADEIRA_PE32_SHADOW_FAIL\r\n";

static void write_console(const char *msg, DWORD len) {
    DWORD written = 0;
    HANDLE out = GetStdHandle(STD_OUTPUT_HANDLE);
    if (out && out != INVALID_HANDLE_VALUE) {
        WriteFile(out, msg, len, &written, NULL);
    }
}

__attribute__((noreturn))
void __stdcall pe32_entry(void) {
    volatile LONG *fixed = (volatile LONG *)VirtualAlloc(
        (LPVOID)0x10000000u,
        0x10000u,
        MEM_RESERVE | MEM_COMMIT,
        PAGE_READWRITE);

    if (!fixed) {
        write_console(shadow_fail, (DWORD)(sizeof(shadow_fail) - 1));
        OutputDebugStringA("MADEIRA_PE32_SHADOW_ALLOC_FAIL");
        ExitProcess(10u);
    }

    fixed[0] = 0x11223344;
    fixed[1] = 41;
    InterlockedIncrement((volatile LONG *)&fixed[1]);

    if (fixed[0] != 0x11223344 || fixed[1] != 42) {
        write_console(shadow_fail, (DWORD)(sizeof(shadow_fail) - 1));
        OutputDebugStringA("MADEIRA_PE32_SHADOW_RW_OR_ATOMIC_FAIL");
        ExitProcess(11u);
    }

    write_console(shadow_marker, (DWORD)(sizeof(shadow_marker) - 1));
    OutputDebugStringA("MADEIRA_PE32_SHADOW_OK");

    write_console(ok_marker, (DWORD)(sizeof(ok_marker) - 1));
    OutputDebugStringA("MADEIRA_PE32_OK");

    volatile DWORD pid = GetCurrentProcessId();
    volatile DWORD tick = GetTickCount();
    ExitProcess(((pid ^ tick) == 0xFFFFFFFFu) ? 1u : 42u);
}
