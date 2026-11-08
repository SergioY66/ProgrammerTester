#pragma once
#include <Arduino.h>

// OLED SSD1306 128×64, I2C 0x3C (A4 — SDA, A5 — SCL), как в CardTester.
// Три строки крупным шрифтом, по центру. nullptr — пустая строка.
void disp_init();
void disp_screen(const char *l1, const char *l2 = nullptr, const char *l3 = nullptr);
