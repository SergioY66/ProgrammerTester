"""Программатор-тестер карт Flash, чипов Zoom и головы ZoomLamp — окно управления."""

import datetime
import json
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

import firmware_image
import head89
import operations as ops
import sniffview
from device import Device, DeviceError, list_ports
from sniffdecode import SniffDecoder

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(HERE, "settings.json")
LOG_DIR = os.path.join(HERE, "logs")

SNIFF_DIR = os.path.join(HERE, "sniffer")
PRODUCTS = {"card": "Карта Flash", "chip": "Чип Zoom", "head": "AT89 (8253 / S52)"}

# Nano держит режим «управление с ПК», пока раз в 5 с приходит хоть один пакет
KEEPALIVE_MS = 2000


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.dev: Device | None = None
        self.busy = False
        self.q = queue.Queue()
        self.cfg = load_settings()
        self.files = self.cfg.get("files", {})
        self.images = {}
        self.stats = {"ok": 0, "fail": 0}

        root.title("Программатор-тестер — карты Flash, чипы Zoom, голова ZoomLamp")
        root.geometry("900x640")
        root.minsize(720, 480)

        pad = {"padx": 6, "pady": 4}

        # --- порт ---
        top = ttk.Frame(root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Порт:").pack(side="left")
        self.port = ttk.Combobox(top, width=34, state="readonly")
        self.port.pack(side="left", padx=4)
        ttk.Button(top, text="Обновить", command=self.refresh_ports).pack(side="left")
        self.btn_conn = ttk.Button(top, text="Подключить", command=self.toggle_connect)
        self.btn_conn.pack(side="left", padx=4)
        self.lbl_conn = ttk.Label(top, text="не подключено", foreground="gray")
        self.lbl_conn.pack(side="left", padx=8)

        # --- изделие и файл ---
        prod = ttk.LabelFrame(root, text="Изделие")
        prod.pack(fill="x", **pad)
        self.product = tk.StringVar(value=self.cfg.get("product", "card"))
        row = ttk.Frame(prod)
        row.pack(fill="x", padx=6, pady=4)
        for key, name in PRODUCTS.items():
            ttk.Radiobutton(row, text=name, value=key, variable=self.product,
                            command=self.on_product).pack(side="left", padx=(0, 16))
        ttk.Label(row, text="Зарядка чипа перед тестом, мс:").pack(side="left", padx=(24, 4))
        self.charge = tk.StringVar(value=str(self.cfg.get("charge_ms", 500)))
        ttk.Spinbox(row, from_=50, to=10000, increment=50, width=7,
                    textvariable=self.charge).pack(side="left")

        row = ttk.Frame(prod)
        row.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(row, text="Прошивка:").pack(side="left")
        self.file_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.file_var, state="readonly").pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row, text="Выбрать…", command=self.choose_file).pack(side="left")
        # EEPROM головы формирует программа (head89.build_eeprom) — файл не выбирается
        self.lbl_file = ttk.Label(prod, text="", foreground="gray")
        self.lbl_file.pack(fill="x", padx=6, pady=(0, 6))

        # --- действия ---
        act = ttk.Frame(root)
        act.pack(fill="x", **pad)
        # Кнопка → изделия, для которых она есть
        self.buttons = []
        for text, fn, where in (("Прошить и проверить", self.do_all, "card chip head"),
                                ("Только прошить", self.do_program, "card chip"),
                                ("Только тест", self.do_test, "card chip"),
                                ("Состояние чипа", self.do_status, "card chip head"),
                                ("Снять дамп", self.do_dump, "chip head"),
                                ("Сниффер 1-Wire", self.do_sniff, "card chip head")):
            b = ttk.Button(act, text=text, command=fn)
            b.pack(side="left", padx=(0, 6), ipady=4)
            self.buttons.append((b, where.split()))
        self.sniff_stop_evt = threading.Event()
        ttk.Button(act, text="Стоп", command=self.sniff_stop_evt.set).pack(side="left", padx=(0, 6), ipady=4)
        ttk.Button(act, text="Визуализатор…", command=self.do_viewer).pack(side="left", padx=(0, 6), ipady=4)
        self.sniff_detail = tk.BooleanVar(value=False)
        ttk.Checkbutton(act, text="тайминги слотов", variable=self.sniff_detail).pack(side="left")
        ttk.Button(act, text="Сбросить счёт", command=self.reset_stats).pack(side="right")
        self.lbl_stats = ttk.Label(act, text="")
        self.lbl_stats.pack(side="right", padx=8)

        self.prog = ttk.Progressbar(root, maximum=1.0)
        self.prog.pack(fill="x", **pad)

        self.result = tk.Label(root, text="—", font=("Segoe UI", 20, "bold"), bg="#e8e8e8", height=2)
        self.result.pack(fill="x", **pad)

        self.log_box = ScrolledText(root, font=("Consolas", 10), state="disabled", wrap="word")
        self.log_box.pack(fill="both", expand=True, **pad)
        self.log_box.tag_config("ok", foreground="#1a7f2e")
        self.log_box.tag_config("warn", foreground="#b35c00")
        self.log_box.tag_config("err", foreground="#c62828")
        self.log_box.tag_config("head", font=("Consolas", 10, "bold"))

        self.refresh_ports()
        self.on_product()
        self.update_stats()
        self.update_buttons()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(50, self.poll)
        root.after(KEEPALIVE_MS, self.keepalive)

    # ------------------------------------------------------------------
    #  Настройки, порт, файл
    # ------------------------------------------------------------------

    def save_settings(self):
        self.cfg.update(product=self.product.get(), files=self.files,
                        port=self.port.get().split(" ")[0], charge_ms=self.charge_ms())
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def charge_ms(self) -> int:
        try:
            return max(0, min(60000, int(self.charge.get())))
        except ValueError:
            return 500

    def refresh_ports(self):
        ports = [f"{d} — {desc}" for d, desc in list_ports()]
        self.port["values"] = ports
        want = self.cfg.get("port", "")
        for p in ports:
            if p.split(" ")[0] == want:
                self.port.set(p)
                break
        else:
            if ports and not self.port.get():
                self.port.set(ports[0])

    def disconnect(self, text="не подключено", color="gray"):
        if self.dev:
            self.dev.close()
            self.dev = None
        self.lbl_conn.config(text=text, foreground=color)
        self.btn_conn.config(text="Подключить")
        self.update_buttons()

    def keepalive(self):
        if self.dev and not self.busy:
            try:
                self.dev.ping()
            except Exception as e:
                self.log(f"Связь с Nano потеряна: {e}", "err")
                self.disconnect("связь потеряна", "#c62828")
        self.root.after(KEEPALIVE_MS, self.keepalive)

    def toggle_connect(self):
        if self.busy:
            return
        if self.dev:
            self.disconnect()
            return
        port = self.port.get().split(" ")[0]
        if not port:
            return
        self.lbl_conn.config(text="подключение…", foreground="gray")
        self.root.update_idletasks()
        try:
            self.dev = Device(port)
        except Exception as e:
            self.lbl_conn.config(text="нет связи", foreground="#c62828")
            self.log(f"Подключение к {port}: {e}", "err")
            return
        self.lbl_conn.config(text=f"Nano: прошивка v{self.dev.version}", foreground="#1a7f2e")
        self.btn_conn.config(text="Отключить")
        self.log(f"Подключено: {port}")
        self.save_settings()
        self.update_buttons()

    def on_product(self):
        path = self.files.get(self.product.get(), "")
        self.file_var.set(path)
        self.load_image(path)
        self.update_buttons()
        self.save_settings()

    def choose_file(self):
        cur = self.file_var.get()
        head = self.product.get() == "head"
        types = ([("Прошивка AT89 / дамп", "*.hex *.ihx *.bin")] if head
                 else [("Прошивка AVR", "*.elf *.hex")])
        path = filedialog.askopenfilename(
            title=f"Прошивка: {PRODUCTS[self.product.get()]}",
            initialdir=os.path.dirname(cur) if cur else (head89.DUMP_DIR if head else None),
            filetypes=types + [("Все файлы", "*.*")])
        if not path:
            return
        self.files[self.product.get()] = path
        self.file_var.set(path)
        self.load_image(path)
        self.save_settings()

    def load_head_image(self, path):
        flash = head89.load_image(path, head89.FLASH_SIZE, "Flash")
        self.images["head"] = flash
        self.lbl_file.config(
            text=f"Flash {head89.used(flash)} Б · тип чипа определяется по сигнатуре; "
                 "у AT89S8253 EEPROM формирует программа (коэффициенты, наработка, новый ключ), "
                 "у AT89S52 — только Flash",
            foreground="black")

    def load_image(self, path):
        self.images.pop(self.product.get(), None)
        if not path:
            self.lbl_file.config(text="файл не выбран", foreground="gray")
            return
        try:
            if self.product.get() == "head":
                self.load_head_image(path)
                return
            img = firmware_image.load(path)
        except Exception as e:
            self.lbl_file.config(text=f"Ошибка: {e}", foreground="#c62828")
            return
        self.images[self.product.get()] = img
        bad = not img.has_fuses
        self.lbl_file.config(text=ops.describe(img), foreground="#c62828" if bad else "black")
        for w in img.warnings:
            self.log(w, "warn")

    # ------------------------------------------------------------------
    #  Журнал и результат
    # ------------------------------------------------------------------

    def log(self, text, tag=None):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_box.config(state="normal")
        self.log_box.insert("end", f"{stamp}  {text}\n", tag or ())
        self.log_box.see("end")
        self.log_box.config(state="disabled")
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            day = datetime.date.today().isoformat()
            with open(os.path.join(LOG_DIR, f"{day}.log"), "a", encoding="utf-8") as f:
                f.write(f"{stamp}  {text}\n")
        except OSError:
            pass

    def set_result(self, ok, text):
        color = {True: "#2e7d32", False: "#c62828", None: "#e8e8e8"}[ok]
        self.result.config(text=text, bg=color, fg="white" if ok is not None else "black")

    def update_stats(self):
        self.lbl_stats.config(text=f"Годных: {self.stats['ok']}   Брак: {self.stats['fail']}")

    def reset_stats(self):
        self.stats = {"ok": 0, "fail": 0}
        self.update_stats()

    def update_buttons(self):
        ready = self.dev and not self.busy
        for b, where in self.buttons:
            b.config(state="normal" if ready and self.product.get() in where else "disabled")

    # ------------------------------------------------------------------
    #  Фоновые операции
    # ------------------------------------------------------------------

    def poll(self):
        try:
            while True:
                kind, *args = self.q.get_nowait()
                if kind == "log":
                    self.log(*args)
                elif kind == "progress":
                    self.prog["value"] = args[0]
                elif kind == "ask":
                    text, answer, event = args
                    try:
                        answer.append(messagebox.askyesno("Подтверждение", text,
                                                          icon="warning", default="no"))
                    finally:
                        event.set()
                elif kind == "choose":
                    text, yes_text, no_text, answer, event = args
                    try:
                        answer.append(self._choose_dialog(text, yes_text, no_text))
                    finally:
                        event.set()
                elif kind == "int":
                    title, text, kwargs, answer, event = args
                    try:
                        answer.append(simpledialog.askinteger(title, text, parent=self.root, **kwargs))
                    finally:
                        event.set()
                elif kind == "done":
                    ok, text, counted = args
                    self.set_result(ok, text)
                    if counted:
                        self.stats["ok" if ok else "fail"] += 1
                        self.update_stats()
                    self.busy = False
                    self.update_buttons()
        except queue.Empty:
            pass
        except Exception as e:
            # Очередь не должна умирать: иначе фоновый поток ждёт ответа вечно,
            # кнопки остаются выключенными и окно не закрывается.
            self.log(f"Внутренняя ошибка интерфейса: {e!r}", "err")
        finally:
            self.root.after(50, self.poll)

    def run(self, title, job, counted=True, ok_text="ГОДНО"):
        if self.busy or not self.dev:
            return
        self.busy = True
        self.update_buttons()
        self.prog["value"] = 0
        self.set_result(None, f"{title}…")
        product = self.product.get()
        img = self.images.get(product)
        charge = self.charge_ms()
        self.save_settings()

        def log(text, tag=None):
            self.q.put(("log", text, tag))

        def progress(v):
            self.q.put(("progress", v))

        def worker():
            try:
                job(product, img, charge, log, progress)
                self.q.put(("done", True, ok_text, counted))
            except ops.Abort as e:
                log(str(e), "err")
                self.q.put(("done", False, "БРАК", counted))
            except (DeviceError, TimeoutError, IOError) as e:
                log(f"Ошибка: {e}", "err")
                self.q.put(("done", False, "ОШИБКА", counted))
            except Exception as e:
                log(f"Внутренняя ошибка: {e!r}", "err")
                self.q.put(("done", False, "ОШИБКА", False))

        threading.Thread(target=worker, daemon=True).start()

    def _wait_answer(self, event):
        """Ждёт ответа оператора, раз в секунду подавая Nano признак жизни.

        Ответ оператор выбирает сколько ему нужно (в журнале — 8 с), а Nano без
        пакетов 5 с уходит в автономный режим: снимает линии и теряет открытую
        ISP-сессию — следующее стирание получает «режим программирования не
        открыт». PING ничего не сбрасывает и ISP-сессию не трогает, а порт в это
        время свободен: фоновый поток ждёт здесь ответа, основной занят диалогом.
        """
        dev = self.dev
        lost = False
        while not event.wait(1.0):
            try:
                dev.ping()
            except Exception as e:
                if not lost:                       # порт мог отвалиться — не спамим
                    lost = True
                    self.q.put(("log", f"Связь с Nano потеряна: {e}", "err"))

    def confirm(self, text) -> bool:
        """Вопрос «Да / Нет» из фонового потока: окно показывает poll()."""
        answer, event = [], threading.Event()
        self.q.put(("ask", text, answer, event))
        self._wait_answer(event)
        return bool(answer[0]) if answer else False     # окно не дало ответа — «Нет»

    def choose_type(self, text, yes_text, no_text):
        """Выбор типа чипа из фонового потока. True — «Да», False — «Нет», None — отмена."""
        answer, event = [], threading.Event()
        self.q.put(("choose", text, yes_text, no_text, answer, event))
        self._wait_answer(event)
        return answer[0] if answer else None

    def ask_integer(self, title, text, **kwargs):
        """Целое число из фонового потока (simpledialog — только в главном)."""
        answer, event = [], threading.Event()
        self.q.put(("int", title, text, kwargs, answer, event))
        self._wait_answer(event)
        return answer[0] if answer else None

    def _choose_dialog(self, text, yes_text, no_text):
        """Окно выбора из двух с отменой. Своё, потому что у askyesnocancel нет
        параметров для подписей кнопок, а с yes=/no= он падает (TclError)."""
        answer = [None]
        win = tk.Toplevel(self.root)
        win.title("Выбор типа чипа")
        win.transient(self.root)
        win.resizable(False, False)
        ttk.Label(win, text=text, justify="left", wraplength=440).pack(
            padx=16, pady=(16, 12), anchor="w")
        row = ttk.Frame(win)
        row.pack(padx=16, pady=(0, 16), fill="x")

        def pick(value):
            answer[0] = value
            win.destroy()

        ttk.Button(row, text=yes_text, command=lambda: pick(True)).pack(side="left", padx=(0, 8))
        ttk.Button(row, text=no_text, command=lambda: pick(False)).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Отмена", command=lambda: pick(None)).pack(side="left")
        win.protocol("WM_DELETE_WINDOW", lambda: pick(None))
        win.bind("<Escape>", lambda e: pick(None))
        win.grab_set()                                # модальное: остальное окно ждёт
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.root.wait_window(win)
        return answer[0]

    def _need_image(self, img):
        if img is None:
            raise ops.Abort("Не выбран файл прошивки для этого изделия")

    def _test(self, product, charge, log):
        if product == "card":
            ops.test_card(self.dev, log)
        else:
            ops.test_chip(self.dev, log, charge)

    def ask_runtime(self):
        """Наработка для EEPROM головы, минуты; None — оператор отменил прошивку.

        Спрашивается из фонового потока и только для AT89S8253: у S52 EEPROM нет.
        """
        hours = self.ask_integer(
            "Наработка головы",
            "Общее время наработки, которое запишется в EEPROM головы, часы.\n"
            "По умолчанию 0 — новая голова.",
            initialvalue=0, minvalue=0, maxvalue=head89.RUNTIME_MAX_MIN // 60)
        return None if hours is None else hours * 60

    def do_all(self):
        if self.product.get() == "head":
            def job(product, img, charge, log, progress):
                self._need_image(img)
                head89.program(self.dev, img, log, progress, self.confirm,
                               choose_type=self.choose_type, ask_runtime=self.ask_runtime)
            self.run("Прошивка AT89", job, counted=False, ok_text="ПРОШИТО")
            return

        def job(product, img, charge, log, progress):
            self._need_image(img)
            ops.program(self.dev, img, log, progress)
            self._test(product, charge, log)
        self.run("Прошивка и тест", job)

    def do_program(self):
        def job(product, img, charge, log, progress):
            self._need_image(img)
            ops.program(self.dev, img, log, progress)
        self.run("Прошивка", job)

    def do_test(self):
        def job(product, img, charge, log, progress):
            self._test(product, charge, log)
        self.run("Тест", job)

    def do_status(self):
        def job(product, img, charge, log, progress):
            if product == "head":
                head89.read_status(self.dev, log, self.choose_type)
            else:
                ops.read_status(self.dev, log)
        self.run("Чтение состояния", job, counted=False, ok_text="Прочитано")

    def do_dump(self):
        def job(product, img, charge, log, progress):
            if product == "head":
                head89.dump(self.dev, log, progress, self.choose_type)
            else:
                ops.dump_chip(self.dev, log, charge)
        self.run("Дамп AT89" if self.product.get() == "head" else "Дамп чипа",
                 job, counted=False, ok_text="ДАМП СНЯТ")

    def _sniff_folder(self) -> str:
        """Новая папка pc/sniffer/sniff_<время>/ — захват никогда не перезаписывается."""
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        folder = os.path.join(SNIFF_DIR, f"sniff_{stamp}")
        n = 1
        while os.path.exists(folder):
            n += 1
            folder = os.path.join(SNIFF_DIR, f"sniff_{stamp}_{n}")
        os.makedirs(folder)
        return folder

    def do_sniff(self):
        """Пассивный сниффер 1-Wire (Y-переходник на X2): идёт, пока не нажата «Стоп»."""
        self.sniff_stop_evt.clear()
        detail = self.sniff_detail.get()

        def job(product, img, charge, log, progress):
            dec = SniffDecoder(detail=detail)
            folder = self._sniff_folder()
            # пишем по ходу захвата и с flush: при обрыве USB пойманное сохранится
            with open(os.path.join(folder, "raw.bin"), "wb") as raw, \
                    open(os.path.join(folder, "log.txt"), "w", encoding="utf-8") as txt:
                def out(lines):
                    for text in lines:
                        log(text)
                        txt.write(text + "\n")
                    txt.flush()

                self.dev.sniff_start()
                log(f"Сниффер запущен, запись в {folder}")
                log("Голова — чип — Nano на одной линии DQ; «Стоп» — остановить.")
                try:
                    while not self.sniff_stop_evt.is_set():
                        data = self.dev.sniff_read()
                        raw.write(data)
                        raw.flush()
                        out(dec.feed(data) + dec.idle())
                finally:
                    tail = self.dev.sniff_stop()
                    raw.write(tail)
                    out(dec.feed(tail) + dec.flush())
            log("Сниффер остановлен.")
            log(f"Сохранено: {folder}", "ok")
        self.run("Сниффер", job, counted=False, ok_text="СТОП")

    def do_viewer(self):
        """Визуализатор сырых данных сниффера — отдельным окном, без связи с Nano."""
        win = tk.Toplevel(self.root)
        last = self.cfg.get("sniff_last", "")
        path = last if last and os.path.exists(last) else None

        def remember(p):
            self.cfg["sniff_last"] = p
            self.save_settings()

        sniffview.Viewer(win, path, on_open=remember)

    def on_close(self):
        if self.busy:
            # Не блокируем закрытие молча: спрашиваем и, если да, выходим.
            if not messagebox.askyesno(
                    "Идёт операция",
                    "Идёт операция с изделием. Прервать и закрыть программу?\n\n"
                    "Прошивка может остаться незавершённой.",
                    icon="warning", default="no", parent=self.root):
                return
        self.save_settings()
        if self.dev:
            self.dev.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    if sys.platform == "win32":
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
