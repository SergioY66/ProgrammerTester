#pragma once
#include <Arduino.h>

#define CHIP_MEM 0x90

// Тест чипа Zoom: паразитное питание от линии 1-Wire, чтение ROM и памяти
// 0x00–0x8F дважды со сверкой. charge_ms — зарядка конденсатора чипа через
// подтяжку перед первым сбросом.
// rom[8], mem[CHIP_MEM] заполняются при ST_OK и ST_OW_DIFF.
uint8_t chip_test(uint16_t charge_ms, uint8_t *rom, uint8_t *mem);
