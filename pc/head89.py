"""Голова ZoomLamp — AT89S8253 по ISP: состояние, дамп, прошивка.

log(text, tag), progress(доля 0…1) — как в operations.py.
confirm(text) → bool — вопрос оператору (окно «Да / Нет»).
"""

import datetime
import os
import time

import firmware_image
from device import Device, ISP_CODE, ISP_DATA, ISP_ATMEL, ISP_USER, ISP_LOCK, ISP_FUSES
from operations import Abort, hexs

FLASH_SIZE = 0x3000
EEPROM_SIZE = 0x800
FLASH_PAGE = 64
EEPROM_PAGE = 32
SIG_SIZE = 64
SIGNATURE = bytes([0x1E, 0x73, 0x01])

HERE = os.path.dirname(os.path.abspath(__file__))
DUMP_DIR = os.path.join(HERE, "dumps")


# ---------------------------------------------------------------------------
#  Файлы
# ---------------------------------------------------------------------------

def load_image(path: str, size: int, what: str) -> bytes:
    """Flash или EEPROM из .bin (как есть) или Intel HEX (.hex / .ihx от SDCC)."""
    with open(path, "rb") as f:
        data = f.read()
    if path.lower().endswith(".bin"):
        body = data
    else:
        body = bytes(firmware_image.load(path).flash)
    if len(body) > size:
        if any(b != 0xFF for b in body[size:]):
            raise ValueError(f"{what}: {len(body)} Б — больше {size} Б")
        body = body[:size]
    return body


def find_eeprom_for(flash_path: str) -> str:
    """eeprom.bin рядом с flash.bin / flash.hex дампа — для восстановления оригинала."""
    d = os.path.dirname(flash_path)
    for name in ("eeprom.bin", "eeprom.hex"):
        p = os.path.join(d, name)
        if os.path.basename(flash_path).lower().startswith("flash") and os.path.exists(p):
            return p
    return ""


def _write_hex(path: str, data: bytes):
    lines = []
    for a in range(0, len(data), 16):
        chunk = data[a:a + 16]
        rec = bytes([len(chunk), a >> 8, a & 0xFF, 0]) + chunk
        lines.append(":" + (rec + bytes([-sum(rec) & 0xFF])).hex().upper())
    lines.append(":00000001FF")
    with open(path, "w", encoding="ascii") as f:
        f.write("\n".join(lines) + "\n")


def _save_dump(d: dict, suffix: str, log) -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = os.path.join(DUMP_DIR, f"head_{stamp}{suffix}")
    n = 1
    while os.path.exists(folder):                  # дамп не перезаписывается никогда
        n += 1
        folder = os.path.join(DUMP_DIR, f"head_{stamp}{suffix}_{n}")
    os.makedirs(folder)
    for name in ("flash", "eeprom", "atmel_sig", "user_sig"):
        with open(os.path.join(folder, f"{name}.bin"), "wb") as f:
            f.write(d[name])
    _write_hex(os.path.join(folder, "flash.hex"), d["flash"])
    _write_hex(os.path.join(folder, "eeprom.hex"), d["eeprom"])
    with open(os.path.join(folder, "info.txt"), "w", encoding="utf-8") as f:
        f.write(f"AT89S8253, дамп {stamp}\n")
        f.write(f"Сигнатура: {hexs(d['signature'])}\n")
        f.write(f"Lock: {d['lock']:02X} — {describe_lock(d['lock'])}\n")
        f.write(f"Фьюзы: {d['fuses']:02X} — {describe_fuses(d['fuses'])}\n")
        f.write(f"Flash: {used(d['flash'])} Б не FF из {FLASH_SIZE}\n")
        f.write(f"EEPROM: {used(d['eeprom'])} Б не FF из {EEPROM_SIZE}\n")
        f.write("Чтение двойное, со сверкой.\n")
    log(f"Дамп сохранён: {folder}", "ok")
    return folder


# ---------------------------------------------------------------------------
#  Расшифровка
# ---------------------------------------------------------------------------

def used(data: bytes) -> int:
    return sum(b != 0xFF for b in data)


def lock_readable(lock: int) -> bool:
    return bool(lock & 0x02)                       # бит = 0 — LB2 запрограммирован


def describe_lock(lock: int) -> str:
    on = [f"LB{i + 1}" for i in range(3) if not lock & (1 << i)]
    if not on:
        return "защиты нет"
    text = "запрограммированы " + ", ".join(on)
    if not lock_readable(lock):
        text += " — чтение Flash/EEPROM запрещено"
    return text


def describe_fuses(f: int) -> str:
    # бит = 0 — фьюз включён (как в readfuses3.py)
    return ", ".join([
        "ISP разрешён" if not f & 1 else "ISP ЗАПРЕЩЁН",
        "X2 (6 тактов)" if not f & 2 else "12 тактов",
        "запись user row разрешена" if not f & 4 else "запись user row запрещена",
        "внутр. генератор" if not f & 8 else "кварц",
    ])


# ---------------------------------------------------------------------------
#  Обмен
# ---------------------------------------------------------------------------

def _open(dev: Device, log):
    mode, sig = dev.isp_open()
    if sig != SIGNATURE:
        dev.isp_close()
        raise Abort(f"Сигнатура {hexs(sig)} — не AT89S8253 ({hexs(SIGNATURE)})")
    log(f"AT89S8253 ({hexs(sig)}), SPI режим {mode}")
    lock = dev.isp_read(ISP_LOCK, 0, 1)[0]
    fuses = dev.isp_read(ISP_FUSES, 0, 1)[0]
    log(f"Lock {lock:02X}: {describe_lock(lock)}", None if lock_readable(lock) else "warn")
    log(f"Фьюзы {fuses:02X}: {describe_fuses(fuses)}")
    return sig, lock, fuses


def _read_space(dev: Device, space, size, page, progress, p0, p1) -> bytes:
    return dev.isp_read_block(space, 0, size, page,
                              lambda done: progress(p0 + (p1 - p0) * done / size))


def _read_all(dev: Device, progress, p0=0.0, p1=1.0) -> dict:
    """Flash и EEPROM — дважды со сверкой."""
    span = (p1 - p0) / 2
    reads = []
    for k in range(2):
        b = p0 + span * k
        flash = _read_space(dev, ISP_CODE, FLASH_SIZE, FLASH_PAGE, progress, b, b + span * 0.85)
        eeprom = _read_space(dev, ISP_DATA, EEPROM_SIZE, EEPROM_PAGE, progress, b + span * 0.85, b + span)
        reads.append((flash, eeprom))
    (f1, e1), (f2, e2) = reads
    if f1 != f2 or e1 != e2:
        nf = sum(a != b for a, b in zip(f1, f2))
        ne = sum(a != b for a, b in zip(e1, e2))
        raise Abort(f"Два чтения не совпали: Flash {nf} Б, EEPROM {ne} Б — "
                    "плохой контакт прищепки или помехи. Дамп не сохранён")
    return {"flash": f1, "eeprom": e1,
            "atmel_sig": dev.isp_read(ISP_ATMEL, 0, SIG_SIZE),
            "user_sig": dev.isp_read(ISP_USER, 0, SIG_SIZE)}


def _dump(dev: Device, sig, lock, fuses, log, progress, p0, p1, suffix=""):
    d = _read_all(dev, progress, p0, p1)
    d.update(signature=sig, lock=lock, fuses=fuses)
    log(f"Flash: {used(d['flash'])} Б не FF, EEPROM: {used(d['eeprom'])} Б не FF")
    if not used(d["flash"]):
        log("Flash пуста (все FF)" + ("" if lock_readable(lock) else " — или чтение запрещено lock"), "warn")
    return _save_dump(d, suffix, log)


# ---------------------------------------------------------------------------
#  Операции
# ---------------------------------------------------------------------------

def read_status(dev: Device, log):
    log("Состояние головы (AT89S8253)", "head")
    _open(dev, log)
    dev.isp_close()


def dump(dev: Device, log, progress):
    log("Дамп головы (AT89S8253)", "head")
    t0 = time.monotonic()
    sig, lock, fuses = _open(dev, log)
    try:
        if not lock_readable(lock):
            log("LB2 запрограммирован: прочитается не содержимое чипа. Дамп снимается для отчёта", "warn")
        _dump(dev, sig, lock, fuses, log, progress, 0.0, 1.0)
    finally:
        dev.isp_close()
    log(f"Готово за {time.monotonic() - t0:.1f} с", "ok")


def _pages(data: bytes, page: int):
    """(адрес, страница) без страниц из одних FF — после стирания они и так FF."""
    size = (len(data) + page - 1) // page * page
    data = data + b"\xFF" * (size - len(data))
    return [(a, data[a:a + page]) for a in range(0, size, page) if data[a:a + page] != b"\xFF" * page]


def program(dev: Device, flash: bytes, eeprom: bytes | None, log, progress, confirm):
    # TODO: ключ подписи чипов для прошивки головы V2 «с чипом» — см. README,
    # раздел «Не забыть: ключ подписи». Генерировать новый, при перепрошивке
    # переносить из дампа _before_write, писать в EEPROM после стирания.
    log("Прошивка головы (AT89S8253)", "head")
    if not used(flash):
        raise Abort("В файле Flash нет данных")
    if eeprom is None:
        log("EEPROM не задана: после стирания она останется пустой (FF) — "
            "коэффициенты и настройки головы будут потеряны", "warn")
    t0 = time.monotonic()
    sig, lock, fuses = _open(dev, log)
    try:
        if fuses & 1:
            log("Фьюз ISP выключен: после стирания чип может не войти в ISP", "warn")

        # Оригинал не теряем: перед стиранием — дамп
        if lock_readable(lock):
            log("Резервный дамп перед стиранием")
            _dump(dev, sig, lock, fuses, log, progress, 0.0, 0.3, "_before_write")
        elif not confirm("Чип защищён от чтения (LB2). Снять дамп нельзя, стирание "
                         "безвозвратно уничтожит оригинальную прошивку и EEPROM.\n\nСтереть?"):
            raise Abort("Прошивка отменена: чип защищён, дамп не снят")
        progress(0.3)

        dev.isp_erase()
        if dev.isp_read(ISP_CODE, 0, FLASH_PAGE) != b"\xFF" * FLASH_PAGE:
            raise Abort("После стирания Flash не пуста")
        log("Чип стёрт")

        fpages = _pages(flash, FLASH_PAGE)
        epages = _pages(eeprom, EEPROM_PAGE) if eeprom else []
        total = len(fpages) + len(epages)
        for i, (a, p) in enumerate(fpages):
            dev.isp_write_page(ISP_CODE, a, p)
            progress(0.3 + 0.35 * (i + 1) / total)
        log(f"Flash записана: {used(flash)} Б, страниц {len(fpages)}")
        for i, (a, p) in enumerate(epages):
            dev.isp_write_page(ISP_DATA, a, p)
            progress(0.3 + 0.35 * (len(fpages) + i + 1) / total)
        if epages:
            log(f"EEPROM записана: страниц {len(epages)}")

        # Сверка всей памяти: в незаписанных местах должно быть FF
        back = _read_space(dev, ISP_CODE, FLASH_SIZE, FLASH_PAGE, progress, 0.65, 0.92)
        want = flash + b"\xFF" * (FLASH_SIZE - len(flash))
        bad = [i for i in range(FLASH_SIZE) if back[i] != want[i]]
        if bad:
            raise Abort(f"Сверка Flash: не совпало {len(bad)} Б, первый по адресу 0x{bad[0]:04X} "
                        f"(записано {want[bad[0]]:02X}, прочитано {back[bad[0]]:02X})")
        eback = _read_space(dev, ISP_DATA, EEPROM_SIZE, EEPROM_PAGE, progress, 0.92, 1.0)
        ewant = (eeprom or b"") + b"\xFF" * (EEPROM_SIZE - len(eeprom or b""))
        bad = [i for i in range(EEPROM_SIZE) if eback[i] != ewant[i]]
        if bad:
            raise Abort(f"Сверка EEPROM: не совпало {len(bad)} Б, первый по адресу 0x{bad[0]:03X}")
        log("Сверка Flash и EEPROM: совпадает", "ok")
        log("Lock не ставится (запись lock для AT89S8253 пока не сделана)")
    finally:
        dev.isp_close()
    progress(1.0)
    log(f"Прошивка завершена за {time.monotonic() - t0:.1f} с", "ok")
