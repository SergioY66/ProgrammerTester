#pragma once
#include <Arduino.h>

// Программатор UPDI для tinyAVR 0/1-series (ATtiny816 / ATtiny1616).
// Все функции вызываются при запрещённых прерываниях (см. main.cpp).
// Возврат — код ST_* из common.h.

uint8_t updi_open(uint8_t *statusa);            // питание, BREAK, настройка связи
void    updi_close();                           // выйти из программирования, снять питание
uint8_t updi_sys_status(uint8_t *ss);           // ASI_SYS_STATUS (бит 0 — LOCKSTATUS)
uint8_t updi_erase_and_enter();                 // стирание ключом (снимает lock) + режим программирования
uint8_t updi_enter_prog();                      // режим программирования без стирания
uint8_t updi_read(uint16_t addr, uint8_t *buf, uint16_t n);       // n ≤ 256
uint8_t updi_write_flash_page(uint16_t offset, const uint8_t *data, uint8_t n);
uint8_t updi_write_eeprom(uint16_t offset, const uint8_t *data, uint8_t n);
uint8_t updi_write_fuse(uint16_t addr, uint8_t value);
