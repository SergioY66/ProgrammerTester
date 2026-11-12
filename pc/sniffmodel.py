"""Модель захвата сниффера 1-Wire (pc/sniffer/sniff_*/raw.bin).

Поток записей описан в firmware/include/sniff.h. Абсолютное время в потоке
есть только у сбросов («мс от старта»), у слотов его нет. Поэтому начало
транзакции известно точно, а слоты внутри неё раскладываются равномерно
между соседними сбросами: границы точны, положение отдельного бита —
интерполировано (погрешность — десятки микросекунд).

Роли байтов (команда мастера / данные) определяются по протоколу, как в
sniffdecode.py.
"""

import bisect
import os

from sniffdecode import ROM_CMDS, FUNC_CMDS

_LEN = {0x90: 7, 0x91: 3, 0x92: 1, 0x93: 3, 0x94: 4, 0x95: 2, 0x96: 3, 0x97: 2}

PRESENCE_DELAY = 15.0     # мкс: пауза мастера перед импульсом присутствия
SLOT_NOMINAL = 205.0      # мкс: резервная оценка периода слота


def _val(bs):
    v = 0
    for i, b in enumerate(bs):
        v |= (b & 0x7F) << (7 * i)
    return v


class Atom:
    """Один непрерывный участок линии: сброс, присутствие, слот, пауза."""

    __slots__ = ("kind", "t0", "t1", "bit", "low", "role", "byte", "flags", "tx")

    def __init__(self, kind, t0, t1, bit=None, low=None, role="sys",
                 byte=None, flags=0, tx=None):
        self.kind = kind          # reset | presence | bit | long | gap
        self.t0 = t0
        self.t1 = t1
        self.bit = bit
        self.low = low            # длительность нуля, мкс
        self.role = role          # cmd | data | sys
        self.byte = byte
        self.flags = flags
        self.tx = tx


class Transaction:
    __slots__ = ("fall", "reset_us", "presence_us", "items", "atoms", "t_start",
                 "t_end", "bytes", "byte_spans", "n_bits", "has_cmd")

    def __init__(self, fall_us):
        self.fall = fall_us
        self.reset_us = 0.0
        self.presence_us = None   # None — не сообщалось, 0 — присутствия нет
        self.items = []           # исходный порядок: (kind, a, b, c)
        self.atoms = []
        self.byte_spans = []      # (t0, t1, значение, роль, имя)
        self.bytes = []
        self.t_start = self.t_end = fall_us
        self.n_bits = 0
        self.has_cmd = False


def _records(data):
    """Поток → (заголовок, полезная нагрузка); у слота нагрузка — длительность нуля."""
    i, n = 0, len(data)
    while i < n:
        h = data[i]
        if 0x80 <= h <= 0x87:
            if i + 1 >= n:
                return
            yield h, data[i + 1]
            i += 2
            continue
        k = _LEN.get(h)
        if k is None:                     # потеря синхронизации
            i += 1
            continue
        if i + k > n:
            return
        yield h, bytes(data[i + 1:i + k])
        i += k


def _analyze(bs):
    """[(роль, имя)] по байтам: команды мастера называются, данные — только код."""
    roles = [("data", None)] * len(bs)
    if not bs:
        return roles
    c = bs[0]
    roles[0] = ("cmd", ROM_CMDS.get(c, "неизвестна"))
    pos = 1
    if c in (0x33, 0x55):                 # READ ROM / MATCH ROM — 8 байт адреса
        pos = 9
    elif c in (0xF0, 0xEC):               # поиск ROM дальше не расшифровываем
        return roles
    if pos < len(bs):
        f = bs[pos]
        roles[pos] = ("cmd", FUNC_CMDS.get(f, "неизвестна"))
    return roles


def _extra(t):
    """Известное время внутри транзакции, кроме слотов, мкс."""
    s = sum(it[1] for it in t.items if it[0] != "bit")
    if t.presence_us:
        s += PRESENCE_DELAY
    return s


def _slot_estimate(txns):
    est = []
    for i in range(len(txns) - 1):
        t = txns[i]
        nb = sum(1 for it in t.items if it[0] == "bit")
        if not nb:
            continue
        rest = txns[i + 1].fall - t.fall - t.reset_us - _extra(t)
        if rest > 0:
            est.append(rest / nb)
    if not est:
        return SLOT_NOMINAL
    est.sort()
    return est[len(est) // 2]


def _link_bytes(t):
    bits = [a for a in t.atoms if a.kind == "bit"]
    nb = len(bits) // 8
    bs = [sum(bits[8 * k + j].bit << j for j in range(8)) for k in range(nb)]
    roles = _analyze(bs)
    t.bytes = bs
    spans = []
    for k in range(nb):
        role, name = roles[k]
        spans.append((bits[8 * k].t0, bits[8 * k + 7].t1, bs[k], role, name))
        for j in range(8):
            bits[8 * k + j].role = role
        if role == "cmd":
            t.has_cmd = True
    extra = len(bits) - 8 * nb
    if extra:                            # неполный байт — показываем как данные
        v = 0
        for j in range(extra):
            v |= bits[8 * nb + j].bit << j
        spans.append((bits[8 * nb].t0, bits[-1].t1, v, "data", None))
        for j in range(8 * nb, len(bits)):
            bits[j].role = "data"
    t.byte_spans = spans


def _assign(txns, p_nom):
    for i, t in enumerate(txns):
        nb = sum(1 for it in t.items if it[0] == "bit")
        p = p_nom
        if nb and i + 1 < len(txns):
            rest = txns[i + 1].fall - t.fall - t.reset_us - _extra(t)
            q = rest / nb
            # берём фактический период, если он правдоподобен; иначе — номинал
            if 0 < q < 1.6 * p_nom:
                p = q
        cur = t.fall
        t.t_start = cur
        atoms = [Atom("reset", cur, cur + t.reset_us, tx=t)]
        cur += t.reset_us
        if t.presence_us:
            cur += PRESENCE_DELAY
            atoms.append(Atom("presence", cur, cur + t.presence_us, tx=t))
            cur += t.presence_us
        bi = 0
        for kind, a, b, c in t.items:
            if kind == "bit":
                atoms.append(Atom("bit", cur, cur + p, bit=a, low=b, flags=c,
                                  byte=bi // 8, tx=t))
                cur += p
                bi += 1
            else:
                us = float(a)
                atoms.append(Atom(kind, cur, cur + us, tx=t))
                cur += us
        t.atoms = atoms
        t.t_end = cur
        t.n_bits = bi
        _link_bytes(t)


class Capture:
    """Готовый захват: транзакции, плоский список атомов, поиск по времени."""

    def __init__(self, path, txns, p_slot, lost_events, lost_bytes, level):
        self.path = path
        self.txns = txns
        self.p_slot = p_slot
        self.lost_events = lost_events
        self.lost_bytes = lost_bytes
        self.level = level
        self.txn_starts = [t.t_start for t in txns]
        self.flat = [a for t in txns for a in t.atoms]
        self.starts = [a.t0 for a in self.flat]
        self.total = max((t.t_end for t in txns), default=0.0)
        self.n_bits = sum(t.n_bits for t in txns)
        self.n_cmd = sum(1 for t in txns for s in t.byte_spans if s[3] == "cmd")

    def info_at(self, t):
        if not self.flat:
            return None
        i = max(0, bisect.bisect_right(self.starts, t) - 1)
        for k in range(max(0, i - 1), min(len(self.flat), i + 3)):
            a = self.flat[k]
            if a.t0 <= t <= a.t1:
                return a
        return None


def parse(data, path=""):
    txns, cur = [], None
    lost = lost_bytes = 0
    level = 1
    for h, payload in _records(data):
        if 0x80 <= h <= 0x87:
            if cur is not None:
                cur.items.append(("bit", h & 1, payload, h & 6))
            continue
        if h == 0x90:
            r = _val(payload[0:2])
            ms = _val(payload[2:6])
            cur = Transaction(ms * 1000.0 - r)
            cur.reset_us = float(r)
            txns.append(cur)
        elif cur is None:
            continue
        elif h == 0x91:
            cur.presence_us = float(_val(payload))
        elif h == 0x92:
            cur.presence_us = 0.0
        elif h == 0x93:
            cur.items.append(("long", _val(payload), None, 0))
        elif h == 0x94:
            cur.items.append(("gap", _val(payload), None, 0))
        elif h == 0x95:
            lost += _val(payload)
        elif h == 0x96:
            lost_bytes += _val(payload)
        elif h == 0x97:
            level = _val(payload)

    p_slot = _slot_estimate(txns)
    _assign(txns, p_slot)
    return Capture(path, txns, p_slot, lost, lost_bytes, level)


SESSION_GAP_US = 50_000     # пауза длиннее этой — граница между сессиями


def _agg(vals):
    if not vals:
        return None
    return {"n": len(vals), "min": min(vals), "max": max(vals),
            "mean": sum(vals) / len(vals)}


def compute_stats(cap):
    """Средние длительности по измеренным данным (не по интерполяции).

    «1»/«0» — длительность низкого уровня слота, как её измерил сниффер.
    «0» делится на запись мастера и ответ чипа по ролям байтов протокола.
    Паузы — высокий уровень дольше 1 мс в конце транзакции; более короткие
    сниффер не измеряет, их в статистике нет (поле no_gap).
    """
    from sniffdecode import SniffDecoder
    analyze = SniffDecoder()._analyze
    one, zero_m, zero_c, zero_u = [], [], [], []
    resets, presence, between_cmd, between_sess = [], [], [], []
    no_gap = 0
    for t in cap.txns:
        resets.append(t.reset_us)
        if t.presence_us:
            presence.append(t.presence_us)
        roles, _ = analyze(t.bytes)
        k = 0
        for kind, a, b, c in t.items:
            if kind != "bit":
                continue
            role = roles[k // 8] if k // 8 < len(roles) else "?"
            k += 1
            if a == 1:
                one.append(b)
            else:
                {"M": zero_m, "C": zero_c}.get(role, zero_u).append(b)
        last = t.items[-1] if t.items else None
        if last and last[0] == "gap":
            (between_sess if last[1] > SESSION_GAP_US else between_cmd).append(last[1])
        else:
            no_gap += 1
    return {"one": _agg(one), "zero_master": _agg(zero_m), "zero_chip": _agg(zero_c),
            "zero_unknown": _agg(zero_u), "reset": _agg(resets),
            "presence": _agg(presence), "between_cmd": _agg(between_cmd),
            "between_sess": _agg(between_sess), "no_gap": no_gap,
            "txns": len(cap.txns)}


def parse_file(path):
    if os.path.isdir(path):
        path = os.path.join(path, "raw.bin")
    with open(path, "rb") as f:
        return parse(f.read(), path)
