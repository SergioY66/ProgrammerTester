"""Разбор файла прошивки (.elf / .hex) для tinyAVR.

Из ELF (avr-gcc / Microchip Studio) берутся области по адресу загрузки (LMA):
    0x000000…  Flash (.text, .data, .rodata)
    0x810000…  EEPROM (.eeprom)
    0x820000…  фьюзы (.fuse, FUSES = {...})
    0x830000…  lock (.lock, LOCKBITS = ...)
    0x840000…  сигнатура (.signature, #include <avr/signature.h>)
Без сторонних библиотек: ELF32 разбирается вручную.
"""

import os
import struct
from dataclasses import dataclass, field

EEPROM_LMA = 0x810000
FUSE_LMA = 0x820000
LOCK_LMA = 0x830000
SIG_LMA = 0x840000
USERSIG_LMA = 0x850000
RAM_BASE_TO_SIG = {
    0x3E00: bytes([0x1E, 0x93, 0x21]),   # ATtiny816
    0x3800: bytes([0x1E, 0x94, 0x21]),   # ATtiny1616
}


@dataclass
class FirmwareImage:
    path: str
    flash: bytearray = field(default_factory=bytearray)
    eeprom: dict = field(default_factory=dict)       # смещение → байт
    fuses: dict = field(default_factory=dict)        # номер фьюза (0…8) → байт
    lock: int | None = None
    signature: bytes | None = None                   # SIGNATURE0, 1, 2 (1E 93 21)
    warnings: list = field(default_factory=list)
    ram_base: int | None = None                      # начало RAM по секции .data (0x3800 / 0x3E00)
    sig_inferred: bool = False                      # сигнатура выведена по адресу RAM, а не из .signature

    @property
    def has_fuses(self) -> bool:
        return bool(self.fuses)


def load(path: str) -> FirmwareImage:
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] == b"\x7fELF":
        return _load_elf(path, data)
    if ext in (".hex", ".ihex") or data[:1] == b":":
        return _load_hex(path, data)
    raise ValueError("Неизвестный формат файла (нужен .elf или .hex)")


# ---------------------------------------------------------------------------
#  ELF
# ---------------------------------------------------------------------------

def _load_elf(path: str, data: bytes) -> FirmwareImage:
    if data[4] != 1 or data[5] != 1:
        raise ValueError("Ожидается ELF32 little-endian (AVR)")
    (e_type, e_machine, _ver, _entry, e_phoff, e_shoff, _flags, _ehsize,
     e_phentsize, e_phnum, e_shentsize, e_shnum, e_shstrndx) = struct.unpack_from(
        "<HHIIIIIHHHHHH", data, 16)
    if e_machine != 83:
        raise ValueError(f"ELF не для AVR (e_machine = {e_machine})")

    segments = []
    for i in range(e_phnum):
        p_type, p_offset, _vaddr, p_paddr, p_filesz, _memsz, _f, _a = struct.unpack_from(
            "<IIIIIIII", data, e_phoff + i * e_phentsize)
        if p_type == 1 and p_filesz:                # PT_LOAD
            segments.append((p_offset, p_paddr, p_filesz))

    sections = []
    for i in range(e_shnum):
        sections.append(struct.unpack_from("<IIIIIIIIII", data, e_shoff + i * e_shentsize))
    strtab_off = sections[e_shstrndx][4]

    def sec_name(off):
        end = data.index(b"\0", strtab_off + off)
        return data[strtab_off + off:end].decode("ascii", "replace")

    img = FirmwareImage(path)
    flash_chunks = []
    for (sh_name, sh_type, sh_flags, sh_addr, sh_offset, sh_size, *_rest) in sections:
        if sh_type != 1 or not (sh_flags & 0x2) or sh_size == 0:   # PROGBITS + ALLOC
            continue
        name = sec_name(sh_name)
        lma = sh_addr
        for p_offset, p_paddr, p_filesz in segments:
            if p_offset <= sh_offset < p_offset + p_filesz:
                lma = p_paddr + (sh_offset - p_offset)
                break
        body = data[sh_offset:sh_offset + sh_size]
        _place(img, name, lma, body, flash_chunks)
        if 0x800000 <= sh_addr < 0x810000:           # SRAM: .data/.bss
            ram_base = sh_addr - 0x800000
            if img.ram_base is None or ram_base < img.ram_base:
                img.ram_base = ram_base

    _build_flash(img, flash_chunks)
    # Без .signature чип определяем по началу RAM: ATtiny1616 — 2 КБ (0x3800), ATtiny816 — 512 Б (0x3E00)
    if img.signature is None and img.ram_base in RAM_BASE_TO_SIG:
        img.signature = RAM_BASE_TO_SIG[img.ram_base]
        img.sig_inferred = True
    return img


def _place(img, name, lma, body, flash_chunks):
    if lma < 0x800000:
        flash_chunks.append((lma, body))
    elif EEPROM_LMA <= lma < FUSE_LMA:
        for i, b in enumerate(body):
            img.eeprom[lma - EEPROM_LMA + i] = b
    elif FUSE_LMA <= lma < LOCK_LMA:
        for i, b in enumerate(body):
            img.fuses[lma - FUSE_LMA + i] = b
    elif LOCK_LMA <= lma < SIG_LMA:
        img.lock = body[0]
    elif SIG_LMA <= lma < USERSIG_LMA:
        # avr/signature.h хранит {SIGNATURE_2, SIGNATURE_1, SIGNATURE_0}
        img.signature = bytes(reversed(body[:3]))
    else:
        img.warnings.append(f"Секция {name} @0x{lma:06X} не записывается (USERROW и т. п.)")


def _build_flash(img, chunks):
    if not chunks:
        return
    end = max(a + len(b) for a, b in chunks)
    flash = bytearray(b"\xFF" * end)
    for a, b in chunks:
        flash[a:a + len(b)] = b
    img.flash = flash


# ---------------------------------------------------------------------------
#  Intel HEX (только Flash, если не разложен по адресам 0x81…/0x82…)
# ---------------------------------------------------------------------------

def _load_hex(path: str, data: bytes) -> FirmwareImage:
    img = FirmwareImage(path)
    base = 0
    mem = {}
    for ln, line in enumerate(data.decode("ascii", "replace").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise ValueError(f"HEX, строка {ln}: нет ':'")
        rec = bytes.fromhex(line[1:])
        if sum(rec) & 0xFF:
            raise ValueError(f"HEX, строка {ln}: неверная контрольная сумма")
        n, addr, typ = rec[0], (rec[1] << 8) | rec[2], rec[3]
        body = rec[4:4 + n]
        if typ == 0:
            for i, b in enumerate(body):
                mem[base + addr + i] = b
        elif typ == 1:
            break
        elif typ == 2:
            base = ((body[0] << 8) | body[1]) << 4
        elif typ == 4:
            base = ((body[0] << 8) | body[1]) << 16
    chunks = {}
    for a, b in mem.items():
        if a < 0x800000:
            chunks[a] = b
        else:
            _place(img, "hex", a, bytes([b]), [])
    if chunks:
        flash = bytearray(b"\xFF" * (max(chunks) + 1))
        for a, b in chunks.items():
            flash[a] = b
        img.flash = flash
    return img


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        im = load(p)
        print(p)
        print(f"  Flash {len(im.flash)} байт, EEPROM {len(im.eeprom)} байт")
        print("  Фьюзы:", " ".join(f"{k}:{v:02X}" for k, v in sorted(im.fuses.items())) or "нет")
        print("  Lock:", f"{im.lock:02X}" if im.lock is not None else "нет")
        print("  Сигнатура:", im.signature.hex(" ").upper() if im.signature else "нет")
        for w in im.warnings:
            print("  !", w)
