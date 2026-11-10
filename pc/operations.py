"""Операции: прошивка с проверкой, фьюзы и lock, тесты изделий.

log(text, tag) — tag: None, "ok", "warn", "err", "head".
progress(доля 0…1).
"""

import datetime
import os
import time

from device import Device, ST_OW_DIFF
from firmware_image import FirmwareImage


class Abort(Exception):
    """Операция остановлена: изделие не годно или прошивать нельзя."""


# --- tinyAVR 1-series ---
DEVICES = {
    bytes([0x1E, 0x93, 0x21]): ("ATtiny816", 8 * 1024, 128),
    bytes([0x1E, 0x94, 0x21]): ("ATtiny1616", 16 * 1024, 256),
}
SIGROW = 0x1100
FUSE_BASE = 0x1280
LOCK_ADDR = 0x128A
FLASH_PAGE = 64
EEPROM_PAGE = 32
FUSE_NAMES = ["WDTCFG", "BODCFG", "OSCCFG", None, "TCD0CFG", "SYSCFG0", "SYSCFG1", "APPEND", "BOOTEND"]
SYSCFG0 = 5
LOCK_NONE = 0xC5          # lock снят
LOCK_RWLOCK = 0x3A        # чтение и запись запрещены
SYS_LOCKSTATUS = 0x01


def hexs(b) -> str:
    return " ".join(f"{x:02X}" for x in b)


# ---------------------------------------------------------------------------
#  Проверка файла до любых действий с изделием
# ---------------------------------------------------------------------------

def plan_fuses_and_lock(img: FirmwareImage, log):
    """Какие фьюзы и lock будут записаны. Abort — если прошивать нельзя."""
    if not img.flash:
        raise Abort("В файле нет данных для Flash")
    if not img.has_fuses:
        raise Abort("В файле нет фьюзов (.fuse). Объявите FUSES = {...} в прошивке "
                    "или используйте .elf вместо .hex")
    fuses = {i: v for i, v in img.fuses.items() if i < len(FUSE_NAMES) and FUSE_NAMES[i]}

    # Вывод UPDI не должен стать GPIO/RESET — иначе без 12 В изделие больше не прошить
    if SYSCFG0 in fuses:
        rstpin = (fuses[SYSCFG0] >> 2) & 3
        if rstpin != 1:
            mode = {0: "GPIO", 2: "RESET"}.get(rstpin, f"{rstpin}")
            raise Abort(f"SYSCFG0 = {fuses[SYSCFG0]:02X}: вывод UPDI станет {mode}. "
                        "После этого чип не прошить без высокого напряжения. Прошивка отменена")

    if img.lock is None:
        log(f"В файле нет lock — будет установлен автоматически ({LOCK_RWLOCK:02X})", "warn")
        lock = LOCK_RWLOCK
    elif img.lock == LOCK_NONE:
        log(f"В файле lock = {LOCK_NONE:02X} (без защиты) — будет установлен {LOCK_RWLOCK:02X}", "warn")
        lock = LOCK_RWLOCK
    else:
        lock = img.lock
    return fuses, lock


def describe(img: FirmwareImage) -> str:
    parts = [f"Flash {len(img.flash)} Б"]
    if img.eeprom:
        parts.append(f"EEPROM {len(img.eeprom)} Б")
    if img.fuses:
        parts.append("фьюзы " + " ".join(
            f"{FUSE_NAMES[i]}={v:02X}" for i, v in sorted(img.fuses.items())
            if i < len(FUSE_NAMES) and FUSE_NAMES[i]))
    else:
        parts.append("ФЬЮЗОВ НЕТ")
    parts.append(f"lock {img.lock:02X}" if img.lock is not None else "lock нет")
    if img.signature:
        name = DEVICES.get(img.signature, ("?",))[0]
        parts.append(f"для {name}" + (" (по RAM)" if img.sig_inferred else ""))
    return " · ".join(parts)


# ---------------------------------------------------------------------------
#  Прошивка
# ---------------------------------------------------------------------------

def _identify(dev: Device, log):
    sig = dev.read(SIGROW, 3)
    if sig not in DEVICES:
        raise Abort(f"Неизвестная сигнатура {hexs(sig)} — не ATtiny816/1616")
    name, flash_size, ee_size = DEVICES[sig]
    log(f"Микроконтроллер: {name} ({hexs(sig)})")
    return sig, flash_size, ee_size


def _eeprom_runs(eeprom: dict):
    """Непрерывные куски EEPROM, не пересекающие границу страницы."""
    runs, cur_start, cur = [], None, bytearray()
    for off in sorted(eeprom):
        if cur and (off != cur_start + len(cur) or off % EEPROM_PAGE == 0):
            runs.append((cur_start, bytes(cur)))
            cur = bytearray()
        if not cur:
            cur_start = off
        cur.append(eeprom[off])
    if cur:
        runs.append((cur_start, bytes(cur)))
    return runs


def program(dev: Device, img: FirmwareImage, log, progress):
    log("Прошивка", "head")
    fuses, lock = plan_fuses_and_lock(img, log)

    t0 = time.monotonic()
    statusa = dev.updi_open()
    log(f"Связь UPDI есть (ревизия {statusa >> 4})")
    try:
        dev.updi_erase_enter()
        log("Чип стёрт, режим программирования")
        sig, flash_size, ee_size = _identify(dev, log)
        if img.signature and img.signature != sig:
            raise Abort(f"Прошивка собрана для {DEVICES.get(img.signature, ('?',))[0]} "
                        f"({hexs(img.signature)}{', по адресу RAM' if img.sig_inferred else ''}), "
                        f"а в разъёме {DEVICES[sig][0]}")
        if len(img.flash) > flash_size:
            raise Abort(f"Прошивка {len(img.flash)} Б больше Flash {flash_size} Б")
        if img.eeprom and max(img.eeprom) >= ee_size:
            raise Abort(f"Данные EEPROM выходят за {ee_size} Б")

        # --- запись Flash ---
        size = (len(img.flash) + FLASH_PAGE - 1) // FLASH_PAGE * FLASH_PAGE
        flash = bytes(img.flash) + b"\xFF" * (size - len(img.flash))
        pages = [(o, flash[o:o + FLASH_PAGE]) for o in range(0, size, FLASH_PAGE)]
        pages = [(o, p) for o, p in pages if p != b"\xFF" * FLASH_PAGE]   # после стирания и так FF
        for i, (off, page) in enumerate(pages):
            dev.write_flash_page(off, page)
            progress(0.45 * (i + 1) / len(pages))
        log(f"Flash записана: {len(img.flash)} Б, страниц {len(pages)}")

        runs = _eeprom_runs(img.eeprom)
        for off, data in runs:
            dev.write_eeprom(off, data)
        if runs:
            log(f"EEPROM записана: {len(img.eeprom)} Б")

        # --- сверка ---
        back = b""
        for off in range(0, size, 128):
            back += dev.read(0x8000 + off, min(128, size - off))
            progress(0.45 + 0.45 * len(back) / size)
        if back != flash:
            bad = [i for i in range(size) if back[i] != flash[i]]
            raise Abort(f"Сверка Flash: не совпало {len(bad)} Б, первый по адресу 0x{bad[0]:04X} "
                        f"(записано {flash[bad[0]]:02X}, прочитано {back[bad[0]]:02X})")
        for off, data in runs:
            got = dev.read(0x1400 + off, len(data))
            if got != data:
                raise Abort(f"Сверка EEPROM: не совпало в куске 0x{off:02X}")
        log("Сверка прошивки: совпадает", "ok")

        # --- фьюзы ---
        for i, v in sorted(fuses.items()):
            dev.write_fuse(FUSE_BASE + i, v)
        got = dev.read(FUSE_BASE, len(FUSE_NAMES))
        bad = [f"{FUSE_NAMES[i]}: {got[i]:02X} вместо {v:02X}" for i, v in fuses.items() if got[i] != v]
        if bad:
            raise Abort("Фьюзы не записались: " + ", ".join(bad))
        log("Фьюзы записаны и проверены: " + " ".join(f"{FUSE_NAMES[i]}={v:02X}" for i, v in sorted(fuses.items())), "ok")

        # --- lock: только после успешной сверки ---
        dev.write_fuse(LOCK_ADDR, lock)
        progress(0.95)
    finally:
        dev.updi_close()

    # Проверка lock: после сброса чип должен сообщить LOCKSTATUS
    time.sleep(0.2)
    dev.updi_open()
    try:
        ss = dev.updi_status()
    finally:
        dev.updi_close()
    if not ss & SYS_LOCKSTATUS:
        raise Abort(f"Lock {lock:02X} записан, но чип не заблокирован (SYS_STATUS {ss:02X})")
    log(f"Lock {lock:02X} установлен, чип заблокирован", "ok")
    progress(1.0)
    log(f"Прошивка завершена за {time.monotonic() - t0:.1f} с", "ok")


def read_status(dev: Device, log):
    """Сигнатура, фьюзы, lock — без изменений в чипе."""
    log("Состояние чипа", "head")
    dev.updi_open()
    try:
        if not dev.updi_enter():
            log("Чип заблокирован (lock). Прочитать можно только после стирания", "warn")
            return
        _identify(dev, log)
        f = dev.read(FUSE_BASE, len(FUSE_NAMES))
        log("Фьюзы: " + " ".join(f"{n}={v:02X}" for n, v in zip(FUSE_NAMES, f) if n))
        lk = dev.read(LOCK_ADDR, 1)[0]
        log(f"Lock: {lk:02X} ({'нет защиты' if lk == LOCK_NONE else 'заблокирован'})")
    finally:
        dev.updi_close()


# ---------------------------------------------------------------------------
#  Тесты
# ---------------------------------------------------------------------------

CARD_EXPECTED = bytes([0x01, 0x14])


def test_card(dev: Device, log):
    log("Тест карты", "head")
    got = dev.card_test()
    log(f"Ответ карты: {hexs(got)} (ожидается {hexs(CARD_EXPECTED)})")
    if got != CARD_EXPECTED:
        raise Abort("Карта ответила неверно — проверьте пайку и КЗ между контактами")
    log("Тест карты пройден", "ok")


def _crc8(data) -> int:
    crc = 0
    for b in data:
        for _ in range(8):
            mix = (crc ^ b) & 1
            crc >>= 1
            if mix:
                crc ^= 0x8C
            b >>= 1
    return crc


def _chip_expected() -> bytes:
    """Память нового (не привязанного) чипа 0x00–0x8F."""
    m = bytearray(0x90)
    m[0x00:0x48] = b"\xFF" * 0x48
    m[0x60:0x62] = bytes([0x0F, 0x3C])                     # длительность 15, лимит 60
    m[0x68:0x70] = bytes([0xFF] * 7 + [0x0F])              # 60 минут
    m[0x78:0x80] = b"\xFF" * 8                             # не привязан
    m[0x80:0x88] = bytes([0xAA, 0xAA, 0xAA, 0xAA, 0x55, 0x55, 0x44, 0x44])
    return bytes(m)


CHIP_EXPECTED = _chip_expected()


def test_chip(dev: Device, log, charge_ms: int):
    log(f"Тест чипа (паразитное питание, зарядка {charge_ms} мс)", "head")
    status, rom, mem = dev.chip_test(charge_ms)
    log(f"ROM: {hexs(rom)}")
    errors = []
    if status == ST_OW_DIFF:
        errors.append("два чтения не совпали")
    if rom[0] != 0xAD:
        errors.append(f"код семейства {rom[0]:02X} вместо AD")
    if rom[5:7] != b"\x20\x00":
        errors.append(f"ROM[5..6] = {hexs(rom[5:7])} вместо 20 00")
    if _crc8(rom[:7]) != rom[7]:
        errors.append("CRC8 ROM неверна")
    diff = [a for a in range(len(CHIP_EXPECTED)) if mem[a] != CHIP_EXPECTED[a]]
    if diff:
        errors.append(f"память отличается в {len(diff)} байтах")
        for row in range(0, 0x90, 16):
            if any(row <= a < row + 16 for a in diff):
                log(f"  {row:02X}: {hexs(mem[row:row + 16])}", "warn")
                log(f"  жд: {hexs(CHIP_EXPECTED[row:row + 16])}")
    if errors:
        raise Abort("Тест чипа не пройден: " + "; ".join(errors))
    log("Тест чипа пройден: ROM, CRC8 и память в порядке", "ok")


DUMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dumps")


def _hexdump(data: bytes) -> str:
    return "\n".join(f"{a:02X}: {hexs(data[a:a + 16])}" for a in range(0, len(data), 16)) + "\n"


def dump_chip(dev: Device, log, charge_ms: int) -> str:
    """Читает ROM и память чипа Zoom (1-Wire) и сохраняет в dumps/chip_<время>_<ROM>/.

    Чип не бракуется: дамп снимают и с привязанных чипов, которые от нового отличаются.
    Возвращает путь к папке дампа.
    """
    log(f"Дамп чипа (паразитное питание, зарядка {charge_ms} мс)", "head")
    status, rom, mem = dev.chip_test(charge_ms)
    log(f"ROM: {hexs(rom)}")
    notes = []
    if status == ST_OW_DIFF:
        notes.append("два чтения не совпали — дамп ненадёжен")
    if rom[0] != 0xAD:
        notes.append(f"код семейства {rom[0]:02X} вместо AD")
    if _crc8(rom[:7]) != rom[7]:
        notes.append("CRC8 ROM неверна")
    diff = [a for a in range(len(CHIP_EXPECTED)) if mem[a] != CHIP_EXPECTED[a]]
    for row in range(0, len(mem), 16):             # содержимое памяти на экран; отличия от нового чипа — жёлтым
        bad = any(row <= a < row + 16 for a in diff)
        log(f"  {row:02X}: {hexs(mem[row:row + 16])}", "warn" if bad else None)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    rom_id = rom[0:7].hex().upper()          # код семейства + 6 байт серийного номера, без CRC
    folder = os.path.join(DUMP_DIR, f"chip_{stamp}_{rom_id}")
    n = 1
    while os.path.exists(folder):                  # дамп не перезаписывается никогда
        n += 1
        folder = os.path.join(DUMP_DIR, f"chip_{stamp}_{rom_id}_{n}")
    os.makedirs(folder)
    with open(os.path.join(folder, "rom.bin"), "wb") as f:
        f.write(rom)
    with open(os.path.join(folder, "mem.bin"), "wb") as f:
        f.write(mem)
    with open(os.path.join(folder, "mem.txt"), "w", encoding="utf-8") as f:
        f.write(_hexdump(mem))
    with open(os.path.join(folder, "info.txt"), "w", encoding="utf-8") as f:
        f.write(f"Чип Zoom (1-Wire), дамп {stamp}\n")
        f.write(f"ROM: {hexs(rom)}\n")
        f.write(f"Память: 0x00–0x{len(mem) - 1:02X} ({len(mem)} Б), зарядка {charge_ms} мс\n")
        if diff:
            f.write(f"Отличается от нового чипа в {len(diff)} байтах: "
                    + " ".join(f"{a:02X}" for a in diff) + "\n")
        else:
            f.write("Совпадает с памятью нового (непривязанного) чипа\n")
        for note in notes:
            f.write(f"Внимание: {note}\n")
    for note in notes:
        log(note, "warn")
    log("Память совпадает с новым чипом" if not diff
        else f"Память отличается от нового чипа в {len(diff)} байтах (чип привязан или использовался)")
    log(f"Дамп сохранён: {folder}", "ok")
    return folder
