// ======================================================================
//  UPDI — программный UART на одном выводе (D5), полудуплекс.
//  Кадр UPDI: старт, 8 бит данных, чётность (even), 2 стоп-бита.
//  Скорость 115200: UPDI подстраивается под нас по символу SYNC (0x55)
//  в начале каждой команды, так что небольшая неточность не страшна.
//  Биты отмеряются по Timer1 (свободный счёт, 16 МГц) — не зависят от
//  длины кода. Прерывания запрещены на всё время обмена.
//
//  Последовательности команд — как в pymcuprog (Microchip, serialupdi).
// ======================================================================

#include "common.h"
#include "updi.h"
#include <util/delay.h>

#define BIT_T ((uint16_t)(F_CPU / 115200UL))       // 138 тактов на бит

#define L_LOW()   (PORTD &= ~_BV(UPDI_BIT))
#define L_HIGH()  (PORTD |= _BV(UPDI_BIT))
#define L_OUT()   (DDRD |= _BV(UPDI_BIT))
#define L_IN()    (DDRD &= ~_BV(UPDI_BIT), PORTD |= _BV(UPDI_BIT))   // вход с подтяжкой
#define L_READ()  (PIND & _BV(UPDI_BIT))

// --- Инструкции UPDI ---
#define SYNC         0x55
#define ACK          0x40
#define I_LDS        0x00
#define I_STS        0x40
#define I_LD         0x20
#define I_ST         0x60
#define I_LDCS       0x80
#define I_STCS       0xC0
#define I_REPEAT     0xA0
#define I_KEY        0xE0
#define PTR_INC      0x04
#define PTR_ADDRESS  0x08
#define ADDR_16      0x04
#define DATA_8       0x00
#define DATA_16      0x01

// --- Регистры CS / ASI ---
#define CS_STATUSA        0x00
#define CS_CTRLA          0x02
#define CS_CTRLB          0x03
#define ASI_KEY_STATUS    0x07
#define ASI_RESET_REQ     0x08
#define ASI_SYS_STATUS    0x0B

#define CTRLA_IBDLY       0x80   // пауза между байтами ответа — нам успеть сохранить байт
#define CTRLA_GT_8        0x04   // guard time 8 бит (по умолчанию 128 — медленно)
#define CTRLB_CCDETDIS    0x08
#define CTRLB_UPDIDIS     0x04

#define KEY_CHIPERASE_BIT 0x08
#define KEY_NVMPROG_BIT   0x10
#define SYS_LOCKSTATUS    0x01
#define SYS_NVMPROG       0x08

// --- NVMCTRL tinyAVR 0/1-series ---
#define NVM_CTRLA   0x1000
#define NVM_STATUS  0x1002
#define NVM_DATA    0x1006
#define NVM_ADDR    0x1008
#define NVM_WP      0x01
#define NVM_ERWP    0x03
#define NVM_PBC     0x04
#define NVM_WFU     0x07
#define NVM_FBUSY   0x01
#define NVM_EEBUSY  0x02
#define NVM_WRERROR 0x04

#define FLASH_BASE  0x8000
#define EEPROM_BASE 0x1400

// ---------------------------------------------------------------------
//  Физический уровень
// ---------------------------------------------------------------------

static void tx(uint8_t b) {
  uint8_t p = b;                                   // чётность even: бит = XOR битов данных
  p ^= p >> 4; p ^= p >> 2; p ^= p >> 1;
  uint16_t frame = ((uint16_t)b << 1) | ((uint16_t)(p & 1) << 9) | (3u << 10);
  L_HIGH(); L_OUT();
  uint16_t t = TCNT1;
  for (uint8_t i = 0; i < 12; i++) {
    if (frame & 1) L_HIGH(); else L_LOW();
    frame >>= 1;
    t += BIT_T;
    while ((int16_t)(TCNT1 - t) < 0) ;
  }
}

static void send(const uint8_t *b, uint8_t n) {
  while (n--) tx(*b++);
  L_IN();                                          // отпустить линию — ждём ответ
}

// Байт или -ST_*.
static int16_t rx() {
  uint32_t to = 150000;                            // ~100 мс
  while (L_READ()) if (!--to) return -ST_UPDI_TIMEOUT;
  uint16_t t = TCNT1 + BIT_T / 2;                  // середина старт-бита
  uint16_t v = 0;
  for (uint8_t i = 0; i < 10; i++) {               // 8 бит данных, чётность, стоп
    t += BIT_T;
    while ((int16_t)(TCNT1 - t) < 0) ;
    if (L_READ()) v |= 1u << i;
  }
  uint8_t d = v, p = d;
  p ^= p >> 4; p ^= p >> 2; p ^= p >> 1;
  if (((v >> 8) & 1) != (p & 1) || !((v >> 9) & 1)) return -ST_UPDI_FRAME;
  return d;
}

static uint8_t get_ack() {
  int16_t r = rx();
  if (r < 0) return -r;
  return r == ACK ? ST_OK : ST_UPDI_NOACK;
}

static void double_break() {
  L_OUT();
  L_LOW();  _delay_ms(30);
  L_HIGH(); _delay_us(200);
  L_LOW();  _delay_ms(30);
  L_HIGH(); _delay_us(200);
  L_IN();
  _delay_ms(2);
}

// ---------------------------------------------------------------------
//  Канальный уровень
// ---------------------------------------------------------------------

static uint8_t ldcs(uint8_t reg, uint8_t *v) {
  uint8_t b[] = {SYNC, (uint8_t)(I_LDCS | reg)};
  send(b, 2);
  int16_t r = rx();
  if (r < 0) return -r;
  *v = r;
  return ST_OK;
}

static void stcs(uint8_t reg, uint8_t v) {
  uint8_t b[] = {SYNC, (uint8_t)(I_STCS | reg), v};
  send(b, 3);
}

static uint8_t lds(uint16_t a, uint8_t *v) {
  uint8_t b[] = {SYNC, I_LDS | ADDR_16 | DATA_8, (uint8_t)a, (uint8_t)(a >> 8)};
  send(b, 4);
  int16_t r = rx();
  if (r < 0) return -r;
  *v = r;
  return ST_OK;
}

static uint8_t sts(uint16_t a, uint8_t v) {
  uint8_t b[] = {SYNC, I_STS | ADDR_16 | DATA_8, (uint8_t)a, (uint8_t)(a >> 8)};
  send(b, 4);
  uint8_t e = get_ack();
  if (e) return e;
  send(&v, 1);
  return get_ack();
}

static uint8_t st_ptr(uint16_t a) {
  uint8_t b[] = {SYNC, I_ST | PTR_ADDRESS | DATA_16, (uint8_t)a, (uint8_t)(a >> 8)};
  send(b, 4);
  return get_ack();
}

static void repeat(uint16_t n) {                   // следующая инструкция выполнится n раз
  uint8_t b[] = {SYNC, I_REPEAT, (uint8_t)(n - 1)};
  send(b, 3);
}

static uint8_t ld_ptr_inc(uint8_t *buf, uint16_t n) {
  if (n > 1) repeat(n);
  uint8_t b[] = {SYNC, I_LD | PTR_INC | DATA_8};
  send(b, 2);
  for (uint16_t i = 0; i < n; i++) {
    int16_t r = rx();
    if (r < 0) return -r;
    buf[i] = r;
  }
  return ST_OK;
}

static uint8_t st_ptr_inc(const uint8_t *data, uint8_t n) {
  if (n > 1) repeat(n);
  uint8_t b[] = {SYNC, I_ST | PTR_INC | DATA_8, data[0]};
  send(b, 3);
  uint8_t e = get_ack();
  for (uint8_t i = 1; i < n && !e; i++) {
    send(&data[i], 1);
    e = get_ack();
  }
  return e;
}

static void key(const char *k) {                   // 8 байт, передаются задом наперёд
  uint8_t b[10] = {SYNC, I_KEY};
  for (uint8_t i = 0; i < 8; i++) b[2 + i] = k[7 - i];
  send(b, 10);
}

static void reset_pulse() {
  stcs(ASI_RESET_REQ, 0x59);
  stcs(ASI_RESET_REQ, 0x00);
}

// Ждём снятия LOCKSTATUS после сброса. 0 — снят, иначе код.
static uint8_t wait_unlocked(uint16_t ms) {
  for (uint16_t i = 0; i < ms; i++) {
    uint8_t ss;
    uint8_t e = ldcs(ASI_SYS_STATUS, &ss);
    if (e) return e;
    if (!(ss & SYS_LOCKSTATUS)) return ST_OK;
    _delay_ms(1);
  }
  return ST_UPDI_LOCKED;
}

// ---------------------------------------------------------------------
//  NVM
// ---------------------------------------------------------------------

static uint8_t nvm_wait() {
  for (uint16_t i = 0; i < 5000; i++) {            // до ~0,5 с с учётом обмена
    uint8_t s;
    uint8_t e = lds(NVM_STATUS, &s);
    if (e) return e;
    if (!(s & (NVM_FBUSY | NVM_EEBUSY)))
      return (s & NVM_WRERROR) ? ST_NVM_WRERROR : ST_OK;
    _delay_us(50);
  }
  return ST_NVM_TIMEOUT;
}

static uint8_t nvm_cmd(uint8_t c) { return sts(NVM_CTRLA, c); }

static uint8_t write_page(uint16_t addr, const uint8_t *data, uint8_t n) {
  uint8_t e;
  if ((e = nvm_wait())) return e;
  if ((e = nvm_cmd(NVM_PBC))) return e;            // очистить буфер страницы
  if ((e = nvm_wait())) return e;
  if ((e = st_ptr(addr))) return e;
  if ((e = st_ptr_inc(data, n))) return e;
  if ((e = nvm_cmd(NVM_ERWP))) return e;           // стереть и записать страницу
  return nvm_wait();
}

// ---------------------------------------------------------------------
//  Публичные функции
// ---------------------------------------------------------------------

uint8_t updi_open(uint8_t *statusa) {
  pins_release();
  timer1_free();
  L_IN();
  for (uint8_t attempt = 0; attempt < 2; attempt++) {
    double_break();
    stcs(CS_CTRLB, CTRLB_CCDETDIS);
    stcs(CS_CTRLA, CTRLA_IBDLY | CTRLA_GT_8);
    uint8_t v = 0;
    if (ldcs(CS_STATUSA, &v) == ST_OK && v != 0) {
      *statusa = v;
      return ST_OK;
    }
  }
  return ST_UPDI_NOLINK;
}

void updi_close() {
  // Питание прищепки ATtiny (X3, контакт 2) — постоянные +5 В, холодного старта
  // между сеансами нет, поэтому UPDIDIS не пишем: отключённый UPDI мог бы
  // проснуться только после сброса по питанию.
  reset_pulse();
  pins_release();
}

uint8_t updi_sys_status(uint8_t *ss) { return ldcs(ASI_SYS_STATUS, ss); }

uint8_t updi_enter_prog() {
  uint8_t ss, ks, e;
  if ((e = ldcs(ASI_SYS_STATUS, &ss))) return e;
  if (ss & SYS_NVMPROG) return ST_OK;
  key("NVMProg ");
  if ((e = ldcs(ASI_KEY_STATUS, &ks))) return e;
  if (!(ks & KEY_NVMPROG_BIT)) return ST_UPDI_KEY;
  reset_pulse();
  if ((e = wait_unlocked(100))) return e;          // lock стоит — ST_UPDI_LOCKED
  if ((e = ldcs(ASI_SYS_STATUS, &ss))) return e;
  return (ss & SYS_NVMPROG) ? ST_OK : ST_UPDI_NOPROG;
}

uint8_t updi_erase_and_enter() {
  uint8_t ks, e;
  key("NVMErase");
  if ((e = ldcs(ASI_KEY_STATUS, &ks))) return e;
  if (!(ks & KEY_CHIPERASE_BIT)) return ST_UPDI_KEY;
  reset_pulse();
  if ((e = wait_unlocked(500))) return e;
  return updi_enter_prog();
}

uint8_t updi_read(uint16_t addr, uint8_t *buf, uint16_t n) {
  if (n == 1) return lds(addr, buf);
  uint8_t e = st_ptr(addr);
  if (e) return e;
  return ld_ptr_inc(buf, n);
}

uint8_t updi_write_flash_page(uint16_t offset, const uint8_t *data, uint8_t n) {
  return write_page(FLASH_BASE + offset, data, n);
}

uint8_t updi_write_eeprom(uint16_t offset, const uint8_t *data, uint8_t n) {
  return write_page(EEPROM_BASE + offset, data, n);
}

uint8_t updi_write_fuse(uint16_t addr, uint8_t value) {
  uint8_t e;
  if ((e = nvm_wait())) return e;
  if ((e = sts(NVM_ADDR, (uint8_t)addr))) return e;
  if ((e = sts(NVM_ADDR + 1, (uint8_t)(addr >> 8)))) return e;
  if ((e = sts(NVM_DATA, value))) return e;
  if ((e = nvm_cmd(NVM_WFU))) return e;
  return nvm_wait();
}
