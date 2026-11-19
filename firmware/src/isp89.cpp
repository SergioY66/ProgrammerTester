// ======================================================================
//  ISP для AT89S8253 и AT89S52 (MCS-51) — аппаратный SPI Nano.
//  Команды по 4 байта при RST = 1; набор команд — как в at89s825x-arduino
//  (mtirado), который проверен на AT89S8253.
//
//  Отличия чипов (по даташитам):
//    8253 — Flash 12 КБ страница 64 Б, EEPROM 2 КБ, фьюзы, сигнатура 1E 73 по 0x30;
//           страничная команда трёхбайтовая: 0x50/0x30, hi, lo, затем данные.
//    S52  — Flash 8 КБ страница 256 Б, без EEPROM и фьюзов, сигнатура 1E 52 06 по 0x00;
//           страничная команда двухбайтовая: 0x50/0x30, старший адрес, затем данные.
//           Адрес страницы у S52 задаётся только старшим байтом (младший = 0).
//
//  SCK = 16 МГц / 128 = 125 кГц: должно быть меньше f/16 кварца (3,58 МГц → 223 кГц,
//  12 МГц → 750 кГц). Режим SPI (0 или 1) подбирается при входе по сигнатуре.
//  Время записи страницы и стирания — с запасом, результат проверяет ПК сверкой.
// ======================================================================

#include "common.h"
#include "isp89.h"
#include <util/delay.h>

#define ERASE_MS  600

struct IspDev {
  uint8_t  id;
  uint8_t  sig[3];
  uint8_t  sig_len;      // сколько байт сигнатуры определено
  uint16_t sig_addr;     // адрес первого байта сигнатуры
  uint16_t sig_step;     // шаг между байтами сигнатуры
  uint16_t flash_page;
  uint16_t eeprom_page;  // 0 — EEPROM нет
  uint8_t  extras;       // 1 — есть user row и фьюзы
  uint8_t  write_ms;     // ожидание записи страницы
};

static const IspDev DEVS[] = {
  // 8253: третий байт сигнатуры не определён и читается FF — сравниваем два;
  // байты лежат подряд (0x30, 0x31). Запись страницы — 64 Б, ждать 12 мс.
  { ISP_DEV_8253, {0x1E, 0x73, 0xFF}, 2, 0x30, 1,    64,  32, 1, 12 },
  // S52 (даташит 1919D, Table 24-1): сигнатура — байты по адресам 000H, 100H,
  // 200H, поэтому A12..A9 идут в команде, и читать их нужно с шагом 0x100.
  // Чтение подряд давало 1E FF 1E: верен только первый байт.
  // Запись страницы — 256 Б; цикл записи байта до 0.5 мс, на страницу ~128 мс.
  { ISP_DEV_S52,  {0x1E, 0x52, 0x06}, 3, 0x00, 0x100, 256, 0,  0, 130 },
};

static const IspDev *dev;          // текущий чип; nullptr — ISP не открыт

// Запись страницы идёт в чипе write_ms после передачи. Nano не ждёт её
// сразу, а отвечает ПК: ожидание совпадает с пересылкой следующей страницы.
// Перед любым следующим обращением к чипу (и перед снятием RST) — isp_settle().
static bool     write_pending;
static uint16_t write_ms;
static uint32_t write_start_us;

// Страница S52 (256 Б) не влезает в один пакет ПК: пишется двумя частями.
static bool     part_open;
static uint16_t part_addr;

void isp_settle() {
  if (!write_pending) return;
  // Остаток считаем один раз и ждём задержкой: pins_release() зовут и при
  // запрещённых прерываниях (UPDI), а тогда micros() не идёт.
  uint32_t done = micros() - write_start_us;
  if (done < write_ms * 1000UL)
    for (uint16_t n = (write_ms * 1000UL - done) / 100 + 1; n; n--) _delay_us(100);
  write_pending = false;
}

// Режим программирования открыт, пока включён SPI: любая другая команда
// вызывает pins_release(), а та выключает SPI.
#define ISP_ON() (SPCR & _BV(SPE))

static uint8_t xfer(uint8_t b) {
  SPDR = b;
  while (!(SPSR & _BV(SPIF))) ;
  return SPDR;
}

static uint8_t cmd4(uint8_t a, uint8_t b, uint8_t c, uint8_t d) {
  xfer(a); xfer(b); xfer(c);
  return xfer(d);
}

static bool is_s52() { return dev && dev->id == ISP_DEV_S52; }

uint16_t isp_page_size(uint8_t space) {
  if (!dev) return 0;
  if (space == ISP_CODE) return dev->flash_page;
  if (space == ISP_DATA) return dev->eeprom_page;
  return 0;
}

static uint8_t read_byte(uint8_t space, uint16_t addr) {
  uint8_t ah = addr >> 8, al = addr;
  switch (space) {
  case ISP_CODE:  return cmd4(0x20, ah, al, 0);
  case ISP_DATA:  return cmd4(0xA0, ah, al, 0);
  case ISP_ATMEL: return cmd4(0x28, ah, al, 0);   // S52: байты по 000H/100H/200H — нужен A12..A9
  case ISP_USER:  return cmd4(0x22, 0, al, 0);
  case ISP_LOCK:  return cmd4(0x24, 0, 0, 0);
  default:        return cmd4(0x21, 0, 0, 0);    // ISP_FUSES
  }
}

// Начало страничной инструкции. Длина заголовка у чипов разная (даташиты):
//   8253 — инструкция, старший адрес, младший адрес (три байта);
//   S52  — инструкция, старший адрес (два байта): страница выровнена по 256,
//          младший адрес всегда 0 и не передаётся. Третий байт у S52 чип принял
//          бы за первый байт данных, и вся страница уехала бы на один.
static void page_start(uint8_t space, uint16_t addr) {
  xfer(space == ISP_CODE ? 0x30 : 0xB0);           // страничное чтение
  xfer(addr >> 8);
  if (!is_s52()) xfer(addr);
}

static void page_write_start(uint8_t space, uint16_t addr) {
  xfer(space == ISP_CODE ? 0x50 : 0xD0);           // страничная запись
  xfer(addr >> 8);
  if (!is_s52()) xfer(addr);
}

static void write_queued() {
  write_ms = dev->write_ms;
  write_start_us = micros();
  write_pending = true;
}

static void rst_low()  { PORTB &= ~_BV(RST89_BIT); DDRB |= _BV(RST89_BIT); }
static void rst_high() { PORTB |= _BV(RST89_BIT);  DDRB |= _BV(RST89_BIT); }

// Сигнатура читается в открытом режиме программирования по текущему SPI.
static bool sig_matches(const IspDev *d, const uint8_t *sig) {
  for (uint8_t i = 0; i < d->sig_len; i++)
    if (sig[i] != d->sig[i]) return false;
  return true;
}

// base и step — из профиля: у 8253 байты подряд (0x30, 0x31), у S52 разнесены
// по 0x100 (000H, 100H, 200H — даташит 1919D, Table 24-1).
static void read_sig(uint16_t base, uint16_t step, uint8_t *out) {
  for (uint8_t i = 0; i < 3; i++) out[i] = read_byte(ISP_ATMEL, base + i * step);
}

// Режим SPI по даташиту: 8253 сэмплирует MOSI по спаду SCK (CPHA=1, режим 1),
// S52 — по фронту (режим 0). При автоопределении режим подбирается сам, а при
// типе, заданном оператором, сигнатуры для проверки нет — берём документированный.
static uint8_t preferred_mode(uint8_t id) {
  return id == ISP_DEV_8253 ? 1 : 0;
}

// force: 0 — определить по сигнатуре, ISP_DEV_* — тип задан вручную.
// out: режим SPI, сигнатура[3], тип чипа, байт lock, ответ на Programming Enable.
uint8_t isp_open(uint8_t force, uint8_t *out) {
  pins_release();
  part_open = false;
  dev = nullptr;
  PORTB &= ~(_BV(SCK_BIT) | _BV(MOSI_BIT));
  DDRB  |= _BV(SCK_BIT) | _BV(MOSI_BIT);           // SCK = 0 до подъёма RST

  uint8_t sig[3] = {0xFF, 0xFF, 0xFF};
  uint8_t lock = 0xFF;
  uint8_t enable = 0xFF;

  for (uint8_t mode = 0; mode < 2; mode++) {
    // SS (D10 = RST) — выходом ДО включения SPI: иначе при SS-входе, прижатом
    // подтяжкой головы к нулю, срабатывает Mode Fault, MSTR сбрасывается,
    // SPI становится ведомым и xfer() ждёт SPIF вечно (Nano «замирает»).
    rst_low();
    SPCR = _BV(SPE) | _BV(MSTR) | _BV(SPR1) | _BV(SPR0) | (mode ? _BV(CPHA) : 0);
    SPSR = 0;
    _delay_ms(10);
    rst_high();
    _delay_ms(50);
    // Чип подтверждает вход в программирование байтом 0110 1001 (0x69) на MISO.
    // Без этого байта любые чтения — шум отпущенной линии, а стирание и запись
    // молча ничего не делают. Отдаём его ПК: это единственный прямой признак,
    // что ISP действительно открыт.
    enable = cmd4(0xAC, 0x53, 0x00, 0x00);         // Programming Enable

    if (force) {
      // Тип задан оператором: сигнатуры для проверки нет (у закрытого чипа её нет),
      // поэтому пробуем документированный режим SPI этого чипа, а затем второй.
      for (uint8_t i = 0; i < sizeof(DEVS) / sizeof(DEVS[0]); i++)
        if (DEVS[i].id == force) dev = &DEVS[i];
      if (!dev) { isp_close(); return ST_BAD_ARGS; }
      if (mode != preferred_mode(force)) continue;
      read_sig(dev->sig_addr, dev->sig_step, sig);
      lock = read_byte(ISP_LOCK, 0);
      out[0] = mode;
      for (uint8_t i = 0; i < 3; i++) out[1 + i] = sig[i];
      out[4] = dev->id;
      out[5] = lock;
      out[6] = enable;
      return ST_OK;
    }

    for (uint8_t i = 0; i < sizeof(DEVS) / sizeof(DEVS[0]); i++) {
      const IspDev *d = &DEVS[i];
      read_sig(d->sig_addr, d->sig_step, sig);
      if (sig_matches(d, sig)) {
        dev = d;
        lock = read_byte(ISP_LOCK, 0);
        out[0] = mode;
        for (uint8_t j = 0; j < 3; j++) out[1 + j] = sig[j];
        out[4] = dev->id;
        out[5] = lock;
        out[6] = enable;
        return ST_OK;
      }
      // 8253 у части партий отдаёт сигнатуру и по 0x00 — пробуем вторую базу
      if (d->sig_len == 2) {
        read_sig(d->sig_addr == 0x30 ? 0x00 : 0x30, 1, sig);
        if (sig_matches(d, sig)) {
          dev = d;
          lock = read_byte(ISP_LOCK, 0);
          out[0] = mode;
          for (uint8_t j = 0; j < 3; j++) out[1 + j] = sig[j];
          out[4] = dev->id;
          out[5] = lock;
          out[6] = enable;
          return ST_OK;
        }
      }
    }
  }

  // Ни одна сигнатура не сошлась: чип закрыт от чтения, не в прищепке или
  // не вошёл в программирование. Отдаём прочитанное, тип выберет оператор.
  // SPI оставляем включённым: ПК переоткроет с нужным типом.
  read_sig(0x00, 1, sig);
  lock = read_byte(ISP_LOCK, 0);
  out[0] = 0;
  for (uint8_t i = 0; i < 3; i++) out[1 + i] = sig[i];
  out[4] = 0;
  out[5] = lock;
  out[6] = enable;
  return ST_ISP_NOSIG;
}

void isp_close() {
  isp_settle();
  part_open = false;
  dev = nullptr;
  if (DDRB & _BV(RST89_BIT)) {                     // выйти из программирования
    rst_low();
    _delay_ms(1);
  }
  pins_release();                                  // голова стартует от своей цепи сброса
}

uint8_t isp_read(uint8_t space, uint16_t addr, uint8_t *buf, uint16_t n) {
  if (!ISP_ON()) return ST_ISP_NOTOPEN;
  if (space > ISP_FUSES || n == 0) return ST_BAD_ARGS;
  if (!dev) return ST_ISP_NOTOPEN;                 // тип не определён
  if (space >= ISP_USER && space != ISP_LOCK && !dev->extras) return ST_BAD_ARGS;   // user row и фьюзов у S52 нет
  uint16_t page = isp_page_size(space);
  isp_settle();
  if (page && n == page && !(addr & (page - 1))) {
    page_start(space, addr);                       // чтение страницы
    for (uint16_t i = 0; i < n; i++) buf[i] = xfer(0);
    return ST_OK;
  }
  if (space == ISP_CODE || space == ISP_DATA) {    // байтовое чтение
    if (!page) return ST_BAD_ARGS;
  }
  for (uint16_t i = 0; i < n; i++) buf[i] = read_byte(space, addr + i);
  return ST_OK;
}

uint8_t isp_write_page(uint8_t space, uint16_t addr, const uint8_t *data, uint16_t n) {
  if (!ISP_ON() || !dev) return ST_ISP_NOTOPEN;
  if (space >= ISP_DATA && !dev->eeprom_page) return ST_BAD_ARGS;
  uint16_t page = isp_page_size(space);
  if (!page || n != page || (addr & (page - 1))) return ST_BAD_ARGS;
  if (page > 128) return ST_BAD_ARGS;              // S52 — только isp_write_part
  isp_settle();
  page_write_start(space, addr);
  for (uint16_t i = 0; i < n; i++) xfer(data[i]);
  write_queued();
  return ST_OK;
}

// Запись страницы S52 (256 Б) частями: 0 — первая половина, 1 — вторая (завершает).
// ВНИМАНИЕ: на AT89S52 в железе не подтверждено — данные в чип не попали.
uint8_t isp_write_part(uint8_t space, uint16_t addr, uint8_t part,
                       const uint8_t *data, uint16_t n) {
  if (!ISP_ON() || !dev) return ST_ISP_NOTOPEN;
  uint16_t page = isp_page_size(space);
  if (space != ISP_CODE || !page || page <= 128) return ST_BAD_ARGS;
  uint16_t half = page / 2;
  if (part > 1 || n != half || (addr & (page - 1))) return ST_BAD_ARGS;
  if (part == 0) {
    if (part_open) return ST_BAD_ARGS;             // предыдущая страница не дописана
    isp_settle();
    page_write_start(space, addr);
    part_addr = addr;
    part_open = true;
  } else if (!part_open || part_addr != addr) {
    return ST_BAD_ARGS;                            // не было начала страницы
  }
  for (uint16_t i = 0; i < n; i++) xfer(data[i]);
  if (part == 1) {
    part_open = false;
    write_queued();
  }
  return ST_OK;
}

// Запись одного байта Flash (даташит 1919D, «Write Program Memory (Byte Mode)»):
//   0100 0000 | A12..A8 | A7..A0 | D7..D0
// Цикл самодосрочный, до 0.5 мс. Нужен там, где страничный режим не проходит:
// байт уходит одним пакетом, без паузы внутри инструкции.
uint8_t isp_write_byte(uint8_t space, uint16_t addr, uint8_t data) {
  if (!ISP_ON() || !dev) return ST_ISP_NOTOPEN;
  if (space != ISP_CODE || !dev->flash_page) return ST_BAD_ARGS;
  isp_settle();
  cmd4(0x40, addr >> 8, addr, data);
  write_ms = 5;                                    // как в at89s-prog: запас на самодосрочную запись
  write_start_us = micros();
  write_pending = true;
  return ST_OK;
}

// Уровни: 2 — запись запрещена (режим 2), 3 — плюс чтение запрещено (режим 3).
//
// Байт 2 у чипов разный (Table 24-1 даташитов):
//   8253 — «1110 0 LB3 LB2 LB1»: каждый бит прописывается явно, 0 — «запрограммирован».
//          Режим 2 → 0xE1, режим 3 → 0xE4. Обратно читается «xxxx x LB3 LB2 LB1».
//   S52  — «1110 00 B1 B2»: B1/B2 кодируют РЕЖИМ, а не отдельные биты
//          (B1=0 B2=1 — режим 2, B1=1 B2=0 — режим 3, B1=1 B2=1 — режим 4).
//          Режим 2 → 0xE1, режим 3 → 0xE2. Обратно читается «xxx LB3 LB2 LB1 xx».
//
// ВАЖНО про S52 (подтверждено на живом чипе 2026-11-19):
//   E1 (B1=0 B2=1) программирует LB1 → читается бит 2;
//   E2 (B1=1 B2=0) программирует LB2 → читается бит 3.
//   Раскладка проверена по факту: 0x46 после E1, 0x4A после E2.
//   За одну сессию программирования чип принимает ТОЛЬКО одну запись lock-битов:
//   из пары команд срабатывает первая, вторая игнорируется в любом порядке.
//   Поэтому режим 3 (LB1+LB2) требует повторного входа в программирование
//   между командами — reenter_programming().
static uint8_t s52_lock_read() { return read_byte(ISP_LOCK, 0); }

// Вход в режим программирования заново. Нужен между двумя записями lock-битов.
// SPI на время импульса RST выключается: SS (= RST) прижатый к нулю роняет MSTR
// и переводит SPI в ведомый (см. комментарий в isp_open).
static void reenter_programming() {
  uint8_t spcr = SPCR;
  SPCR = 0;
  rst_low();
  _delay_ms(10);
  rst_high();
  _delay_ms(50);
  SPCR = spcr;                                     // SPE|MSTR|SPR1|SPR0|CPHA обратно
  cmd4(0xAC, 0x53, 0x00, 0x00);                    // Programming Enable
  _delay_ms(2);
}

uint8_t isp_lock(uint8_t level) {
  if (!ISP_ON() || !dev) return ST_ISP_NOTOPEN;
  if (level < 2 || level > 3) return ST_BAD_ARGS;
  isp_settle();

  if (dev->id == ISP_DEV_8253) {
    cmd4(0xAC, level >= 3 ? 0xE4 : 0xE1, 0x00, 0x00);
    _delay_ms(5);                                  // цикл записи самодосрочный, ~0,4 мс
    return ST_OK;
  }

  if (level < 3) {
    cmd4(0xAC, 0xE1, 0x00, 0x00);                  // режим 2: только LB1
    _delay_ms(10);
    return ST_OK;
  }

  // Режим 3 = LB1+LB2, по примечанию к Table 24-1 режимы включаются
  // последовательно: сначала 2 (LB1), затем 3 (LB2). Между ними повторный вход.
  // Два прохода — на случай, если первый вход после записи первого бита не удался.
  for (uint8_t pass = 0; pass < 2; pass++) {
    cmd4(0xAC, 0xE1, 0x00, 0x00);                  // LB1
    _delay_ms(10);
    reenter_programming();
    cmd4(0xAC, 0xE2, 0x00, 0x00);                  // LB2 → вместе это режим 3
    _delay_ms(10);
    if ((s52_lock_read() & 0x0C) == 0x0C) return ST_OK;   // LB1 и LB2 на месте
    reenter_programming();
  }
  return ST_OK;                                    // что вышло — ПК увидит по чтению
}

uint8_t isp_erase() {
  if (!ISP_ON() || !dev) return ST_ISP_NOTOPEN;
  isp_settle();
  part_open = false;
  cmd4(0xAC, 0x80, 0x00, 0x00);                    // Chip Erase: Flash + EEPROM + lock
  _delay_ms(ERASE_MS);
  return ST_OK;
}
