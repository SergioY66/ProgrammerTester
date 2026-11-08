#pragma once
#include <Arduino.h>

// Программатор ISP для AT89S8253 (голова ZoomLamp): SPI D11/D12/D13, RST — D10.
// Голова питается сама, у неё должен работать кварц. Возврат — код ST_* из common.h.

enum : uint8_t {
  ISP_CODE    = 0,   // Flash 12 КБ, страница 64 Б
  ISP_DATA    = 1,   // EEPROM 2 КБ, страница 32 Б
  ISP_ATMEL   = 2,   // сигнатура Atmel, 64 Б (1E 73 01 по 0x30)
  ISP_USER    = 3,   // сигнатура пользователя, 64 Б
  ISP_LOCK    = 4,   // биты защиты, 1 Б (бит = 0 — запрограммирован)
  ISP_FUSES   = 5,   // фьюзы, 1 Б
};

uint8_t isp_open(uint8_t *out);                 // out: режим SPI, сигнатура[3]
void    isp_close();
uint8_t isp_read(uint8_t space, uint16_t addr, uint8_t *buf, uint8_t n);
uint8_t isp_write_page(uint8_t space, uint16_t addr, const uint8_t *data, uint8_t n);
uint8_t isp_erase();
void    isp_settle();                           // дождаться конца записи страницы (если идёт)
