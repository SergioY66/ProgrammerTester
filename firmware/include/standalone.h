#pragma once
#include <Arduino.h>

// Автономный режим: кнопка A1 — тест карты, A2 — тест чипа, результат на OLED.
void standalone_init();
void standalone_idle_screen();
void standalone_poll();
