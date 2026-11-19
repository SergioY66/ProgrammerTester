"""AT89 по ISP (AT89S8253 — голова ZoomLamp, AT89S52 — микросхема в панельке):
состояние, дамп, прошивка.

Тип чипа определяется по сигнатуре сам. У закрытого от чтения чипа сигнатуры
нет — тогда тип выбирает оператор (choose_type).

log(text, tag), progress(доля 0…1) — как в operations.py.
confirm(text) → bool — вопрос оператору (окно «Да / Нет», по умолчанию «Нет»).
choose_type(text, yes_text, no_text) → bool | None — выбор из двух, None — отмена.
ask_runtime() → int | None — наработка головы в минутах, None — отмена.
"""

import datetime
import os
import secrets
import time

import firmware_image
from device import (Device, ISP_CODE, ISP_DATA, ISP_ATMEL, ISP_USER, ISP_LOCK,
                    ISP_FUSES, ISP_DEV_8253, ISP_DEV_S52, IspSignatureUnknown)
from operations import Abort, hexs

FLASH_SIZE = 0x3000                 # проверка файла: по старшему чипу (S52 — 0x2000)
SIG_SIZE = 64                       # user row 8253

# ---------------------------------------------------------------------------
#  Профили чипов (firmware/src/isp89.cpp — DEVS)
#
#  lock — байт, который чип отдаёт командой «Read Lock Bits» (0x24). Раскладка
#  у чипов РАЗНАЯ (Table 24-1 обоих даташитов), и это единственный источник истины:
#    8253 — «xxxx x LB3 LB2 LB1», запрограммирован = 0 (чистый чип FF);
#    S52  — «xxx LB3 LB2 LB1 xx»,  запограммирован = 1 (чистый чип 00).
#  Неопределённые биты (у 8253 биты 7…3, у S52 — 7…5 и 1…0) чип отдаёт как
#  попало: в логах они видны как 0x40, 0x50 и т. п. Их не читаем и ни на что
#  не опираемся — только на lock_lb.
# ---------------------------------------------------------------------------

PROFILES = {
    ISP_DEV_8253: dict(
        name="AT89S8253",
        signature=bytes([0x1E, 0x73]),
        flash=0x3000, page=64,
        eeprom=0x800, epage=32,
        extras=True,                 # user row и фьюзы есть
        lock_lb=(0x01, 0x02, 0x04),  # LB1, LB2, LB3 — биты 0…2
        lock_programmed=0,
    ),
    ISP_DEV_S52: dict(
        name="AT89S52",
        signature=bytes([0x1E, 0x52, 0x06]),
        flash=0x2000, page=256,
        eeprom=0, epage=0,
        extras=False,
        lock_lb=(0x04, 0x08, 0x10),  # LB1, LB2, LB3 — биты 2…4
        lock_programmed=1,
    ),
}

# EEPROM головы формирует эта программа целиком. Раскладка — по прошивке головы
# (zoomlamp-head-firmware-new/v2-with-chip/src/store.c и chiplist.c):
#   0x000–0x006  настройки: 'Z' 'L' версия LO MID HI сумма^0x5A   ← пишем
#   0x020–0x08F  журнал попыток записи коэффициентов              FF
#   0x090–0x0A0  ключ подписи 16 байт + контроль (сумма^0x3C)     ← пишем
#   0x0A4–0x0A6  указатель кольца серийных номеров чипов          FF (не трогаем)
#   0x100–0x1FF  кольцо наработки: 64 ячейки r0 r1 r2 (сумма^0xA5) ← пишем ячейку 0
#   0x200–0x7FE  кольцо серийных номеров: 307 записей по 5 байт   FF (не трогаем)
# Пустые (FF) указатель и записи прошивка «с чипом» читает как пустое кольцо
# и начинает его с записи 0 — поэтому область кольца остаётся FF.

# Настройки: коэффициенты мощности — константы, как COEF_DEFAULT_* в config.h
COEF_ADDR = 0x000
COEF_VERSION = 1
COEF_CHECK_XOR = 0x5A
COEF_LO, COEF_MID, COEF_HI = 50, 120, 190

# Ключ подписи чипов: все FF или неверный контроль — для головы ключа нет,
# и она берёт тестовый CHIP_TEST_KEY из config.h
KEY_ADDR = 0x090
KEY_SIZE = 16
KEY_CHECK_ADDR = 0x0A0
KEY_CHECK_XOR = 0x3C

# Наработка, минуты (3 байта). Голова берёт ячейку с наибольшим значением
RT_ADDR = 0x100
RT_CHECK_XOR = 0xA5
RUNTIME_MAX_MIN = 0xFFFFFF

# Области кольца серийных номеров (chiplist.c): программа их не заполняет
CHIPLIST_AREAS = ((0x0A4, 0x0A7), (0x200, 0x7FF))

HERE = os.path.dirname(os.path.abspath(__file__))
DUMP_DIR = os.path.join(HERE, "dumps")
KEYS_DIR = os.path.join(HERE, "keys")


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
    names = ["flash"]
    if d["eeprom"] is not None:
        names.append("eeprom")
    if d["user_sig"] is not None:
        names.append("user_sig")
    for name in names:
        with open(os.path.join(folder, f"{name}.bin"), "wb") as f:
            f.write(d[name])
    _write_hex(os.path.join(folder, "flash.hex"), d["flash"])
    if d["eeprom"] is not None:
        _write_hex(os.path.join(folder, "eeprom.hex"), d["eeprom"])
    with open(os.path.join(folder, "info.txt"), "w", encoding="utf-8") as f:
        f.write(f"{d['name']}, дамп {stamp}\n")
        f.write(f"Сигнатура: {hexs(d['signature'])}\n")
        f.write(f"Lock: {d['lock']:02X} — {describe_lock(d['lock'], d['dev'])}\n")
        if d["fuses"] is not None:
            f.write(f"Фьюзы: {d['fuses']:02X} — {describe_fuses(d['fuses'])}\n")
        f.write(f"Flash: {used(d['flash'])} Б не FF из {len(d['flash'])}\n")
        if d["eeprom"] is not None:
            f.write(f"EEPROM: {used(d['eeprom'])} Б не FF из {len(d['eeprom'])}\n")
        f.write("Чтение двойное, со сверкой.\n")
    log(f"Дамп сохранён: {folder}", "ok")
    return folder


# ---------------------------------------------------------------------------
#  Расшифровка
# ---------------------------------------------------------------------------

def used(data: bytes) -> int:
    return sum(b != 0xFF for b in data)


def lock_bits(lock: int, dev_id: int) -> dict:
    """{1: bool, 2: bool, 3: bool} — запрограммирован ли LB1/LB2/LB3.

    Читаются только биты, определённые даташитом (Table 24-1): у 8253 — 0…2,
    у S52 — 2…4. Остальные биты чип отдаёт произвольно (в логах от них 0x40,
    0x42, 0x50), поэтому в расчёт не идут.
    """
    prof = PROFILES[dev_id]
    prog = prof["lock_programmed"]
    return {n + 1: bool(lock & mask) == bool(prog)
            for n, mask in enumerate(prof["lock_lb"])}


def lock_mode(lock: int, dev_id: int) -> int:
    """Режим защиты по Table 17-1/19-1: 1 — без защиты, 2, 3 или 4.

    Режимы включаются строго по порядку, поэтому одиночный LB3 — не режим 4,
    а мусор в неопределённом бите: LB3 учитывается только вместе с LB1+LB2.
    """
    b = lock_bits(lock, dev_id)
    if not b[1]:
        return 1
    if not b[2]:
        return 2
    return 4 if b[3] else 3


def lock_readable(lock: int, dev_id: int) -> bool:
    """Содержимое можно прочитать. Режим 2 запрещает только программирование,
    чтение (verify) запрещено с режима 3 — когда запрограммирован LB2."""
    return lock_mode(lock, dev_id) <= 2


def lock_inconsistent(lock: int, dev_id: int) -> bool:
    """Биты не складываются в допустимый режим защиты.

    Режимы включаются строго по порядку: LB2 без LB1 и LB3 без LB1+LB2 даташит
    не предусматривает. Такое встречается, когда чип отдаёт мусор в неопределённых
    битах — например, у S52 в логах были 0x50 (один LB3) и 0x42 (ничего).
    """
    b = lock_bits(lock, dev_id)
    return (b[2] and not b[1]) or (b[3] and not (b[1] and b[2]))


def describe_lock(lock: int, dev_id: int) -> str:
    mode = lock_mode(lock, dev_id)
    if mode == 1:
        text = "защиты нет"
    elif mode == 2:
        text = "режим 2 (LB1) — чтение разрешено, запись запрещена"
    elif mode == 3:
        text = ("режим 3 (LB1+LB2) — чтение Flash запрещено"
                + (" и EEPROM" if PROFILES[dev_id]["extras"] else ""))
    else:
        text = "режим 4 (LB1+LB2+LB3) — чтение и внешнее выполнение запрещены"
    if lock_inconsistent(lock, dev_id):
        text += " [биты вне режима — неопределённые биты чипа]"
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

def describe_enable(enable) -> str:
    """Ответ чипа на Programming Enable — только для справки.

    Байт 0x69 отдают AVR и AT89LP; у AT89S8253/S52 подтверждения в этом месте
    нет: на проверенной голове, которая прошивается штатно, приходит 00.
    Поэтому ответ на Enable ни о чём не говорит и работой не управляет.
    None — прошивка Nano (до v7) этот байт не присылает.
    """
    return "нет данных" if enable is None else f"{enable:02X}"


def _open(dev: Device, log, choose_type=None):
    """Вход в ISP с определением типа чипа.

    Возвращает (тип, профиль, сигнатура, lock). Если сигнатура не читается
    (чип закрыт от чтения), тип спрашивают у оператора и открывают заново.
    """
    try:
        mode, sig, dev_id, lock, enable = dev.isp_open()
    except IspSignatureUnknown as e:
        # Сигнатуры нет: либо чип закрыт LB2/LB3 (у S52 сигнатура закрыта),
        # либо чип не в режиме программирования.
        log(f"Сигнатура не читается: {hexs(e.sig)}, lock {e.lock:02X}", "warn")
        if choose_type is None:
            raise Abort("Сигнатура не читается, тип чипа не определён")
        answer = choose_type(
            "Сигнатура чипа не читается — так бывает у закрытого от чтения "
            "контроллера.\n\n"
            "«Да» — AT89S8253 (голова ZoomLamp)\n"
            "«Нет» — AT89S52\n"
            "«Отмена» — прервать",
            "AT89S8253", "AT89S52")
        if answer is None:
            raise Abort("Тип чипа не выбран — работа отменена")
        dev_id = ISP_DEV_8253 if answer else ISP_DEV_S52
        mode, sig, dev_id, lock, enable = dev.isp_open(dev_id)
        log(f"Тип задан оператором: {PROFILES[dev_id]['name']}", "warn")
    prof = PROFILES[dev_id]
    log(f"Контроллер: {prof['name']} ({hexs(sig)}), SPI режим {mode}, "
        f"ответ на Prog Enable {describe_enable(enable)}", "head")
    log(f"Lock {lock:02X}: {describe_lock(lock, dev_id)}",
        None if lock_readable(lock, dev_id) else "warn")
    if prof["extras"]:
        fuses = dev.isp_read(ISP_FUSES, 0, 1)[0]
        log(f"Фьюзы {fuses:02X}: {describe_fuses(fuses)}")
    return dev_id, prof, sig, lock


def _read_space(dev: Device, space, size, page, progress, p0, p1) -> bytes:
    return dev.isp_read_block(space, 0, size, page,
                              lambda done: progress(p0 + (p1 - p0) * done / size))


def _read_all(dev: Device, prof, progress, p0=0.0, p1=1.0) -> dict:
    """Flash и EEPROM (если есть) — дважды со сверкой."""
    span = (p1 - p0) / 2
    reads = []
    for k in range(2):
        b = p0 + span * k
        flash = _read_space(dev, ISP_CODE, prof["flash"], prof["page"], progress, b, b + span * 0.85)
        eeprom = None
        if prof["eeprom"]:
            eeprom = _read_space(dev, ISP_DATA, prof["eeprom"], prof["epage"], progress,
                                 b + span * 0.85, b + span)
        reads.append((flash, eeprom))
    (f1, e1), (f2, e2) = reads
    if f1 != f2 or e1 != e2:
        nf = sum(a != b for a, b in zip(f1, f2))
        ne = sum(a != b for a, b in zip(e1, e2)) if e1 is not None else 0
        raise Abort(f"Два чтения не совпали: Flash {nf} Б, EEPROM {ne} Б — "
                    "плохой контакт прищепки или помехи. Дамп не сохранён")
    return {"flash": f1, "eeprom": e1,
            "atmel_sig": dev.isp_read(ISP_ATMEL, 0, 3),
            "user_sig": dev.isp_read(ISP_USER, 0, SIG_SIZE) if prof["extras"] else None}


def _dump(dev: Device, dev_id, prof, sig, lock, log, progress, p0, p1, suffix=""):
    d = _read_all(dev, prof, progress, p0, p1)
    d.update(name=prof["name"], dev=dev_id, signature=sig, lock=lock,
             fuses=dev.isp_read(ISP_FUSES, 0, 1)[0] if prof["extras"] else None)
    log(f"Flash: {used(d['flash'])} Б не FF" +
        (f", EEPROM: {used(d['eeprom'])} Б не FF" if d["eeprom"] is not None else ""))
    if not used(d["flash"]):
        log("Flash пуста (все FF)" + ("" if lock_readable(lock, dev_id)
                                      else " — или чтение запрещено lock"), "warn")
    return _save_dump(d, suffix, log)


# ---------------------------------------------------------------------------
#  Образ EEPROM: коэффициенты, наработка, ключ подписи
# ---------------------------------------------------------------------------

def coef_put(eeprom: bytearray):
    """Кладёт в образ EEPROM блок настроек с константами LO/MID/HI (как store_save_coef)."""
    blk = bytes([ord("Z"), ord("L"), COEF_VERSION, COEF_LO, COEF_MID, COEF_HI])
    eeprom[COEF_ADDR:COEF_ADDR + 6] = blk
    eeprom[COEF_ADDR + 6] = sum(blk) & 0xFF ^ COEF_CHECK_XOR


def runtime_put(eeprom: bytearray, minutes: int):
    """Наработка в ячейку 0 кольца (как store_save_runtime); остальные ячейки — FF."""
    r0, r1, r2 = minutes & 0xFF, (minutes >> 8) & 0xFF, (minutes >> 16) & 0xFF
    eeprom[RT_ADDR:RT_ADDR + 4] = bytes([r0, r1, r2, (r0 + r1 + r2) & 0xFF ^ RT_CHECK_XOR])


def build_eeprom(runtime_min: int, key: bytes) -> bytes:
    """Полный образ EEPROM головы: FF, а в нём настройки, ключ и наработка."""
    if not 0 <= runtime_min <= RUNTIME_MAX_MIN:
        raise Abort(f"Наработка {runtime_min} мин вне 0…{RUNTIME_MAX_MIN}")
    img = bytearray(b"\xFF" * PROFILES[ISP_DEV_8253]["eeprom"])
    coef_put(img)
    key_put(img, key)
    runtime_put(img, runtime_min)
    for a, b in CHIPLIST_AREAS:                      # кольцо серийных номеров не трогаем
        if any(x != 0xFF for x in img[a:b]):
            raise Abort(f"Образ EEPROM задевает кольцо номеров чипов 0x{a:03X}–0x{b - 1:03X}")
    return bytes(img)


def key_check(key: bytes) -> int:
    return sum(key) & 0xFF ^ KEY_CHECK_XOR


def key_new() -> bytes:
    """Новый случайный ключ, не совпадающий с уже выданными (файлы в pc/keys)."""
    issued = known_keys()
    while True:
        key = secrets.token_bytes(KEY_SIZE)
        if all(b == 0xFF for b in key):            # все FF — признак «ключа нет»
            continue
        if key.hex() in issued:
            continue
        return key


def key_put(eeprom: bytearray, key: bytes):
    """Кладёт ключ и контроль в образ EEPROM (образ правится на месте)."""
    eeprom[KEY_ADDR:KEY_ADDR + KEY_SIZE] = key
    eeprom[KEY_CHECK_ADDR] = key_check(key)


def known_keys() -> dict:
    """Ключи, уже выданные программой: {hex: путь} — по файлам в pc/keys."""
    out = {}
    if not os.path.isdir(KEYS_DIR):
        return out
    for name in os.listdir(KEYS_DIR):
        if not name.lower().endswith(".txt"):
            continue
        path = os.path.join(KEYS_DIR, name)
        try:
            with open(path, "rb") as f:
                k = f.read().strip()
        except OSError:
            continue
        if len(k) == KEY_SIZE * 2:
            out[k.decode("ascii", "ignore").lower()] = path
    return out


def _save_prepared(key: bytes, image: bytes, runtime_min: int, log) -> str:
    """Копия ключа (.txt — по ним ключ не повторяется) и образа EEPROM в pc/keys."""
    os.makedirs(KEYS_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.join(KEYS_DIR, f"key_{stamp}")
    n = 1
    while os.path.exists(base + ".txt"):            # ключ никогда не перезаписывается
        n += 1
        base = os.path.join(KEYS_DIR, f"key_{stamp}_{n}")
    with open(base + ".txt", "w", encoding="ascii") as f:
        f.write(key.hex().upper() + "\n")
    with open(base + "_eeprom.bin", "wb") as f:
        f.write(image)
    with open(base + "_info.txt", "w", encoding="utf-8") as f:   # не ключ: длина не 32
        f.write(f"Образ EEPROM головы, {stamp}\n"
                f"Коэффициенты: LO {COEF_LO}, MID {COEF_MID}, HI {COEF_HI}\n"
                f"Наработка: {runtime_min} мин ({runtime_min / 60:.1f} ч)\n"
                f"Ключ подписи: {key.hex().upper()}\n")
    log(f"Ключ и образ EEPROM сохранены: {base}.txt, {base}_eeprom.bin", "ok")
    return base


# ---------------------------------------------------------------------------
#  Операции
# ---------------------------------------------------------------------------

def read_status(dev: Device, log, choose_type=None):
    log("Состояние AT89", "head")
    _open(dev, log, choose_type)
    dev.isp_close()


def dump(dev: Device, log, progress, choose_type=None):
    log("Дамп AT89", "head")
    t0 = time.monotonic()
    dev_id, prof, sig, lock = _open(dev, log, choose_type)
    try:
        if not lock_readable(lock, dev_id):
            log("LB2 запрограммирован: прочитается не содержимое чипа. "
                "Дамп снимается для отчёта", "warn")
        _dump(dev, dev_id, prof, sig, lock, log, progress, 0.0, 1.0)
    finally:
        dev.isp_close()
    log(f"Готово за {time.monotonic() - t0:.1f} с", "ok")


def _pages(data: bytes, page: int):
    """(адрес, страница) без страниц из одних FF — после стирания они и так FF."""
    size = (len(data) + page - 1) // page * page
    data = data + b"\xFF" * (size - len(data))
    return [(a, data[a:a + page]) for a in range(0, size, page) if data[a:a + page] != b"\xFF" * page]


def _write_page(dev: Device, prof, space, addr, page: bytes):
    """Запись области Flash/EEPROM.

    S52 — побайтово командой 0x40: страничный режим (0x50 + 256 байт) на живом
    чипе данных не записал (проверено 2026-11-19), а рабочий проект at89s-prog
    для S52 использует только байтовый режим. Запись идёт без страниц.
    8253 — страницами: страничный режим у неё проверен.
    """
    if prof["page"] > 128:                          # S52: страница 256 Б
        for i, v in enumerate(page):
            if v != 0xFF:                           # FF после стирания и так FF
                dev.isp_write_byte(space, addr + i, v)
    else:
        dev.isp_write_page(space, addr, page)


def program(dev: Device, flash: bytes, log, progress, confirm,
            choose_type=None, ask_runtime=None):
    """Прошивка AT89 любого из двух типов.

    8253 (голова): Flash из файла, EEPROM формирует программа (build_eeprom) —
    коэффициенты 50/120/190, наработка (спрашивается у оператора) и новый ключ
    подписи. Ключ при каждой прошивке новый: Chip Erase стирает прежний, а из
    защищённой (LB2) головы его не прочитать.
    S52: только Flash 8 КБ — EEPROM, ключа и наработки у него нет.
    """
    log("Прошивка AT89", "head")
    if not used(flash):
        raise Abort("В файле Flash нет данных")
    t0 = time.monotonic()
    dev_id, prof, sig, lock = _open(dev, log, choose_type)
    try:
        if len(flash) > prof["flash"]:
            raise Abort(f"Файл {len(flash)} Б больше Flash {prof['name']} ({prof['flash']} Б)")
        if prof["extras"]:
            fuses = dev.isp_read(ISP_FUSES, 0, 1)[0]
            if fuses & 1:
                log("Фьюз ISP выключен: после стирания чип может не войти в ISP", "warn")

        # Оригинал не теряем: перед стиранием — дамп
        if lock_readable(lock, dev_id):
            log("Резервный дамп перед стиранием")
            _dump(dev, dev_id, prof, sig, lock, log, progress, 0.0, 0.3, "_before_write")
        else:
            warn = ("Ключ подписи чипов будет потерян — привязанные этой головой "
                    "световоды станут для неё IMPROPER. Будет выдан новый ключ.\n\n"
                    if dev_id == ISP_DEV_8253 else "")
            if not confirm(f"{prof['name']} защищён от чтения (LB2). Снять дамп нельзя, "
                           "стирание безвозвратно уничтожит оригинальную прошивку.\n\n"
                           f"{warn}Стереть?"):
                raise Abort("Прошивка отменена: чип защищён, дамп не снят")
        progress(0.3)

        # EEPROM головы (8253): ключ всегда новый — прежний из закрытого чипа не прочитать
        eeprom = None
        if prof["extras"]:
            if ask_runtime is None:
                raise Abort("Не задана наработка головы")
            runtime_min = ask_runtime()
            if runtime_min is None:
                raise Abort("Прошивка отменена: наработка не задана")
            key = key_new()
            eeprom = build_eeprom(runtime_min, key)
            log(f"EEPROM: коэффициенты LO {COEF_LO}, MID {COEF_MID}, HI {COEF_HI}; "
                f"наработка {runtime_min} мин ({runtime_min / 60:.1f} ч)")
            log(f"Ключ подписи чипов (новый): {key.hex().upper()}", "head")
            _save_prepared(key, eeprom, runtime_min, log)

        # Заблокировать после прошивки? По умолчанию — нет
        want_lock = confirm(
            f"Заблокировать {prof['name']} от чтения после прошивки?\n\n"
            "Будет установлен lock LB1+LB2: Flash нельзя будет прочитать, "
            "снять защиту можно только полным стиранием.\n\n"
            "«Нет» — чип останется открытым (рекомендуется для отладки).")
        if want_lock:
            log("После прошивки и сверки будет установлен lock LB1+LB2", "warn")

        log(f"Тип контроллера: {prof['name']} ({hexs(sig)}), Flash {prof['flash']} Б, "
            f"страница {prof['page']} Б", "head")
        log(f"Начало прошивки: {prof['name']}, Flash {used(flash)} Б"
            + (f", EEPROM {used(eeprom)} Б" if eeprom else ""), "head")

        dev.isp_erase()
        # Проверка: начало Flash пустое (длина запроса — один байт, максимум 128)
        head = dev.isp_read(ISP_CODE, 0, min(prof["page"], 128))
        if head != b"\xFF" * len(head):
            raise Abort("После стирания Flash не пуста")
        log("Чип стёрт")

        fpages = _pages(flash, prof["page"])
        epages = _pages(eeprom, prof["epage"]) if eeprom else []
        total = len(fpages) + len(epages)
        by_byte = prof["page"] > 128                 # S52 — побайтовая запись
        for i, (a, p) in enumerate(fpages):
            _write_page(dev, prof, ISP_CODE, a, p)
            progress(0.3 + 0.35 * (i + 1) / total)
        log(f"Flash записана: {used(flash)} Б, "
            + (f"байтов {used(flash)}" if by_byte else f"страниц {len(fpages)}"))
        for i, (a, p) in enumerate(epages):
            _write_page(dev, prof, ISP_DATA, a, p)
            progress(0.3 + 0.35 * (len(fpages) + i + 1) / total)
        if epages:
            log(f"EEPROM записана: страниц {len(epages)}")

        # Сверка всей памяти: в незаписанных местах должно быть FF
        back = _read_space(dev, ISP_CODE, prof["flash"], prof["page"], progress, 0.65, 0.92)
        want = flash + b"\xFF" * (prof["flash"] - len(flash))
        bad = [i for i in range(prof["flash"]) if back[i] != want[i]]
        if bad:
            raise Abort(f"Сверка Flash: не совпало {len(bad)} Б, первый по адресу 0x{bad[0]:04X} "
                        f"(записано {want[bad[0]]:02X}, прочитано {back[bad[0]]:02X})")
        if eeprom:
            eback = _read_space(dev, ISP_DATA, prof["eeprom"], prof["epage"], progress, 0.92, 1.0)
            bad = [i for i in range(prof["eeprom"]) if eback[i] != eeprom[i]]
            if bad:
                raise Abort(f"Сверка EEPROM: не совпало {len(bad)} Б, первый по адресу 0x{bad[0]:03X}")
        log("Сверка: совпадает", "ok")

        # --- lock: только после успешной сверки ---
        if want_lock:
            dev.isp_lock(3)
            dev.isp_close()
            # Переоткрываем с тем же типом: сигнатура у закрытого чипа может не читаться
            _, sig2, dev_id2, lock2, _ = dev.isp_open(dev_id)
            mode2 = lock_mode(lock2, dev_id)
            if mode2 < 3:
                raise Abort(f"Lock записан, но встал режим {mode2}, а не 3 "
                            f"(lock {lock2:02X}) — чип по-прежнему читается")
            # Режимы 3 и 4 отключают verify — но каким именно мусором чип отвечает
            # на чтение, даташит не говорит, и сигнатура у S52 продолжает читаться
            # (проверено 2026-11-19: lock 4E — режим 3, сигнатура 1E 52 06 на месте).
            # Поэтому проверяем по делу: начало записанного не должно прочитаться
            # таким, каким его записали.
            # Читаем одиночным CMD_ISP_READ, а не блоком: блок требует total,
            # кратный странице (у S52 — 256), а длина запроса здесь не больше 128.
            probe = flash[:min(len(flash), 128)]
            if probe and dev.isp_read(ISP_CODE, 0, len(probe)) == probe:
                raise Abort(f"Lock {lock2:02X} записан, но Flash всё ещё читается — "
                            "защита не встала")
            log(f"Lock {lock2:02X}: {describe_lock(lock2, dev_id)} — защита встала "
                f"(чтение Flash отключено, сигнатура {hexs(sig2)} отдаётся)", "ok")
        else:
            log("Lock не ставится: чип оставлен открытым")
    finally:
        dev.isp_close()
    progress(1.0)
    log(f"Прошивка завершена за {time.monotonic() - t0:.1f} с", "ok")
