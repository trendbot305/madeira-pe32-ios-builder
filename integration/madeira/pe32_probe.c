#include <windows.h>
#include <stdint.h>

/*
 * Deliberately CRT-free PE32 fixture.  This keeps the first iPhone WoW64 test
 * focused on Wine/FEX + kernel32 instead of depending on a 32-bit MSVCRT.
 */
static const char marker[] = "MADEIRA_PE32_OK\r\n";

__attribute__((noreturn))
void __stdcall pe32_entry(void) {
    DWORD written = 0;
    HANDLE out = GetStdHandle(STD_OUTPUT_HANDLE);

    if (out && out != INVALID_HANDLE_VALUE) {
        WriteFile(out, marker, (DWORD)(sizeof(marker) - 1), &written, NULL);
    }
    OutputDebugStringA("MADEIRA_PE32_OK");

    /* Exercise ordinary Win32 calls before returning the probe result. */
    volatile DWORD pid = GetCurrentProcessId();
    volatile DWORD tick = GetTickCount();
    ExitProcess(((pid ^ tick) == 0xFFFFFFFFu) ? 1u : 42u);
}
