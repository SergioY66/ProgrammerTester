"""Визуализатор захвата сниффера 1-Wire (pc/sniffer/sniff_*/raw.bin).

Верхняя шкала — вся сессия (старт: 2 с на экране), колесо мыши — масштаб,
слайдер внизу — прокрутка. Правая кнопка: первый клик — начало интервала,
второй — конец; интервал и его длительность подписываются. Левая кнопка
выделяет окно, которое разворачивается на нижней шкале.

Команды мастера — красным и со словами, данные — синим и только кодом.
Положение слота внутри транзакции интерполировано между метками сбросов
(в потоке сниффера у слотов нет собственного времени).
"""

import bisect
import math
import os
import sys
import tkinter as tk
from tkinter import filedialog, ttk

import sniffmodel

CMD_COLOR = "#c62828"
DATA_COLOR = "#1565c0"
SYS_COLOR = "#616161"
GRID_COLOR = "#e4e4e4"
WAVE_COLOR = "#37474f"
SEL_COLOR = "#ffe0b2"
MEAS_COLOR = "#6a1b9a"
BG = "#ffffff"

MONO = ("Consolas", 8)
MONO_B = ("Consolas", 8, "bold")
SMALL = ("Segoe UI", 8)

MIN_SPAN = 4.0          # мкс — предел увеличения
DEFAULT_SPAN = 2e6      # 2 с — начальный вид


def fmt_dur(us):
    if us >= 1e6:
        return f"{us / 1e6:.6g} с"
    if us >= 1000:
        return f"{us / 1000:.6g} мс"
    return f"{us:.6g} мкс"


def nice_step(span, target):
    raw = span / max(1, target)
    exp = 10 ** _log10(raw)
    for m in (1, 2, 5, 10):
        if raw <= m * exp:
            return m * exp
    return 10 * exp


def _log10(v):
    return math.floor(math.log10(v)) if v > 0 else 0


def tick_label(v, step):
    if step >= 1e6:
        return f"{v / 1e6:.6g} с"
    if step >= 1000:
        return f"{v / 1000:.6g} мс"
    return f"{v:.0f} мкс"


class Timeline:
    """Полоса времени: линейка, осциллограмма DQ и коды байтов в окне [t0, t1]."""

    def __init__(self, cap, width, height, ruler_h=22, wave_h=54, byte_h=30,
                 bit_h=0, interactive=True):
        self.cap = cap
        self.width = width
        self.height = height
        self.ruler_h = ruler_h
        self.wave_h = wave_h
        self.byte_h = byte_h
        self.bit_h = bit_h
        self.name_h = 12
        self.interactive = interactive
        self.y_wave = ruler_h
        self.y_name = self.y_wave + wave_h
        self.y_byte = self.y_name + self.name_h
        self.y_bit = self.y_byte + byte_h
        self.layout(height)
        # побайтовые спаны всех транзакций, отсортированы по времени
        self.spans = [s + (t,) for t in (cap.txns if cap else [])
                      for s in t.byte_spans]
        self.spans.sort(key=lambda s: s[0])
        self.span_starts = [s[0] for s in self.spans]
        # первый бит байта → (код, роль, название): подпись над битовой дорожкой
        self.byte_at = {}
        for t in (cap.txns if cap else []):
            for s0, s1, val, role, name in t.byte_spans:
                self.byte_at.setdefault(s0, (val, role, name))

    # --- отрисовка ---

    def layout(self, height):
        """Раскладывает полосы по высоте холста."""
        self.height = height
        free = height - self.ruler_h - self.byte_h - self.name_h
        if self.bit_h:
            self.wave_h = 54
            self.bit_h = max(34, min(140, free - self.wave_h))
        else:
            self.wave_h = max(40, free - 4)
        self.y_wave = self.ruler_h
        self.y_name = self.y_wave + self.wave_h
        self.y_byte = self.y_name + self.name_h
        self.y_bit = self.y_byte + self.byte_h

    def draw(self, c, t0, t1):
        c.delete("all")
        if t1 <= t0 or not self.cap or not self.cap.flat:
            return
        W = self.width
        dt = (t1 - t0) / W
        self._ruler(c, t0, t1, dt)
        self._wave(c, t0, t1, dt, W)
        self._bytes(c, t0, t1, dt, W)
        if self.bit_h:
            self._bits(c, t0, t1, dt, W)

    def _ruler(self, c, t0, t1, dt):
        y = self.ruler_h
        c.create_line(0, y, self.width, y, fill="#bdbdbd")
        step = nice_step(t1 - t0, max(2, self.width // 110))
        first = (int(t0 // step) + 1) * step
        v = first
        while v < t1:
            x = (v - t0) / dt
            c.create_line(x, 0, x, self.height, fill=GRID_COLOR, dash=(1, 3))
            c.create_line(x, y - 5, x, y, fill="#757575")
            c.create_text(x + 2, 2, text=tick_label(v, step), anchor="nw",
                          font=SMALL, fill="#424242")
            v += step

    def _wave(self, c, t0, t1, dt, W):
        """Ступенчатая форма DQ; при плотности выше пикселя — закрашенный столбец."""
        y_hi = self.y_wave + 6
        y_lo = self.y_wave + self.wave_h - 6
        c.create_line(0, y_lo, W, y_lo, fill="#f0f0f0")
        cats = self._columns(t0, dt, W)
        x = 0
        prev = None
        while x < W:
            k = cats[x]
            x2 = x
            while x2 + 1 < W and cats[x2 + 1] == k:
                x2 += 1
            if k == 2:
                c.create_rectangle(x, y_hi, x2 + 1, y_lo, fill="#90a4ae", outline="")
            else:
                y = y_hi if k == 0 else y_lo
                c.create_line(x, y, x2 + 1, y, fill=WAVE_COLOR)
                if prev is not None and prev != k:
                    c.create_line(x, y_hi, x, y_lo, fill=WAVE_COLOR)
                prev = k
            x = x2 + 1

    def _columns(self, t0, dt, W):
        """Категория колонки: 0 — высокий, 1 — ноль, 2 — фронты (плотно)."""
        atoms = self.cap.flat
        starts = self.cap.starts
        cats = [0] * W
        import bisect
        j = max(0, bisect.bisect_right(starts, t0) - 1)
        n = len(atoms)
        for x in range(W):
            cs = t0 + x * dt
            ce = cs + dt
            while j < n and atoms[j].t1 <= cs:
                j += 1
            k = j
            low = high = 0
            while k < n and atoms[k].t0 < ce:
                a = atoms[k]
                if a.kind in ("reset", "presence", "long"):
                    low = 1
                elif a.kind == "bit":
                    le = a.t0 + a.low
                    if min(le, a.t1) > cs and a.t0 < ce:
                        low = 1
                    if a.t1 > max(le, cs) and a.t0 < ce:
                        high = 1
                else:
                    high = 1
                k += 1
            cats[x] = 2 if (low and high) else (1 if low else 0)
        return cats

    def _bytes(self, c, t0, t1, dt, W):
        y0 = self.y_byte + 2
        y1 = self.y_byte + self.byte_h - 2
        i = max(0, bisect.bisect_left(self.span_starts, t0) - 1) \
            if self.spans else 0
        names = []
        while i < len(self.spans):
            s0, s1, val, role, name, tx = self.spans[i]
            if s0 >= t1:
                break
            i += 1
            if s1 <= t0 or role == "sys":
                continue
            x0 = max(0, (s0 - t0) / dt)
            x1 = min(W, (s1 - t0) / dt)
            if x1 - x0 < 0.4:
                x1 = x0 + 0.4
            col = CMD_COLOR if role == "cmd" else DATA_COLOR
            c.create_rectangle(x0, y0, x1, y1, fill=col, outline="")
            w = x1 - x0
            if w >= 15:
                c.create_text((x0 + x1) / 2, (y0 + y1) / 2,
                              text=f"{val:02X}", font=MONO, fill="#ffffff")
            if role == "cmd" and name:
                names.append((x0, x1, name))
        self._names(c, names)

    def _names(self, c, names):
        """Названия команд в своей полосе; при нехватке места не налезают друг на друга."""
        if not names:
            return
        yc = self.y_name + self.name_h / 2
        c.create_line(0, self.y_name, self.width, self.y_name, fill="#f0f0f0")
        edge = -10.0
        dropped = 0
        for x0, x1, name in names:
            w = len(name) * 5.6 + 8
            left = max(0.0, min(x0, self.width - w))
            if left < edge + 10:              # не налезаем на предыдущее название
                dropped += 1
                continue
            c.create_line(x0, self.y_name, x0, self.y_name + 3, fill=CMD_COLOR)
            c.create_text(left, yc, text=name, anchor="w", font=SMALL,
                          fill=CMD_COLOR)
            edge = left + w
        if dropped:
            c.create_text(self.width - 2, yc, text=f"+{dropped}", anchor="e",
                          font=SMALL, fill="#9e9e9e")

    def _bits(self, c, t0, t1, dt, W):
        """Битовая дорожка: каждый слот — ноль (низ), затем высокий до конца окна.

        Показывается только когда слот шире пары пикселей — при большом окне
        плотность бита неинформативна, и её изображает волна выше.
        """
        nominal = self.cap.p_slot if self.cap else 200.0
        if nominal / dt < 2.5:
            return
        y0 = self.y_bit + 3
        y1 = self.y_bit + self.bit_h - 3
        atoms = self.cap.flat
        j = max(0, bisect.bisect_right(self.cap.starts, t0) - 1)
        c.create_line(0, y1, W, y1, fill="#ececec")
        while j < len(atoms):
            a = atoms[j]
            if a.t0 >= t1:
                break
            j += 1
            if a.kind != "bit" or a.t1 <= t0:
                continue
            x0 = (a.t0 - t0) / dt
            x1 = (a.t1 - t0) / dt
            w = x1 - x0
            col = CMD_COLOR if a.role == "cmd" else DATA_COLOR
            if w < 2:                                  # слишком плотно — один столбец
                c.create_line(x0, y0, x0, y1, fill=col)
                continue
            # низ: от начала слота до конца нуля, затем высокий до конца слота
            xl = (min(a.t1, a.t0 + a.low) - t0) / dt
            xl = min(x1, max(x0, xl))
            c.create_line(x0, y1, xl, y1, fill=col, width=2)
            if xl < x1:
                c.create_line(xl, y0, x1, y0, fill=col, width=2)
                c.create_line(xl, y0, xl, y1, fill=col)
            if w >= 13:
                c.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=str(a.bit),
                              font=MONO, fill=col)
            first = self.byte_at.get(a.t0)
            if first is not None and w >= 40:      # подпись байта над его первым битом
                val, role, name = first
                c.create_text(x0 + 2, y0, text=f"{val:02X}", anchor="nw",
                              font=MONO_B, fill=col)
                if role == "cmd" and name and w * 8 >= 7 * len(name) + 40:
                    c.create_text(x0 + 26, y0, text=name, anchor="nw",
                                  font=MONO, fill=col)


class Panel(tk.Frame):
    """Холст с собственным окном времени: линейка, волна, коды (и биты)."""

    def __init__(self, master, cap, height, bit_h=0, on_wheel=None):
        super().__init__(master)
        self.cap = cap
        self.canvas = tk.Canvas(self, height=height, bg=BG,
                                highlightthickness=1, highlightbackground="#cfd8dc")
        self.canvas.pack(fill="both", expand=True)
        self.bit_h = bit_h
        self.ruler_h, self.wave_h, self.byte_h = 22, 54, 30
        self.model = Timeline(cap, 1000, height, self.ruler_h, self.wave_h,
                              self.byte_h, bit_h)
        self.t0, self.t1 = 0.0, 1.0
        self._dirty = False
        self.on_draw = None       # вызывается после отрисовки — рисует метки
        self.canvas.bind("<Configure>", self._resize)
        if on_wheel:
            self.canvas.bind("<MouseWheel>", on_wheel)

    def _resize(self, ev):
        self.model.width = max(1, ev.width)
        self.model.layout(max(60, ev.height))
        self.redraw()

    def set_window(self, t0, t1):
        self.t0, self.t1 = t0, t1
        self.redraw()

    def redraw(self):
        if self._dirty:
            return
        self._dirty = True
        self.after_idle(self._draw)

    def _draw(self):
        self._dirty = False
        self.model.width = max(1, self.canvas.winfo_width())
        self.model.layout(max(60, self.canvas.winfo_height()))
        self.model.draw(self.canvas, self.t0, self.t1)
        if self.on_draw:
            self.on_draw()

    def x_to_t(self, x):
        return self.t0 + (self.t1 - self.t0) * x / max(1, self.model.width)

    def t_to_x(self, t):
        return (t - self.t0) / (self.t1 - self.t0) * self.model.width


class Viewer:
    def __init__(self, root, path=None, on_open=None):
        self.root = root
        self.cap = None
        self.on_open = on_open or (lambda p: None)
        root.title("Сниффер 1-Wire — просмотр захвата")
        root.geometry("1280x760")
        root.minsize(900, 560)

        menubar = tk.Menu(root)
        fm = tk.Menu(menubar, tearoff=0)
        fm.add_command(label="Открыть…", accelerator="Ctrl+O", command=self.open_dialog)
        fm.add_separator()
        fm.add_command(label="Выход", command=root.destroy)
        menubar.add_cascade(label="Файл", menu=fm)
        root.config(menu=menubar)
        root.bind("<Control-o>", lambda e: self.open_dialog())

        top = ttk.Frame(root)
        top.pack(fill="x", padx=6, pady=(6, 2))
        self.lbl_file = ttk.Label(top, text="файл не открыт", foreground="gray")
        self.lbl_file.pack(side="left")
        self.lbl_info = ttk.Label(top, text="", foreground="#424242")
        self.lbl_info.pack(side="right")

        self.main = Panel(root, None, height=152, bit_h=36)
        self.main.pack(fill="x", padx=6, pady=2)

        bar = ttk.Frame(root)
        bar.pack(fill="x", padx=6, pady=2)
        ttk.Button(bar, text="−", width=3, command=lambda: self.zoom(2.0)).pack(side="left")
        ttk.Button(bar, text="+", width=3, command=lambda: self.zoom(0.5)).pack(side="left")
        ttk.Button(bar, text="Вся сессия", command=self.show_all).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Статистика", command=self.show_stats).pack(side="left", padx=(6, 6))
        self.slider = ttk.Scale(bar, from_=0.0, to=1.0, command=self._on_slider)
        self.slider.pack(side="left", fill="x", expand=True)
        self.lbl_scale = ttk.Label(bar, text="", width=30, anchor="e")
        self.lbl_scale.pack(side="right", padx=(6, 0))

        self.lbl_meas = ttk.Label(root, text="ПКМ: начало интервала — второй клик конец · "
                                            "ЛКМ: выделить окно для нижней шкалы",
                                  foreground="#6a1b9a")
        self.lbl_meas.pack(fill="x", padx=6)

        self.detail = Panel(root, None, height=196, bit_h=44)
        self.detail.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        self.lbl_sel = ttk.Label(root, text="", foreground="#424242")
        self.lbl_sel.pack(fill="x", padx=6, pady=(0, 4))

        self.status = ttk.Label(root, text="", anchor="w", relief="sunken")
        self.status.pack(fill="x", side="bottom")

        self.m_start = self.m_end = None
        self.measure_panel = None
        self.drag_from = None
        self.sel = None
        self.detail_t0 = self.detail_t1 = None
        self.main.on_draw = self._draw_marks
        self.detail.on_draw = self._draw_marks

        c = self.main.canvas
        c.bind("<MouseWheel>", self.on_wheel)
        c.bind("<ButtonPress-1>", self.sel_press)
        c.bind("<B1-Motion>", self.sel_motion)
        c.bind("<ButtonRelease-1>", self.sel_release)
        c.bind("<ButtonPress-3>", lambda e: self.measure_click(self.main, e))
        c.bind("<Motion>", self.hover)
        d = self.detail.canvas
        d.bind("<MouseWheel>", self.detail_wheel)
        d.bind("<ButtonPress-3>", lambda e: self.measure_click(self.detail, e))
        d.bind("<Motion>", self.hover)
        d.bind("<ButtonPress-1>", self.detail_press)
        d.bind("<B1-Motion>", self.detail_motion)
        d.bind("<ButtonRelease-1>", self.detail_release)
        root.bind("<Escape>", lambda e: self.clear_marks())

        if path:
            self.open(path)

    # --- файл ---

    def open_dialog(self):
        here = os.path.dirname(os.path.abspath(__file__))
        start = os.path.join(here, "sniffer")
        path = filedialog.askopenfilename(
            title="Захват сниффера",
            initialdir=start if os.path.isdir(start) else here,
            filetypes=[("Захват сниффера", "raw.bin"), ("Двоичные", "*.bin"),
                       ("Все файлы", "*.*")])
        if path:
            self.open(path)

    def open(self, path):
        try:
            cap = sniffmodel.parse_file(path)
        except Exception as e:
            self.lbl_file.config(text=f"Ошибка чтения: {e}", foreground=CMD_COLOR)
            return
        self.cap = cap
        self.on_open(path)
        name = os.path.basename(os.path.dirname(path)) or os.path.basename(path)
        self.lbl_file.config(text=path, foreground="#212121")
        self.lbl_info.config(
            text=f"{name} · {fmt_dur(cap.total)} · транзакций {len(cap.txns)} · "
                 f"бит {cap.n_bits} · слот ≈ {cap.p_slot:.0f} мкс"
                 + (f" · ПОТЕРИ: событий {cap.lost_events}, байт {cap.lost_bytes}"
                    if cap.lost_events or cap.lost_bytes else ""))
        for panel in (self.main, self.detail):
            panel.cap = cap
            panel.model = Timeline(cap, panel.model.width, panel.model.height,
                                   panel.model.ruler_h, panel.model.wave_h,
                                   panel.model.byte_h, panel.model.bit_h)
        self.show_all(initial=True)

    def show_all(self, initial=False):
        if not self.cap:
            return
        t1 = max(self.cap.total, 1.0)
        span = DEFAULT_SPAN if initial else t1
        self.set_view(0.0, min(span, t1))
        self.clear_marks()
        if len(self.cap.txns):
            first = self.cap.txns[0]
            self.select(first.t_start, min(first.t_end, first.t_start + 50e3))

    def show_stats(self):
        if not self.cap:
            return
        s = sniffmodel.compute_stats(self.cap)

        def row(title, a):
            if not a:
                return f"{title:<42} нет данных\n"
            return (f"{title:<42} {fmt_dur(a['mean']):>12}   "
                    f"{fmt_dur(a['min'])} … {fmt_dur(a['max'])}   n={a['n']}\n")

        text = f"{'':<42} {'среднее':>12}   мин … макс   количество\n"
        text += "—" * 92 + "\n"
        text += row("Лог «1» (низкий уровень слота)", s["one"])
        text += row("Лог «0», мастер пишет", s["zero_master"])
        text += row("Лог «0», чип отвечает", s["zero_chip"])
        if s["zero_unknown"]:
            text += row("Лог «0», роль не определена", s["zero_unknown"])
        text += row("Reset (импульс сброса)", s["reset"])
        text += row("Присутствие (ответ чипа)", s["presence"])
        text += row("Пауза между командами", s["between_cmd"])
        text += row(f"Пауза между сессиями (> {sniffmodel.SESSION_GAP_US // 1000} мс)", s["between_sess"])
        text += (f"\nТранзакций: {s['txns']}. Паузы измеряются только от 1 мс: у "
                 f"{s['no_gap']} транзакций пауза до следующего Reset короче 1 мс "
                 f"и в среднее не входит.\n")
        win = tk.Toplevel(self.root)
        win.title("Статистика захвата")
        box = tk.Text(win, font=("Consolas", 10), width=96, height=14, wrap="none")
        box.insert("1.0", text)
        box.config(state="disabled")
        box.pack(fill="both", expand=True, padx=8, pady=8)

    # --- окно времени ---

    def set_view(self, t0, t1):
        total = max(self.cap.total, 1.0)
        t0 = max(0.0, min(t0, total - MIN_SPAN))
        t1 = min(total, max(t1, t0 + MIN_SPAN))
        if t1 - t0 > total:
            t0, t1 = 0.0, total
        self.main.set_window(t0, t1)
        self._sync_slider()
        self.lbl_scale.config(text=f"окно {fmt_dur(t1 - t0)}: "
                                   f"{fmt_dur(t0)} — {fmt_dur(t1)}")

    def _sync_slider(self):
        total = max(self.cap.total, 1.0)
        span = self.main.t1 - self.main.t0
        room = max(1e-9, total - span)
        self.slider.set(min(1.0, max(0.0, self.main.t0 / room)))

    def _on_slider(self, _v):
        if not self.cap:
            return
        total = max(self.cap.total, 1.0)
        span = self.main.t1 - self.main.t0
        room = max(0.0, total - span)
        t0 = float(self.slider.get()) * room
        self.main.set_window(t0, t0 + span)
        self.lbl_scale.config(text=f"окно {fmt_dur(span)}: "
                                   f"{fmt_dur(t0)} — {fmt_dur(t0 + span)}")

    def on_wheel(self, ev):
        if not self.cap:
            return
        anchor = self.main.x_to_t(ev.x)
        factor = 0.8 if ev.delta > 0 else 1.25
        self.zoom(factor, anchor)

    def zoom(self, factor, anchor=None):
        if not self.cap:
            return
        t0, t1 = self.main.t0, self.main.t1
        if anchor is None:
            anchor = (t0 + t1) / 2
        span = max(MIN_SPAN, min((t1 - t0) * factor, self.cap.total))
        k = (anchor - t0) / max(1e-9, t1 - t0)
        nt0 = anchor - span * k
        self.set_view(nt0, nt0 + span)

    # --- выделение и измерение ---

    def sel_press(self, ev):
        if not self.cap:
            return
        self.drag_from = self.main.x_to_t(ev.x)

    def sel_motion(self, ev):
        if not self.cap or self.drag_from is None:
            return
        t = self.main.x_to_t(ev.x)
        self.select(self.drag_from, t, live=True)

    def sel_release(self, ev):
        if not self.cap or self.drag_from is None:
            return
        t = self.main.x_to_t(ev.x)
        a, b = sorted((self.drag_from, t))
        self.drag_from = None
        if b - a < (self.main.t1 - self.main.t0) / self.main.model.width * 3:
            return
        self.select(a, b)

    def select(self, a, b, live=False):
        a, b = sorted((a, b))
        if b - a < MIN_SPAN:
            b = a + MIN_SPAN
        self.sel = (a, b)
        self.detail_t0, self.detail_t1 = a, b
        self.detail.set_window(a, b)
        self._sel_label()
        self._draw_marks()

    def _sel_label(self):
        a, b = self.sel
        n = self._bits_between(a, b)
        self.lbl_sel.config(
            text=f"Выделение: {fmt_dur(a)} — {fmt_dur(b)} · Δ {fmt_dur(b - a)} · "
                 f"бит {n} · байт {n // 8}")

    def _bits_between(self, a, b):
        n = 0
        for t in self.cap.txns:
            if t.t_end < a or t.t_start > b:
                continue
            for at in t.atoms:
                if at.kind == "bit" and at.t1 > a and at.t0 < b:
                    n += 1
        return n

    def measure_click(self, panel, ev):
        if not self.cap:
            return
        t = panel.x_to_t(ev.x)
        if self.measure_panel is not panel or self.m_start is None or \
                self.m_end is not None:
            self.measure_panel = panel
            self.m_start, self.m_end = t, None
        else:
            self.m_end = t
        self._draw_marks()
        if self.m_end is not None:
            a, b = sorted((self.m_start, self.m_end))
            self.lbl_meas.config(
                text=f"Интервал: {fmt_dur(a)} — {fmt_dur(b)} · длительность {fmt_dur(b - a)}"
                     f" · бит {self._bits_between(a, b)}   (ПКМ — новый интервал)",
                foreground=MEAS_COLOR)

    def clear_marks(self):
        self.m_start = self.m_end = None
        self.measure_panel = self.main
        self.lbl_meas.config(text="ПКМ: начало интервала — второй клик конец · "
                                  "ЛКМ: выделить окно для нижней шкалы",
                             foreground=MEAS_COLOR)
        self._draw_marks()

    def _draw_marks(self):
        """Выделение и измерение на обеих шкалах (координаты у каждой свои)."""
        if not self.cap:
            return
        for panel in (self.main, self.detail):
            self._marks_on(panel)

    def _marks_on(self, panel):
        c = panel.canvas
        c.delete("mark")
        m = panel.model
        if self.sel and panel is self.main:
            a, b = self.sel
            x0, x1 = panel.t_to_x(a), panel.t_to_x(b)
            if x1 >= 0 and x0 <= m.width:
                c.create_rectangle(max(0, x0), 0, min(m.width, x1), m.height,
                                   fill=SEL_COLOR, stipple="gray50",
                                   outline="#fb8c00", tags="mark")
        if self.m_start is None:
            return
        pts = [self.m_start] + ([self.m_end] if self.m_end is not None else [])
        for i, t in enumerate(pts):
            x = panel.t_to_x(t)
            if not (-50 <= x <= m.width + 50):
                continue
            c.create_line(x, 0, x, m.height, fill=MEAS_COLOR, dash=(4, 2),
                          tags="mark")
            c.create_text(x + (3 if i == 0 else -3), m.height - 2,
                          text=f"■ {fmt_dur(t)}", anchor="sw" if i == 0 else "se",
                          font=SMALL, fill=MEAS_COLOR, tags="mark")
        if self.m_end is not None:
            a, b = sorted((self.m_start, self.m_end))
            xa, xb = panel.t_to_x(a), panel.t_to_x(b)
            y = m.y_wave + 6
            c.create_line(xa, y, xb, y, fill=MEAS_COLOR, width=2, tags="mark")
            c.create_text((xa + xb) / 2, y - 2, text=fmt_dur(b - a),
                          anchor="s", font=MONO_B, fill=MEAS_COLOR, tags="mark")

    # --- детальная шкала ---

    def detail_press(self, ev):
        if not self.detail.cap:
            return
        self.detail_drag = self.detail.x_to_t(ev.x)

    def detail_motion(self, ev):
        if getattr(self, "detail_drag", None) is None:
            return
        t = self.detail.x_to_t(ev.x)
        a, b = sorted((self.detail_drag, t))
        if b - a > MIN_SPAN:
            self.detail.set_window(a, b)

    def detail_release(self, ev):
        self.detail_drag = None

    def detail_wheel(self, ev):
        if not self.detail.cap:
            return
        t0, t1 = self.detail.t0, self.detail.t1
        anchor = self.detail.x_to_t(ev.x)
        factor = 0.8 if ev.delta > 0 else 1.25
        span = max(MIN_SPAN, (t1 - t0) * factor)
        if self.detail_t0 is not None:
            span = min(span, self.detail_t1 - self.detail_t0)
        k = (anchor - t0) / max(1e-9, t1 - t0)
        nt0 = anchor - span * k
        if self.detail_t0 is not None:
            nt0 = max(self.detail_t0, min(nt0, self.detail_t1 - span))
        self.detail.set_window(nt0, nt0 + span)
        self.lbl_sel.config(text=self._sel_prefix() + f" · нижняя шкала: "
                            f"{fmt_dur(nt0)} — {fmt_dur(nt0 + span)}")

    def _sel_prefix(self):
        a, b = self.sel
        n = self._bits_between(a, b)
        return (f"Выделение: {fmt_dur(a)} — {fmt_dur(b)} · Δ {fmt_dur(b - a)} · "
                f"бит {n} · байт {n // 8}")

    def hover(self, ev):
        panel = self.main if ev.widget is self.main.canvas else self.detail
        if not panel.cap:
            return
        t = panel.x_to_t(ev.x)
        a = panel.cap.info_at(t)
        if a is None:
            self.status.config(text=f"t = {fmt_dur(t)}   (тишина на линии)")
            return
        kind = {"reset": "сброс", "presence": "присутствие", "bit": "слот",
                "long": "длинный ноль", "gap": "пауза"}.get(a.kind, a.kind)
        txt = f"t = {fmt_dur(t)}   {kind}"
        if a.kind == "presence":
            txt += f" {fmt_dur(a.t1 - a.t0)}"
        if a.kind in ("long", "gap"):
            txt += f" {fmt_dur(a.t1 - a.t0)}"
        if a.kind == "bit":
            role = "команда" if a.role == "cmd" else "данные"
            val = 0
            for s in a.tx.byte_spans:
                if s[0] <= a.t0 <= s[1]:
                    val = s[2]
            txt += (f"   бит {a.bit}, ноль {a.low} мкс"
                    + (f"   байт {val:02X} ({role})" if a.byte is not None else ""))
        self.status.config(text=txt)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    root = tk.Tk()
    if sys.platform == "win32":
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    Viewer(root, path)
    root.mainloop()


if __name__ == "__main__":
    main()
