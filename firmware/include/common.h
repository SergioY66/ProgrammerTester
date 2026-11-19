#pragma once
#include <Arduino.h>

// ======================================================================
//  Выводы Nano
//
//  Три разъёма; в работе одновременно только один.
//
//  X1 — SIM-разъём карты Flash: прошивка и тест
//    VCC ← пин 5V Nano (постоянно, не D6; с 2026-11-06), UPDI ↔ D5 (PD5) через 470 Ом, GND,
//    SDA ↔ D4 (PD4), SCL ← D9 (PB1, OC1A) — тест как в CardTester
//
//  X2 — тест чипа Zoom (1-Wire, паразитное питание): GND, DQ ↔ D2 (PD2);
//    подтяжка D3 (PD3) → 2 кОм → D2
//
//  X3 — IDC 2×3, раскладка Microchip (ISP-6 / UPDI-6), прищепки ATtiny и AT89:
//    1  MISO → D12 (PB4)  и  UPDI ↔ D5 через 470 Ом   2  +5 В (пин 5V Nano)
//    3  SCK  ← D13 (PB5)                             4  MOSI ← D11 (PB3)
//    5  RST  ← D10 (PB2)                             6  GND
//  Контакт 1 общий: D12 и D5 соединены на нём, вне своего режима оба —
//  входы без подтяжки (pins_release). Прищепка ATtiny: 1, 2, 6.
//  Прищепка AT89S8253: 1, 3, 4, 5, 6; контакт 2 не подключается — голова
//  питается от своего +5 В.
//  Контакт 2 питается напрямую с пина 5V Nano (с 2026-11-06), НЕ от D6:
//  включить/выключить его программой нельзя, питание есть всегда. D6 свободен
//  (карта в X1 тоже питается с 5V, прошивка D6 больше не трогает). Холодного старта нет.
// ======================================================================

#define UPDI_BIT   PD5
#define SDA_BIT    PD4
#define SCL_BIT    PB1
#define DQ_BIT     PD2
#define PULL_BIT   PD3
#define RST89_BIT  PB2      // D10 (SS — в режиме ведущего SPI обязан быть выходом)
#define MOSI_BIT   PB3      // D11
#define MISO_BIT   PB4      // D12
#define SCK_BIT    PB5      // D13

// Кнопки автономного режима (на GND, внутренняя подтяжка)
#define BTN_CARD_BIT PC1    // A1 — тест карты
#define BTN_CHIP_BIT PC2    // A2 — тест чипа

// Питание изделий (X1, X3 контакт 2) — постоянно с пина 5V Nano, программой не
// управляется. D6 свободен.

// Timer1 — свободный счёт без делителя (1 такт = 62,5 нс), без прерываний.
// Им отмеряются биты UPDI и слоты 1-Wire. Тест карты перенастраивает Timer1 под себя.
#define US(x) ((uint16_t)((x) * 16U))
static inline void timer1_free() {
  TIMSK1 = 0;
  TCCR1A = 0;
  TCCR1B = _BV(CS10);
}

// Все выводы изделия — в безопасное состояние, питание выключено.
void pins_release();

// Коды ответа Nano → ПК (расшифровка в pc/device.py)
enum : uint8_t {
  ST_OK            = 0,
  ST_BAD_PACKET    = 1,
  ST_UNKNOWN_CMD   = 2,
  ST_BAD_ARGS      = 3,

  ST_UPDI_TIMEOUT  = 10,   // изделие не ответило по UPDI
  ST_UPDI_NOACK    = 11,   // вместо ACK пришло другое
  ST_UPDI_FRAME    = 12,   // ошибка чётности / стоп-бита
  ST_UPDI_NOLINK   = 13,   // после BREAK нет связи
  ST_UPDI_KEY      = 14,   // ключ не принят
  ST_UPDI_LOCKED   = 15,   // чип заблокирован (lock)
  ST_UPDI_NOPROG   = 16,   // не вошёл в режим программирования
  ST_NVM_TIMEOUT   = 17,
  ST_NVM_WRERROR   = 18,

  ST_CARD_TIMEOUT  = 20,

  ST_OW_NOPRESENCE = 30,
  ST_OW_LINE_LOW   = 31,
  ST_OW_DIFF       = 32,

  ST_ISP_NOSYNC    = 40,   // AT89: нет сигнатуры 1E — не вошёл в программирование
  ST_ISP_NOTOPEN   = 41,   // команда ISP без ISP_OPEN
  ST_ISP_NOSIG     = 42,   // сигнатура не опознана (чип закрыт или не в программировании)
};
