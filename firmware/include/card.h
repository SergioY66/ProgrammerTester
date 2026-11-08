#pragma once
#include <Arduino.h>

// Тест карты Flash. out[0..1] — прочитанные байты (у годной карты 01 14).
uint8_t card_test(uint8_t *out);
