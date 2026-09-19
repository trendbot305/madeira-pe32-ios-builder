#!/usr/bin/env python3
"""Dependency-free PE characterizer for legally owned Windows game binaries."""

from __future__ import annotations
import argparse, hashlib, json, struct
from pathlib import Path

MACHINES={0x14C:"x86",0x8664:"x86_64",0xAA64:"arm64"}
SUBSYSTEMS={2:"windows_gui",3:"windows_cui"}
GRAPHICS={"d3d9.dll","d3d10.dll","d3d10_1.dll","d3d11.dll","dxgi.dll","d3dcompiler_43.dll","d3dcompiler_47.dll","dinput8.dll","xinput1_3.dll"}
WINDOWS={"kernel32.dll","user32.dll","advapi32.dll","shell32.dll","ole32.dll","oleaut32.dll","gdi32.dll","winmm.dll","ws2_32.dll"}
MIDDLEWARE_HINTS=("bink","wwise","fmod","physx","apex","steam","vorbis","openal")

def u16(b,o): return struct.unpack_from("<H",b,o)[0]
def u32(b,o): return struct.unpack_from("<I",b,o)[0]

def cstr(b,o):
    end=b.find(b"\0",o)
    if end<0: end=len(b)
    return b[o:end].decode("ascii","replace")

def parse_pe(path:Path):
    data=path.read_bytes()
    if len(data)<0x100 or data[:2]!=b"MZ": raise ValueError("not an MZ/PE executable")
    pe=u32(data,0x3C)
    if data[pe:pe+4]!=b"PE\0\0": raise ValueError("PE signature not found")
    coff=pe+4
    machine=u16(data,coff); nsec=u16(data,coff+2); opt_size=u16(data,coff+16)
    opt=coff+20; magic=u16(data,opt)
    pe32=magic==0x10B; pe64=magic==0x20B
    if not (pe32 or pe64): raise ValueError(f"unsupported optional header 0x{magic:x}")
    entry=u32(data,opt+16); image_base=u32(data,opt+28) if pe32 else struct.unpack_from("<Q",data,opt+24)[0]
    subsystem=u16(data,opt+68)
    dd=opt+(96 if pe32 else 112)
    import_rva=u32(data,dd+8); import_size=u32(data,dd+12)
    sec_off=opt+opt_size; sections=[]
    for i in range(nsec):
        o=sec_off+i*40
        name=data[o:o+8].split(b"\0",1)[0].decode("ascii","replace")
        vs=u32(data,o+8); va=u32(data,o+12); raw_size=u32(data,o+16); raw=u32(data,o+20)
        sections.append({"name":name,"virtual_address":va,"virtual_size":vs,"raw_offset":raw,"raw_size":raw_size})
    def rva_off(rva):
        for s in sections:
            span=max(s["virtual_size"],s["raw_size"])
            if s["virtual_address"]<=rva<s["virtual_address"]+span:
                return s["raw_offset"]+(rva-s["virtual_address"])
        if rva < len(data): return rva
        raise ValueError(f"RVA 0x{rva:x} not mapped")
    imports=[]
    if import_rva:
        o=rva_off(import_rva)
        ptr=4 if pe32 else 8
        while o+20<=len(data):
            oft,_,_,name_rva,ft=struct.unpack_from("<IIIII",data,o)
            if not any((oft,name_rva,ft)): break
            dll=cstr(data,rva_off(name_rva)).lower()
            thunk_rva=oft or ft; t=rva_off(thunk_rva); funcs=[]
            while t+ptr<=len(data):
                val=u32(data,t) if pe32 else struct.unpack_from("<Q",data,t)[0]
                if val==0: break
                ordinal_flag=0x80000000 if pe32 else 0x8000000000000000
                if val & ordinal_flag: funcs.append({"ordinal":val & 0xFFFF})
                else:
                    no=rva_off(val)
                    funcs.append({"name":cstr(data,no+2),"hint":u16(data,no)})
                t+=ptr
            imports.append({"dll":dll,"functions":funcs}); o+=20
    dlls=[x["dll"] for x in imports]
    lower_name=path.name.lower()
    return {
      "schema_version":1,"file":path.name,"size_bytes":len(data),
      "sha256":hashlib.sha256(data).hexdigest(),
      "pe":{"machine_hex":f"0x{machine:04x}","machine":MACHINES.get(machine,"unknown"),
            "format":"PE32" if pe32 else "PE32+","entry_rva":entry,
            "image_base":image_base,"subsystem":SUBSYSTEMS.get(subsystem,str(subsystem)),
            "sections":sections},
      "imports":imports,
      "summary":{"dll_count":len(imports),"function_import_count":sum(len(x["functions"]) for x in imports),
                 "graphics_dlls":sorted(set(dlls)&GRAPHICS),
                 "windows_surface":sorted(set(dlls)&WINDOWS),
                 "middleware_hints":sorted([d for d in dlls if any(h in d for h in MIDDLEWARE_HINTS)]),
                 "filename_hint":lower_name}
    }

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("binary",type=Path)
    ap.add_argument("-o","--output",type=Path)
    a=ap.parse_args()
    report=parse_pe(a.binary)
    text=json.dumps(report,indent=2)
    if a.output: a.output.write_text(text+"\n",encoding="utf-8")
    else: print(text)
if __name__=="__main__": main()
