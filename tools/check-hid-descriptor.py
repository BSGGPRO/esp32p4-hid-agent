"""Parse the HID report descriptor out of the built firmware ELF.

Verifies docs/protocol.md section 5: single HID interface with two report IDs,
Report ID 1 = keyboard (8 bytes: modifier, reserved, k0..k5),
Report ID 2 = mouse    (5 bytes: buttons, dx, dy, wheel, pan).

The descriptor is located by parsing the ELF symbol table for
`hid_report_descriptor` and mapping its vaddr through the section headers.
(Never hardcode the address: it moves on every rebuild and a stale address
silently reads some other blob - e.g. the USB config descriptor - which then
fails with a confusing IndexError.)
"""

import struct
import sys

import os
_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_ELF = os.path.join(_HERE, '..', 'firmware', 'build', 'esp32p4_hid_agent.elf')
ELF = os.path.normpath(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_ELF
SYM_NAME = "hid_report_descriptor"

SHT_SYMTAB = 2
SHT_NOBITS = 8

# Fallback: the keyboard collection opening, used only if the ELF is stripped.
FALLBACK_MAGIC = bytes([0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x01])


def _sections(blob):
    if blob[:4] != b"\x7fELF":
        raise SystemExit("not an ELF file: %s" % ELF)
    if blob[4] != 1:
        raise SystemExit("expected ELF32, got class %d" % blob[4])
    e_shoff, = struct.unpack_from("<I", blob, 32)
    e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", blob, 46)
    secs = []
    for n in range(e_shnum):
        off = e_shoff + n * e_shentsize
        (sh_name, sh_type, _flags, sh_addr, sh_offset,
         sh_size, sh_link, _info, _align, sh_entsize) = struct.unpack_from("<10I", blob, off)
        secs.append({
            "name_off": sh_name, "type": sh_type, "addr": sh_addr,
            "offset": sh_offset, "size": sh_size, "link": sh_link,
            "entsize": sh_entsize,
        })
    shstr = secs[e_shstrndx]
    strtab = blob[shstr["offset"]:shstr["offset"] + shstr["size"]]
    for s in secs:
        end = strtab.find(b"\x00", s["name_off"])
        s["name"] = strtab[s["name_off"]:end].decode("utf-8", "replace")
    return secs


def _find_symbol(blob, secs, name):
    for s in secs:
        if s["type"] != SHT_SYMTAB or s["entsize"] == 0:
            continue
        strtab = secs[s["link"]]
        names = blob[strtab["offset"]:strtab["offset"] + strtab["size"]]
        count = s["size"] // s["entsize"]
        for n in range(count):
            off = s["offset"] + n * s["entsize"]
            st_name, st_value, st_size = struct.unpack_from("<III", blob, off)
            if st_name == 0 or st_size == 0:
                continue
            end = names.find(b"\x00", st_name)
            if names[st_name:end].decode("utf-8", "replace") == name:
                return st_value, st_size
    return None


def _vaddr_to_offset(secs, vaddr):
    for s in secs:
        if s["type"] == SHT_NOBITS or s["addr"] == 0:
            continue
        if s["addr"] <= vaddr < s["addr"] + s["size"]:
            return s["offset"] + (vaddr - s["addr"]), s["name"]
    return None, None


def load_descriptor():
    with open(ELF, "rb") as f:
        blob = f.read()

    secs = _sections(blob)
    hit = _find_symbol(blob, secs, SYM_NAME)

    if hit is None:
        # Stripped ELF: scan every PROGBITS section for the keyboard collection.
        for s in secs:
            if s["type"] in (0, SHT_NOBITS) or s["size"] == 0:
                continue
            body = blob[s["offset"]:s["offset"] + s["size"]]
            pos = body.find(FALLBACK_MAGIC)
            if pos >= 0:
                print("WARN: %s not in symtab, matched descriptor bytes in %s" % (SYM_NAME, s["name"]))
                return body[pos:pos + 146]
        raise SystemExit("cannot locate %s in %s" % (SYM_NAME, ELF))

    vaddr, size = hit
    off, secname = _vaddr_to_offset(secs, vaddr)
    if off is None:
        raise SystemExit("vaddr 0x%08x of %s is not in any section" % (vaddr, SYM_NAME))
    print("symbol %s: vaddr=0x%08x size=%d -> %s + 0x%x"
          % (SYM_NAME, vaddr, size, secname, off - next(
              s["offset"] for s in secs if s["name"] == secname)))
    return blob[off:off + size]


def parse(d):
    i = 0
    report_id = None
    g = {"size": 0, "count": 0, "logmin": 0, "logmax": 0, "usage_page": 0}
    cur = []
    reports = {}
    while i < len(d):
        b = d[i]
        i += 1
        if b == 0xFE:  # long item
            i += 2 + d[i]
            continue
        n = b & 0x03
        if n == 3:
            n = 4
        tag = (b >> 4) & 0x0F
        typ = (b >> 2) & 0x03
        val = 0
        for k in range(n):
            val |= d[i + k] << (8 * k)
        i += n

        if typ == 1:      # global
            if tag == 0:
                g["usage_page"] = val
            elif tag == 1:
                g["logmin"] = val
            elif tag == 2:
                g["logmax"] = val
            elif tag == 7:
                g["size"] = val
            elif tag == 8:
                report_id = val
            elif tag == 9:
                g["count"] = val
        elif typ == 2:    # local
            if tag == 0:
                cur.append(val if n == 4 else (g["usage_page"] << 16) | val)
        elif typ == 0:    # main
            if tag == 8:  # Input
                reports.setdefault(report_id, []).append(
                    {
                        "page": g["usage_page"],
                        "usages": list(cur) or [0],
                        "bits": g["size"] * g["count"],
                        "count": g["count"],
                        "size": g["size"],
                        "const": bool(val & 0x01),
                        "rel": bool(val & 0x04),
                        "logmin": g["logmin"],
                        "logmax": g["logmax"],
                    }
                )
                cur = []
            elif tag in (9, 11):  # Output / Feature
                cur = []
            elif tag == 12:       # End Collection
                cur = []
    return reports


def main():
    d = load_descriptor()
    print("descriptor: %d bytes" % len(d))
    print(" ".join("%02x" % b for b in d))
    print()

    reports = parse(d)
    print("=== parsed input reports ===")
    for rid in sorted(reports, key=lambda x: (x is None, x)):
        fields = reports[rid]
        total = sum(f["bits"] for f in fields)
        print("Report ID %s: %d bits = %d bytes" % (rid, total, (total + 7) // 8))
        for f in fields:
            kind = "CONST(pad)" if f["const"] else ("REL" if f["rel"] else "ABS")
            usages = ",".join("0x%04x" % u for u in f["usages"][:8])
            print(
                "   %-10s %3d bits  count=%-2d size=%-2d log=[%s..%s] page=0x%02x usages=%s"
                % (kind, f["bits"], f["count"], f["size"], f["logmin"], f["logmax"], f["page"], usages)
            )

    expected = {1: 8 * 8, 2: 5 * 8}
    print()
    ok = set(reports) == {1, 2}
    for rid, want in expected.items():
        got = sum(f["bits"] for f in reports.get(rid, []))
        good = got == want
        ok = ok and good
        print("Report ID %d: got %d bits, want %d bits -> %s" % (rid, got, want, "OK" if good else "MISMATCH"))

    # keyboard must carry 6 non-modifier keycode slots (8-bit array, logical max 0xFF)
    kb = reports.get(1, [])
    kb_keys = sum(f["count"] for f in kb
                  if not f["const"] and f["size"] == 8 and f["logmax"] == 0xFF and f["page"] == 0x07)
    mse = reports.get(2, [])
    # dx+dy arrive as ONE REL item with count=2 on the Generic Desktop page
    rel_axes = sum(f["count"] for f in mse
                   if f["rel"] and f["page"] == 0x01 and set(f["usages"]) <= {0x10030, 0x10031})
    wheel = sum(f["count"] for f in mse if f["rel"] and 0x10038 in f["usages"])
    pan = sum(f["count"] for f in mse if f["rel"] and 0xC0238 in f["usages"])
    print()
    print("keyboard keycode slots: %d (want 6)" % kb_keys)
    print("mouse relative axes   : %d (want 2: dx, dy)" % rel_axes)
    print("mouse wheel           : %d (want 1)" % wheel)
    print("mouse pan             : %d (want 1)" % pan)
    ok = ok and kb_keys == 6 and rel_axes == 2 and wheel == 1 and pan == 1

    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
