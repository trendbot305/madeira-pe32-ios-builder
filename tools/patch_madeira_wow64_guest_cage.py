#!/usr/bin/env python3
from pathlib import Path
import sys

root = Path(sys.argv[1] if len(sys.argv) > 1 else "external/Madeira")
vpath = root / "build/ntdll-unix/virtual_ios.c"
text = vpath.read_text(encoding="utf-8")

def repl(old, new, label, count=1):
    global text
    if new in text:
        return
    if old not in text:
        raise SystemExit(f"{label}: anchor not found")
    text = text.replace(old, new, count)

# High backing cage state. mach_vm_map's alignment mask guarantees the host
# base is a multiple of 2^32, so low32(host) == x86 guest pointer.
anchor = """ULONG_PTR user_space_wow_limit = 0;
struct _KUSER_SHARED_DATA *user_shared_data = (void *)0x7ffe0000;
"""
insert = """ULONG_PTR user_space_wow_limit = 0;
struct _KUSER_SHARED_DATA *user_shared_data = (void *)0x7ffe0000;

#ifdef WINE_IOS
#define IOS_WOW64_GUEST_SIZE 0x100000000ULL
static mach_vm_address_t ios_wow64_cage_base;
static void mmap_add_reserved_area( void *addr, SIZE_T size );

unsigned long long ios_wow64_guest_bias(void)
{
    return (unsigned long long)ios_wow64_cage_base;
}

void *ios_wow64_guest_to_host( ULONG_PTR guest )
{
    if (!guest) return NULL;
    if (!ios_wow64_cage_base || guest >= IOS_WOW64_GUEST_SIZE) return (void *)(uintptr_t)guest;
    return (void *)(uintptr_t)(ios_wow64_cage_base + (uint32_t)guest);
}

ULONG_PTR ios_wow64_host_to_guest( const void *host )
{
    uintptr_t value = (uintptr_t)host;
    if (ios_wow64_cage_base && value >= ios_wow64_cage_base &&
        value <= ios_wow64_cage_base + IOS_WOW64_GUEST_SIZE)
        return value - ios_wow64_cage_base;
    return value;
}

ULONG_PTR ios_wow64_guest_limit_to_host( ULONG_PTR limit )
{
    if (!ios_wow64_cage_base || !limit || limit > IOS_WOW64_GUEST_SIZE) return limit;
    return (ULONG_PTR)ios_wow64_cage_base + limit;
}

static int ios_wow64_reserve_cage(void)
{
    mach_vm_address_t address = 0;
    kern_return_t kr = mach_vm_map( mach_task_self(), &address,
                                    (mach_vm_size_t)IOS_WOW64_GUEST_SIZE,
                                    (mach_vm_offset_t)(IOS_WOW64_GUEST_SIZE - 1),
                                    VM_FLAGS_ANYWHERE, MEMORY_OBJECT_NULL, 0, FALSE,
                                    VM_PROT_NONE, VM_PROT_ALL, VM_INHERIT_DEFAULT );
    if (kr != KERN_SUCCESS)
    {
        dprintf( 2, "[wow64-cage] reserve failed kr=%d size=0x%llx\n",
                 (int)kr, (unsigned long long)IOS_WOW64_GUEST_SIZE );
        return 0;
    }
    if ((address & (IOS_WOW64_GUEST_SIZE - 1)) || address < IOS_WOW64_GUEST_SIZE)
    {
        dprintf( 2, "[wow64-cage] invalid base=0x%llx; releasing\n",
                 (unsigned long long)address );
        mach_vm_deallocate( mach_task_self(), address, (mach_vm_size_t)IOS_WOW64_GUEST_SIZE );
        return 0;
    }

    ios_wow64_cage_base = address;
    mmap_add_reserved_area( (void *)(uintptr_t)address, (SIZE_T)IOS_WOW64_GUEST_SIZE );
    dprintf( 2, "[wow64-cage] READY host=0x%llx guest=0x0..0xffffffff aligned4g=1\n",
             (unsigned long long)address );
    return 1;
}
#endif
"""
repl(anchor, insert, "cage globals")

# Reserve only after Wine's metadata/free-range structures exist, preventing
# alloc_virtual_heap from consuming the new guest cage itself.
anchor = """    /* task #35: reserve the one 8GB-aligned stretch CEF's cppgc cage needs
     * before top-down placement can put furniture in it — see IOS_CAGE_BASE. */
"""
insert = """#ifdef WINE_IOS
    if (!ios_wow64_reserve_cage())
        ERR( "iOS WoW64: could not reserve aligned 4GiB guest backing cage\n" );
#endif

""" + anchor
repl(anchor, insert, "virtual_init cage reservation")

# Put KUSER_SHARED_DATA and the first TEB/PEB block at deterministic logical
# addresses inside the cage. Their low 32 bits are exactly what PE32 sees.
old = """#ifdef WINE_IOS
    /* iOS arm64 has mandatory 4GB __PAGEZERO — can't map at 0x7ffe0000 */
    user_shared_data = NULL;
#endif
    status = NtAllocateVirtualMemory( NtCurrentProcess(), (void **)&user_shared_data, 0, &data_size,
                                      MEM_RESERVE | MEM_COMMIT, PAGE_READONLY );"""
new = """#ifdef WINE_IOS
    if (ios_wow64_cage_base)
        user_shared_data = (struct _KUSER_SHARED_DATA *)(uintptr_t)(ios_wow64_cage_base + 0x7ffe0000ULL);
    else
        user_shared_data = NULL;
#endif
    status = NtAllocateVirtualMemory( NtCurrentProcess(), (void **)&user_shared_data, 0, &data_size,
                                      MEM_RESERVE | MEM_COMMIT, PAGE_READONLY );"""
repl(old, new, "KUSER cage mapping")

old = """#ifdef WINE_IOS
    /* iOS 4GB __PAGEZERO blocks all addresses below 4GB — skip below-2GB constraint */
    NtAllocateVirtualMemory( NtCurrentProcess(), &teb_block, 0, &total,
                             MEM_RESERVE | MEM_TOP_DOWN, PAGE_READWRITE );
#else"""
new = """#ifdef WINE_IOS
    if (ios_wow64_cage_base)
        teb_block = (void *)(uintptr_t)(ios_wow64_cage_base + 0x70000000ULL);
    else
        teb_block = NULL;
    status = NtAllocateVirtualMemory( NtCurrentProcess(), &teb_block, 0, &total,
                                      MEM_RESERVE | MEM_TOP_DOWN, PAGE_READWRITE );
    if (status)
    {
        ERR( "iOS WoW64: first TEB block allocation failed: %08x base=%p\n", status, teb_block );
        exit(1);
    }
    dprintf( 2, "[wow64-cage] first-teb-block host=%p guest=0x%lx size=0x%lx\n",
             teb_block, (unsigned long)ios_wow64_host_to_guest( teb_block ), (unsigned long)total );
#else"""
repl(old, new, "TEB cage mapping")

# Once Wine knows this is a WoW64 process, keep its user-space ceiling inside
# the high cage rather than the physically unavailable low 2/4GiB host range.
old = """        else user_space_wow_limit = ((main_image_info.ImageCharacteristics & IMAGE_FILE_LARGE_ADDRESS_AWARE) ? limit_4g : limit_2g) - 1;"""
new = """        else
        {
            ULONG_PTR logical_limit =
                (main_image_info.ImageCharacteristics & IMAGE_FILE_LARGE_ADDRESS_AWARE) ? limit_4g : limit_2g;
#ifdef WINE_IOS
            user_space_wow_limit = ios_wow64_cage_base
                ? (ULONG_PTR)ios_wow64_cage_base + logical_limit - 1
                : logical_limit - 1;
#else
            user_space_wow_limit = logical_limit - 1;
#endif
        }"""
repl(old, new, "WoW64 user limit")

# Preferred PE32 image bases are logical Windows VAs. Map them at bias+VA while
# preserving image_info->base for relocation arithmetic.
old = """    if (image_info->map_addr)
    {
        base = wine_server_get_ptr( image_info->map_addr );
        if ((ULONG_PTR)base != image_info->map_addr) base = NULL;
    }
    else
    {
        base = wine_server_get_ptr( image_info->base );
        if ((ULONG_PTR)base != image_info->base) base = NULL;
    }"""
new = """    if (image_info->map_addr)
    {
        base = wine_server_get_ptr( image_info->map_addr );
        if ((ULONG_PTR)base != image_info->map_addr) base = NULL;
    }
    else
    {
        base = wine_server_get_ptr( image_info->base );
        if ((ULONG_PTR)base != image_info->base) base = NULL;
    }
#ifdef WINE_IOS
    if (ios_wow64_cage_base && image_info->base < limit_4g)
    {
        if (base && (ULONG_PTR)base < limit_4g)
            base = ios_wow64_guest_to_host( (ULONG_PTR)base );
        if (limit_low < (ULONG_PTR)ios_wow64_cage_base)
            limit_low = (ULONG_PTR)ios_wow64_cage_base + max( limit_low, (ULONG_PTR)0x10000 );
        if (!limit_high || limit_high < (ULONG_PTR)ios_wow64_cage_base)
        {
            ULONG_PTR logical_high = limit_high ? min( limit_high, (ULONG_PTR)limit_4g - 1 )
                                                : (ULONG_PTR)limit_4g - 1;
            limit_high = (ULONG_PTR)ios_wow64_cage_base + logical_high;
        }
    }
#endif"""
repl(old, new, "PE32 image cage translation")

# Standard WoW64 VirtualAlloc(NULL, ...) calls arrive through wow64.dll with
# in_wow64_call() set. Give the allocator both a cage floor and ceiling.
anchor = """    if (!*ret)
        limit = get_zero_bits_limit( zero_bits );
    else
        limit = 0;
"""
insert = """    if (!*ret)
        limit = get_zero_bits_limit( zero_bits );
    else
        limit = 0;

    ULONG_PTR ios_wow64_limit_low = 0;
#ifdef WINE_IOS
    if (ios_wow64_cage_base && in_wow64_call())
    {
        ios_wow64_limit_low = (ULONG_PTR)ios_wow64_cage_base + 0x10000;
        if (!limit || limit < (ULONG_PTR)ios_wow64_cage_base)
        {
            ULONG_PTR logical_limit = limit ? min( limit, (ULONG_PTR)limit_4g - 1 )
                                            : (ULONG_PTR)limit_4g - 1;
            limit = (ULONG_PTR)ios_wow64_cage_base + logical_limit;
        }
        if (*ret && (ULONG_PTR)*ret < limit_4g)
            *ret = ios_wow64_guest_to_host( (ULONG_PTR)*ret );
    }
#endif
"""
repl(anchor, insert, "NtAllocate WoW64 cage limits")
# Restrict only the standard NtAllocateVirtualMemory calls in this function.
# These exact calls are stable in the pinned Madeira source.
text = text.replace(
    "allocate_virtual_memory( ret, size_ptr, type, protect, 0, limit, 0, 0 )",
    "allocate_virtual_memory( ret, size_ptr, type, protect, ios_wow64_limit_low, limit, 0, 0 )",
)

# Provide a native helper for pre-WoW64 bootstrap data (environment/process
# parameters) which is created before in_wow64_call() can be true.
anchor = """/***********************************************************************
 *             NtAllocateVirtualMemory   (NTDLL.@)
 *             ZwAllocateVirtualMemory   (NTDLL.@)
 */
"""
helper = """#ifdef WINE_IOS
NTSTATUS ios_wow64_allocate_bootstrap( void **ret, SIZE_T *size, ULONG type, ULONG protect,
                                       ULONG_PTR guest_low, ULONG_PTR guest_high )
{
    if (!ios_wow64_cage_base) return STATUS_NO_MEMORY;
    return allocate_virtual_memory( ret, size, type, protect,
                                    (ULONG_PTR)ios_wow64_cage_base + guest_low,
                                    (ULONG_PTR)ios_wow64_cage_base + guest_high,
                                    0, 0 );
}
#endif


""" + anchor
repl(anchor, helper, "bootstrap allocator")

vpath.write_text(text, encoding="utf-8")

# Native ntdll files dereference several 32-bit WOW64 pointer fields directly.
# Convert those sites to the cage-aware helper; PtrToUlong remains correct
# because the cage base is 4GiB aligned.
for rel in (
    "build/ntdll-unix/process_ios.c",
    "build/ntdll-unix/server_ios.c",
    "build/ntdll-unix/thread_ios.c",
):
    p = root / rel
    s = p.read_text(encoding="utf-8")
    if "ios_wow64_guest_to_host" not in s:
        include_anchor = '#include "unix_private.h"\n'
        if include_anchor not in s:
            raise SystemExit(f"{rel}: unix_private include anchor not found")
        s = s.replace(include_anchor, include_anchor + "extern void *ios_wow64_guest_to_host( ULONG_PTR guest );\n", 1)
    s = s.replace("ULongToPtr(", "ios_wow64_guest_to_host(")
    p.write_text(s, encoding="utf-8")

# virtual_ios.c has its own WOW_TEB dereferences; use the local helper.
s = vpath.read_text(encoding="utf-8")
for old, new in (
    ("ULongToPtr( wow_teb->DeallocationStack )", "ios_wow64_guest_to_host( wow_teb->DeallocationStack )"),
    ("ULongToPtr( ptr )", "ios_wow64_guest_to_host( ptr )"),
    ("ULongToPtr( wow_teb->TlsExpansionSlots )", "ios_wow64_guest_to_host( wow_teb->TlsExpansionSlots )"),
    ("ULongToPtr( wow_teb->Tib.StackLimit )", "ios_wow64_guest_to_host( wow_teb->Tib.StackLimit )"),
    ("ULongToPtr( wow_teb->Tib.StackBase )", "ios_wow64_guest_to_host( wow_teb->Tib.StackBase )"),
):
    s = s.replace(old, new)
vpath.write_text(s, encoding="utf-8")

# build_wow64_parameters executes before the child is fully marked as WoW64.
# Allocate its 32-bit-visible block explicitly inside the cage.
env = root / "build/ntdll-unix/env_ios.c"
s = env.read_text(encoding="utf-8")
if "ios_wow64_allocate_bootstrap" not in s:
    anchor = '#include "unix_private.h"\n'
    if anchor not in s:
        raise SystemExit("env_ios.c: unix_private include anchor not found")
    s = s.replace(anchor, anchor + "extern NTSTATUS ios_wow64_allocate_bootstrap( void **ret, SIZE_T *size, ULONG type, ULONG protect, ULONG_PTR guest_low, ULONG_PTR guest_high );\n", 1)
old = """    status = NtAllocateVirtualMemory( NtCurrentProcess(), (void **)&wow64_params, limit_2g - 1, &size,
                                      MEM_COMMIT, PAGE_READWRITE );"""
new = """#ifdef WINE_IOS
    status = ios_wow64_allocate_bootstrap( (void **)&wow64_params, &size,
                                           MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE,
                                           0x10000, limit_2g - 1 );
#else
    status = NtAllocateVirtualMemory( NtCurrentProcess(), (void **)&wow64_params, limit_2g - 1, &size,
                                      MEM_COMMIT, PAGE_READWRITE );
#endif"""
if new not in s:
    if old not in s:
        raise SystemExit("env_ios.c: wow64 parameter allocation anchor not found")
    s = s.replace(old, new, 1)
env.write_text(s, encoding="utf-8")

# Validate the critical properties rather than allowing a partial patch.
checks = {
    vpath: [
        "IOS_WOW64_GUEST_SIZE", "mach_vm_map", "aligned4g=1",
        "ios_wow64_allocate_bootstrap", "ios_wow64_limit_low",
        "user_shared_data = (struct _KUSER_SHARED_DATA *)(uintptr_t)(ios_wow64_cage_base + 0x7ffe0000ULL)",
    ],
    env: ["ios_wow64_allocate_bootstrap", "PtrToUlong( wow64_params )"],
}
for path, needles in checks.items():
    data = path.read_text(encoding="utf-8")
    for needle in needles:
        if needle not in data:
            raise SystemExit(f"validation failed: {needle!r} missing from {path}")

print("Installed Madeira/Wine 4GiB-aligned WoW64 high backing cage")
