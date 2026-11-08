"""Расшифровка потока сниффера 1-Wire (запись — firmware/include/sniff.h).

Сниффер не различает, кто передаёт бит (голова или чип), поэтому роли байтов
определяются по протоколу DS2431-подобного чипа Zoom: ROM-команда, затем
функция. Тайминги — длительность нуля слота в мкс (по ней «1» отличается от «0»).
"""

import time

ROM_CMDS = {0x33: "READ ROM", 0xCC: "SKIP ROM", 0x55: "MATCH ROM", 0xF0: "SEARCH ROM",
            0xEC: "ALARM SEARCH", 0xA5: "RESUME"}
FUNC_CMDS = {0x0F: "WRITE SCRATCHPAD", 0xAA: "READ SCRATCHPAD",
             0x55: "COPY SCRATCHPAD", 0xF0: "READ MEMORY"}

# длина записи по заголовку; None — пока неизвестна
_LEN = {0x90: 7, 0x91: 3, 0x92: 1, 0x93: 3, 0x94: 4, 0x95: 2, 0x96: 3, 0x97: 2}

IDLE_S = 0.3          # пауза, после которой транзакция считается законченной


def _hex(bs):
    return " ".join(f"{b:02X}" for b in bs)


def _val(bs):
    v = 0
    for i, b in enumerate(bs):
        v |= (b & 0x7F) << (7 * i)
    return v


def _stat(lows):
    if not lows:
        return None
    return f"{min(lows)}–{max(lows)} (ср. {sum(lows) / len(lows):.1f}) мкс, n={len(lows)}"


class Transaction:
    def __init__(self, reset_us, ms):
        self.reset_us = reset_us
        self.ms = ms
        self.presence = "?"          # int мкс, "нет" или "?"
        self.slots = []              # (бит, ноль мкс, флаги)
        self.longs = []              # нули 120–380 мкс
        self.gaps = []               # паузы > 1 мс, мкс
        self.touched = time.monotonic()


class SniffDecoder:
    def __init__(self, detail=False):
        self.detail = detail
        self.buf = bytearray()
        self.txn = None
        self.count = 0
        self.lost_events = 0
        self.lost_bytes = 0

    # --- ввод потока ---

    def feed(self, data):
        """Принимает байты потока, возвращает список готовых текстовых блоков."""
        out = []
        self.buf += data
        while self.buf:
            h = self.buf[0]
            if h < 0x80:                              # не заголовок — потеря синхронизации
                del self.buf[0]
                continue
            if 0x80 <= h <= 0x87:
                n = 2
            else:
                n = _LEN.get(h)
                if n is None:                         # неизвестный заголовок
                    del self.buf[0]
                    continue
            if len(self.buf) < n:
                break
            rec = bytes(self.buf[:n])
            del self.buf[:n]
            msg = self._record(rec)
            if msg:
                out.append(msg)
        return out

    def idle(self):
        """Завершает транзакцию, если линия молчит дольше IDLE_S."""
        t = self.txn
        if t and t.slots and time.monotonic() - t.touched > IDLE_S:
            return [self._finish()]
        return []

    def flush(self):
        """Принудительно завершает текущую транзакцию (после остановки)."""
        if self.txn and (self.txn.slots or self.txn.presence != "?"):
            return [self._finish()]
        self.txn = None
        return []

    # --- записи ---

    def _record(self, r):
        h = r[0]
        if 0x80 <= h <= 0x87:
            if self.txn is not None:
                self.txn.slots.append((h & 1, r[1], h & 6))
                self.txn.touched = time.monotonic()
            return None
        if h == 0x90:
            done = self._finish() if self.txn and (self.txn.slots or self.txn.presence != "?") else None
            self.txn = Transaction(_val(r[1:3]), _val(r[3:7]))
            return done
        if h == 0x91:
            if self.txn:
                self.txn.presence = _val(r[1:3])
            return None
        if h == 0x92:
            if self.txn:
                self.txn.presence = "нет"
            return None
        if h == 0x93:
            if self.txn:
                self.txn.longs.append(_val(r[1:3]))
            return None
        if h == 0x94:
            if self.txn:
                self.txn.gaps.append(_val(r[1:4]))
                self.txn.touched = time.monotonic()
            return None
        if h == 0x95:
            self.lost_events += r[1]
            return f"!! ПОТЕРЯНО событий захвата: {r[1]} (всего {self.lost_events})"
        if h == 0x96:
            n = _val(r[1:3])
            self.lost_bytes += n
            return f"!! ПОТЕРЯНО байт вывода: {n} (всего {self.lost_bytes})"
        if h == 0x97:
            return ("Сниффер запущен. Линия DQ: высокий уровень (подтяжка есть)." if r[1]
                    else "Сниффер запущен. Линия DQ: НИЗКИЙ уровень — нет питания/подтяжки "
                         "(голова выключена? переходник не подключён?)")
        return None

    # --- расшифровка транзакции ---

    def _finish(self):
        t, self.txn = self.txn, None
        self.count += 1
        head = f"#{self.count} @ {t.ms} мс   сброс {t.reset_us} мкс, присутствие "
        head += f"{t.presence} мкс" if isinstance(t.presence, int) else ("НЕТ" if t.presence == "нет" else "—")
        lines = [head]

        bits = [s[0] for s in t.slots]
        nbytes = len(bits) // 8
        bs = [sum(bits[8 * i + k] << k for k in range(8)) for i in range(nbytes)]
        extra = len(bits) - 8 * nbytes
        roles, notes = self._analyze(bs)
        lines += ["  " + n for n in notes]
        if extra:
            lines.append(f"  незавершённый байт: {extra} бит ({''.join(map(str, bits[8 * nbytes:]))})")
        if not t.slots:
            lines.append("  слотов нет")

        # тайминги по ролям: M — мастер пишет, C — чип отвечает
        groups = {}
        for idx, (bit, low, fl) in enumerate(t.slots):
            k = idx // 8
            role = roles[k] if k < len(roles) else "?"
            groups.setdefault((role, bit), []).append(low)
        names = {"M": "мастер пишет", "C": "чип отвечает (слот чтения)", "?": "роль не определена"}
        for role in ("M", "C", "?"):
            for bit in (1, 0):
                s = _stat(groups.get((role, bit), []))
                if s:
                    lines.append(f"  ноль «{bit}», {names[role]}: {s}")
        refall = sum(1 for s in t.slots if s[2] & 2)
        if refall:
            lines.append(f"  слотов, где чип прижал линию после отпускания головы: {refall}")
        if t.longs:
            lines.append(f"  длинные нули (120–380 мкс): {t.longs}")
        if t.gaps:
            lines.append("  паузы > 1 мс, мкс: " + ", ".join(str(g) for g in t.gaps))
        if self.detail:
            row = []
            for i, (bit, low, fl) in enumerate(t.slots):
                row.append(f"{bit}/{low}" + ("*" if fl & 2 else ""))
                if len(row) == 16 or i == len(t.slots) - 1:
                    lines.append("    слоты (бит/ноль мкс, * — повторный спад): " + " ".join(row))
                    row = []
        return "\n".join(lines)

    def _analyze(self, bs):
        """(роли байтов 'M'/'C', строки расшифровки)."""
        roles = ["M"] * len(bs)
        notes = []
        if not bs:
            return roles, notes
        c = bs[0]
        notes.append(f"ROM-команда {c:02X} — {ROM_CMDS.get(c, 'неизвестна')}")
        pos = 1

        def seg(n, role):
            nonlocal pos
            chunk = bs[pos:pos + n]
            for k in range(pos, min(pos + n, len(bs))):
                roles[k] = role
            pos += n
            return chunk

        if c == 0x33:
            notes.append("ROM чипа (чип→голова): " + _hex(seg(8, "C")))
        elif c == 0x55:
            notes.append("ROM адресата (голова→чип): " + _hex(seg(8, "M")))
        elif c in (0xF0, 0xEC):
            notes.append("поиск ROM — дальше не расшифровывается")
            return roles, notes
        if pos >= len(bs):
            return roles, notes

        f = bs[pos]
        notes.append(f"функция {f:02X} — {FUNC_CMDS.get(f, 'неизвестна')}")
        pos += 1
        if f == 0xF0:
            a = seg(2, "M")
            if len(a) == 2:
                notes.append(f"адрес {a[1] << 8 | a[0]:04X}")
            data = seg(len(bs) - pos, "C")
            self._dump(notes, "данные чипа", data)
        elif f == 0x0F:
            a = seg(2, "M")
            if len(a) == 2:
                notes.append(f"адрес {a[1] << 8 | a[0]:04X}")
            self._dump(notes, "данные голова→чип", seg(8, "M"))
            crc = seg(2, "C")
            if crc:
                notes.append("CRC16 от чипа: " + _hex(crc))
        elif f == 0xAA:
            ta = seg(3, "C")
            if ta:
                notes.append("TA1 TA2 E/S (чип): " + _hex(ta))
            self._dump(notes, "данные scratchpad (чип)", seg(8, "C"))
            crc = seg(2, "C")
            if crc:
                notes.append("CRC16 от чипа: " + _hex(crc))
        elif f == 0x55:
            ta = seg(3, "M")
            if ta:
                notes.append("TA1 TA2 E/S (голова): " + _hex(ta))
            conf = seg(1, "C")
            if conf:
                notes.append(f"подтверждение чипа: {conf[0]:02X}")
        rest = bs[pos:]
        if rest:
            self._dump(notes, "остаток", rest)
        return roles, notes

    @staticmethod
    def _dump(notes, title, data):
        if not data:
            return
        notes.append(f"{title} ({len(data)} байт):")
        for i in range(0, len(data), 16):
            notes.append("    " + _hex(data[i:i + 16]))
