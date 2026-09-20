#!/usr/bin/env python3
"""Patch Madeira's iOS Wine ntdll for a high-address PE32 guest shadow.

The iOS ARM64 task cannot map the low 4GiB.  For the dedicated PE32/WoW64
test path we reserve one 4GiB-aligned host window, keep guest-visible
addresses 32-bit, and translate low-address Wine allocations into that
window.  The patch is deliberately gated by MADEIRA_PE32_SHADOW=1 so the
existing ARM64EC/x64 path is untouched.
"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("external/Madeira")
VIRTUAL = ROOT / "build/ntdll-unix/virtual_ios.c"
ENV = ROOT / "build/ntdll-unix/env_ios.c"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one anchor, found {count}")
    return text.replace(old, new, 1)


def patch_virtual() -> None:
    text = VIRTUAL.read_text(encoding="utf-8")

    global_anchor = """ULONG_PTR user_space_wow_limit = 0;
struct _KUSER_SHARED_DATA *user_shared_data = (void *)0x7ffe0000;
"""
    global_block = global_anchor + r'''
#ifdef WINE_IOS
/*
 * PE32 guest shadow.
 *
 * iOS/arm64 reserves the low 4GiB for __PAGEZERO.  Wine and FEX's WoW64
 * frontend still need a normal 0..4GiB x86 address space, so keep those
 * addresses architectural and back them with a 4GiB-aligned high host
 * window.  A 4GiB alignment is important: truncating a host pointer to
 * ULONG then naturally yields its guest offset, matching Wine's existing
 * 32-bit structure layout.
 */
#define IOS_PE32_GUEST_SPACE_SIZE 0x100000000ULL
static ULONG_PTR ios_pe32_guest_bias;
static SIZE_T ios_pe32_guest_size;
static int ios_pe32_shadow_active;

ULONG_PTR ios_pe32_guest_address_bias(void)
{
    return ios_pe32_shadow_active ? ios_pe32_guest_bias : 0;
}

SIZE_T ios_pe32_guest_address_size(void)
{
    return ios_pe32_shadow_active ? ios_pe32_guest_size : 0;
}

void *ios_pe32_guest_to_host_ptr( ULONG_PTR guest )
{
    if (ios_pe32_shadow_active && guest < ios_pe32_guest_size)
        return (void *)(ios_pe32_guest_bias + guest);
    return (void *)guest;
}

ULONG_PTR ios_pe32_host_to_guest_ptr( const void *host )
{
    ULONG_PTR value = (ULONG_PTR)host;
    if (ios_pe32_shadow_active && value >= ios_pe32_guest_bias &&
        value - ios_pe32_guest_bias < ios_pe32_guest_size)
        return value - ios_pe32_guest_bias;
    return value;
}

static int ios_pe32_shadow_requested(void)
{
    const char *opt = getenv("MADEIRA_PE32_SHADOW");
    return opt && opt[0] == '1' && opt[1] == 0;
}
#endif
'''
    if "IOS_PE32_GUEST_SPACE_SIZE" not in text:
        text = replace_once(text, global_anchor, global_block, "guest-shadow globals")

    reserve_anchor = """/***********************************************************************
 *           virtual_init
 */
void virtual_init(void)
"""
    reserve_block = r'''
#ifdef WINE_IOS
/*
 * Reserve the complete guest32 backing before Wine starts placing PE images,
 * TEBs, stacks or process parameters.  mach_vm_map's mask requests 4GiB
 * alignment while VM_FLAGS_ANYWHERE lets XNU choose a legal high address.
 * The reservation is added to Wine's reserved-area list so ordinary Wine
 * allocations avoid it and guest32 map_view calls can replace subranges with
 * their real protections/backing.
 */
static int ios_pe32_reserve_guest_shadow(void)
{
    mach_vm_address_t base = 0;
    const mach_vm_size_t size = (mach_vm_size_t)IOS_PE32_GUEST_SPACE_SIZE;
    const mach_vm_offset_t alignment_mask = (mach_vm_offset_t)IOS_PE32_GUEST_SPACE_SIZE - 1;
    kern_return_t kr;
    char value[32];

    if (!ios_pe32_shadow_requested()) return 0;
    if (ios_pe32_shadow_active) return 1;

    kr = mach_vm_map( mach_task_self(), &base, size, alignment_mask, VM_FLAGS_ANYWHERE,
                      MEMORY_OBJECT_NULL, 0, FALSE, VM_PROT_NONE, VM_PROT_ALL,
                      VM_INHERIT_COPY );
    if (kr != KERN_SUCCESS)
    {
        dprintf( 2, "[pe32-shadow] reserve 4GiB failed kr=%d\n", (int)kr );
        setenv( "WINE_IOS_PE32_SHADOW_STATUS", "reserve-failed", 1 );
        return 0;
    }

    if ((base & alignment_mask) ||
        base < IOS_PE32_GUEST_SPACE_SIZE ||
        (host_addr_space_limit && base + size > (mach_vm_address_t)(uintptr_t)host_addr_space_limit))
    {
        dprintf( 2, "[pe32-shadow] rejected base=0x%llx size=0x%llx host_limit=%p\n",
                 (unsigned long long)base, (unsigned long long)size, host_addr_space_limit );
        mach_vm_deallocate( mach_task_self(), base, size );
        setenv( "WINE_IOS_PE32_SHADOW_STATUS", "geometry-rejected", 1 );
        return 0;
    }

    ios_pe32_guest_bias = (ULONG_PTR)base;
    ios_pe32_guest_size = (SIZE_T)size;
    ios_pe32_shadow_active = 1;
    mmap_add_reserved_area( (void *)(uintptr_t)base, (SIZE_T)size );

    snprintf( value, sizeof(value), "0x%llx", (unsigned long long)base );
    setenv( "WINE_IOS_PE32_GUEST_BIAS", value, 1 );
    snprintf( value, sizeof(value), "0x%llx", (unsigned long long)size );
    setenv( "WINE_IOS_PE32_GUEST_SIZE", value, 1 );
    setenv( "WINE_IOS_PE32_SHADOW_STATUS", "ready", 1 );

    dprintf( 2, "[pe32-shadow] READY bias=0x%llx size=0x%llx guest=0..0xffffffff\n",
             (unsigned long long)base, (unsigned long long)size );
    return 1;
}
#endif

''' + reserve_anchor
    if "static int ios_pe32_reserve_guest_shadow(void)" not in text:
        text = replace_once(text, reserve_anchor, reserve_block, "guest-shadow reservation")

    mmap_init_anchor = """    mmap_init( preload_info ? *preload_info : NULL );

    if ((preload = getenv("WINEPRELOADRESERVE")))
"""
    mmap_init_new = """    mmap_init( preload_info ? *preload_info : NULL );

#ifdef WINE_IOS
    /*
     * This must happen before the first TEB/PEB and before any PE32 image can
     * be placed.  Failure leaves the normal x64 path intact; the PE32 test
     * will emit an explicit shadow-status diagnostic instead of corrupting VA.
     */
    ios_pe32_reserve_guest_shadow();
#endif

    if ((preload = getenv("WINEPRELOADRESERVE")))
"""
    if "ios_pe32_reserve_guest_shadow();" not in text:
        text = replace_once(text, mmap_init_anchor, mmap_init_new, "virtual_init shadow call")

    map_view_anchor = """    if (!align_mask) align_mask = granularity_mask;
    assert( align_mask >= host_page_mask );

    if (alloc_type & MEM_REPLACE_PLACEHOLDER)
"""
    map_view_new = """    if (!align_mask) align_mask = granularity_mask;
    assert( align_mask >= host_page_mask );

#ifdef WINE_IOS
    /*
     * Translate only an explicitly 32-bit-constrained mapping.  Native x64
     * allocations have no <=4GiB upper bound and therefore remain untouched.
     * Fixed guest addresses are translated as well, which covers preferred
     * PE32 image bases and subsequent commit operations that enter map_view.
     */
    if (ios_pe32_shadow_active)
    {
        if (base && (ULONG_PTR)base < ios_pe32_guest_size)
            base = (void *)(ios_pe32_guest_bias + (ULONG_PTR)base);

        if (limit_high && limit_high <= ios_pe32_guest_size)
        {
            limit_low = ios_pe32_guest_bias + limit_low;
            limit_high = ios_pe32_guest_bias + limit_high;
        }
    }
#endif

    if (alloc_type & MEM_REPLACE_PLACEHOLDER)
"""
    if "Translate only an explicitly 32-bit-constrained mapping" not in text:
        text = replace_once(text, map_view_anchor, map_view_new, "map_view translation")

    image_anchor = """    limit_low = max( limit_low, (ULONG_PTR)address_space_start );  /* make sure the DOS area remains free */
    /* task #35 furniture ceiling: images pack below it (inclusive limit).
"""
    image_new = """#ifdef WINE_IOS
    if (ios_pe32_shadow_active && image_info->base < limit_4g)
    {
        /*
         * Keep PE32 addresses architectural here. map_view performs the
         * guest->host biasing.  Clamping to a logical 32-bit range prevents
         * the final fallback from escaping into unrelated native VA.
         */
        limit_low = max( limit_low, (ULONG_PTR)0x10000 );
        if (!limit_high || limit_high >= ios_pe32_guest_size)
            limit_high = ios_pe32_guest_size - 1;
    }
    else
#endif
        limit_low = max( limit_low, (ULONG_PTR)address_space_start );  /* make sure the DOS area remains free */
    /* task #35 furniture ceiling: images pack below it (inclusive limit).
"""
    if "Keep PE32 addresses architectural here" not in text:
        text = replace_once(text, image_anchor, image_new, "map_image_view translation")

    usd_anchor = """#ifdef WINE_IOS
    /* iOS arm64 has mandatory 4GB __PAGEZERO — can't map at 0x7ffe0000 */
    user_shared_data = NULL;
#endif
"""
    usd_new = """#ifdef WINE_IOS
    /*
     * In guest-shadow mode the architectural shared-user-data address is
     * backed at bias+0x7ffe0000. Otherwise keep Madeira's existing high pick.
     */
    if (ios_pe32_shadow_active)
        user_shared_data = (void *)(ios_pe32_guest_bias + 0x7ffe0000ULL);
    else
        user_shared_data = NULL;
#endif
"""
    if "architectural shared-user-data address" not in text:
        text = replace_once(text, usd_anchor, usd_new, "shared user data placement")

    teb_anchor = """#ifdef WINE_IOS
    /* iOS 4GB __PAGEZERO blocks all addresses below 4GB — skip below-2GB constraint */
    NtAllocateVirtualMemory( NtCurrentProcess(), &teb_block, 0, &total,
                             MEM_RESERVE | MEM_TOP_DOWN, PAGE_READWRITE );
#else
"""
    teb_new = """#ifdef WINE_IOS
    /*
     * A WoW64 TEB/PEB must live in the same biased 4GiB band as the rest of
     * guest32 memory so its truncated 32-bit address translates back to the
     * real host object.  The central map_view translator turns this logical
     * below-2GiB request into the high shadow range.
     */
    status = NtAllocateVirtualMemory( NtCurrentProcess(), &teb_block,
                                      ios_pe32_shadow_active ? limit_2g - 1 : 0, &total,
                                      MEM_RESERVE | MEM_TOP_DOWN, PAGE_READWRITE );
    if (status)
    {
        ERR( "wine: failed to reserve initial TEB/PEB block: %08x\\n", status );
        exit(1);
    }
#else
"""
    if "same biased 4GiB band" not in text:
        text = replace_once(text, teb_anchor, teb_new, "initial TEB/PEB placement")

    # Native ntdll occasionally dereferences 32-bit stack fields itself.
    # Translate only these known guest-address reads; PtrToUlong writes remain
    # valid because the shadow base is exactly 4GiB aligned.
    replacements = {
        "if (wow_teb && (ptr = ULongToPtr( wow_teb->DeallocationStack )))":
            "if (wow_teb && (ptr = ios_pe32_guest_to_host_ptr( wow_teb->DeallocationStack )))",
        "stack->start = ULongToPtr( wow_teb->DeallocationStack );":
            "stack->start = ios_pe32_guest_to_host_ptr( wow_teb->DeallocationStack );",
        "stack->limit = ULongToPtr( wow_teb->Tib.StackLimit );":
            "stack->limit = ios_pe32_guest_to_host_ptr( wow_teb->Tib.StackLimit );",
        "stack->end   = ULongToPtr( wow_teb->Tib.StackBase );":
            "stack->end   = ios_pe32_guest_to_host_ptr( wow_teb->Tib.StackBase );",
    }
    for old, new in replacements.items():
        if old in text:
            text = text.replace(old, new)
        elif new not in text:
            raise SystemExit(f"native guest-pointer anchor missing: {old}")

    VIRTUAL.write_text(text, encoding="utf-8")


def patch_env() -> None:
    text = ENV.read_text(encoding="utf-8")
    anchor = """    status = NtAllocateVirtualMemory( NtCurrentProcess(), (void **)&wow64_params, limit_2g - 1, &size,
                                      MEM_COMMIT, PAGE_READWRITE );
    assert( !status );
"""
    new = """    status = NtAllocateVirtualMemory( NtCurrentProcess(), (void **)&wow64_params, limit_2g - 1, &size,
                                      MEM_COMMIT, PAGE_READWRITE );
#ifdef WINE_IOS
    if (status)
    {
        const char *shadow = getenv("WINE_IOS_PE32_SHADOW_STATUS");
        dprintf( 2, "[pe32-shadow] build_wow64_parameters allocation failed status=%08x shadow=%s\\n",
                 status, shadow ? shadow : "off" );
    }
    else
    {
        extern ULONG_PTR ios_pe32_guest_address_bias(void);
        extern SIZE_T ios_pe32_guest_address_size(void);
        ULONG_PTR bias = ios_pe32_guest_address_bias();
        SIZE_T guest_size = ios_pe32_guest_address_size();
        ULONG_PTR host = (ULONG_PTR)wow64_params;
        if (bias && (host < bias || host - bias >= guest_size))
        {
            dprintf( 2, "[pe32-shadow] FATAL params host=%p escaped bias=%p size=%p\\n",
                     wow64_params, (void *)bias, (void *)guest_size );
            status = STATUS_NO_MEMORY;
        }
        else if (bias)
            dprintf( 2, "[pe32-shadow] wow64 params host=%p guest=0x%08lx\\n",
                     wow64_params, (unsigned long)(host - bias) );
    }
#endif
    assert( !status );
"""
    if "[pe32-shadow] wow64 params" not in text:
        text = replace_once(text, anchor, new, "wow64 parameter allocation diagnostics")
    ENV.write_text(text, encoding="utf-8")


patch_virtual()
patch_env()
print("PE32 high guest-shadow patch applied")
