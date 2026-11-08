// ======================================================================
//  Пассивный сниффер 1-Wire — порт из ZoomLamp/.../onewire_sniffer.ino
//  (проверен на железе 14.10.2026), вывод — бинарные записи (sniff.h).
//
//  Спад на линии открывает слот — окно 30 мкс. Спады внутри окна игнорируются:
//  при чтении «0» голова отпускает линию на 10–15-й мкс, а чип прижимает её сам.
//  В конце окна: линия свободна — «0», если в окне линию прижимали повторно
//  или последний подъём позже T_ZERO (24 мкс), иначе «1»; линия в нуле — бит
//  по подъёму: по длине нуля отличаются «0», присутствие и сброс (>= 380 мкс).
//
//  Timer1 считает на 16 МГц, фронты ловит INT0, конец окна — OCR1A.
//  На время захвата отключены Timer0 и прерывания UART (вывод опросом),
//  чтобы ничто не задерживало захват. Подробнее: sniff.h.
// ======================================================================

#include "common.h"
#include "sniff.h"
#include <avr/interrupt.h>

#define TK(x)          ((uint32_t)(x) * 16UL)      // мкс → такты
#define T_WINDOW       TK(30)
#define T_ZERO         TK(24)
#define T_BIT0_MAX     TK(120)
#define T_RESET_MIN    TK(380)
#define T_PRESENCE_WIN TK(120)
#define T_GAP_MIN      TK(1000)

// ---- очередь событий: прерывания → основной цикл ----
enum { EV_FALL, EV_RISE, EV_S0, EV_S1, EV_S1R };

#define EVQ 32                                     // степень двойки
static uint32_t *ev_t;
static uint8_t  *ev_kind;
static volatile uint8_t  ev_head, ev_tail, ev_lost;
static volatile uint16_t ovf_cnt;
static volatile uint8_t  slot_open;                // идёт окно слота после спада
static volatile uint8_t  refall;                   // в окне линию прижали повторно
static volatile uint8_t  last_lvl;

static inline void ev_push(uint32_t t, uint8_t kind) {
  uint8_t h = ev_head;
  uint8_t n = (h + 1) & (EVQ - 1);
  if (n == ev_tail) { if (ev_lost < 255) ev_lost++; return; }
  ev_t[h] = t;
  ev_kind[h] = kind;
  ev_head = n;
}

static inline uint32_t now_ticks() {
  uint16_t tc = TCNT1;
  uint16_t hi = ovf_cnt;
  if ((TIFR1 & _BV(TOV1)) && tc < 0x8000) hi++;    // переполнение ещё не обработано
  return ((uint32_t)hi << 16) | tc;
}

ISR(TIMER1_OVF_vect) { ovf_cnt++; }

// held — на ноге действительно ноль (а не выброс, который уже кончился)
static inline void on_fall_isr(uint32_t t, uint8_t held) {
  if (slot_open) {                                 // повторный спад в окне — тот же слот
    if (held) refall = 1;
    return;
  }
  slot_open = 1;
  refall = 0;
  ev_push(t, EV_FALL);
  OCR1A = (uint16_t)t + (uint16_t)T_WINDOW;
  TIFR1 = _BV(OCF1A);
  TIMSK1 |= _BV(OCIE1A);
}

ISR(INT0_vect) {
  uint32_t t = now_ticks();
  uint8_t lvl = (PIND & _BV(2)) ? 1 : 0;
  if (lvl == last_lvl) {
    // Два фронта успели пройти до входа в прерывание (флаг INT0 один).
    if (lvl) { on_fall_isr(t, 0); ev_push(t, EV_RISE); }
    else     { ev_push(t, EV_RISE); on_fall_isr(t, 1); }
  } else if (lvl) {
    ev_push(t, EV_RISE);
  } else {
    on_fall_isr(t, 1);
  }
  last_lvl = lvl;
}

ISR(TIMER1_COMPA_vect) {
  uint8_t lvl = (PIND & _BV(2)) ? 1 : 0;
  TIMSK1 &= ~_BV(OCIE1A);
  slot_open = 0;
  ev_push(now_ticks(), !lvl ? EV_S0 : (refall ? EV_S1R : EV_S1));
}

// ---- вывод: кольцо 256 байт, UART опрашивается без прерываний ----
static uint8_t *obuf;
static uint8_t  o_head, o_tail;                    // uint8_t: естественный оборот на 256
static uint16_t o_lost;

static bool room(uint8_t n) {
  if ((uint8_t)(o_tail - o_head - 1) < n) { o_lost++; return false; }
  return true;
}
static inline void put(uint8_t c) { obuf[o_head++] = c; }

static void emit_slot(uint8_t flags, uint32_t us) {
  if (!room(2)) return;
  put(0x80 | flags);
  put(us > 127 ? 127 : (uint8_t)us);
}

static void put_val(uint32_t v, uint8_t nbytes) {
  uint32_t max = (1UL << (7 * nbytes)) - 1;
  if (v > max) v = max;
  for (uint8_t i = 0; i < nbytes; i++) { put(v & 0x7F); v >>= 7; }
}

static void emit_val(uint8_t kind, uint32_t v, uint8_t nbytes) {
  if (!room(1 + nbytes)) return;
  put(kind);
  put_val(v, nbytes);
}

static void emit_reset(uint32_t us, uint32_t ms) {
  if (!room(7)) return;
  put(0x90);
  put_val(us, 2);
  put_val(ms, 4);
}

static void uart_service() {
  if (o_head != o_tail && (UCSR0A & _BV(UDRE0))) UDR0 = obuf[o_tail++];
}

// ---- разбор событий ----
static uint8_t  started, after_reset, waiting, sampled_low, have_rise;
static uint32_t t_fall, t_rise, t_last, ms_total, ms_rem;

static void advance_clock(uint32_t t) {
  ms_rem += t - t_last;
  t_last = t;
  if (ms_rem >= 16000UL) { ms_total += ms_rem / 16000UL; ms_rem %= 16000UL; }
}

static void on_fall(uint32_t t) {
  if (have_rise) {
    uint32_t high = t - t_rise;
    if (started && high >= T_GAP_MIN) emit_val(0x94, high >> 4, 3);
    if (after_reset && high > T_PRESENCE_WIN) { if (room(1)) put(0x92); after_reset = 0; }
  }
  t_fall = t;
  waiting = 1;
  sampled_low = 0;
}

static void on_rise(uint32_t t) {
  t_rise = t;
  have_rise = 1;
  if (!waiting || !sampled_low) return;            // подъём внутри окна — решит конец окна
  waiting = 0;
  uint32_t low = t - t_fall;
  if (low >= T_RESET_MIN) {
    emit_reset(low >> 4, ms_total);
    started = 1;
    after_reset = 1;
    return;
  }
  if (!started) return;                            // до первого сброса — середина обмена
  if (after_reset) { after_reset = 0; emit_val(0x91, low >> 4, 2); return; }
  if (low < T_BIT0_MAX) emit_slot(4, low >> 4);    // бит 0, ноль держался до конца окна
  else emit_val(0x93, low >> 4, 2);
}

static void on_sample(uint8_t high, uint8_t refell, uint32_t t) {
  (void)t;
  if (!waiting) return;
  if (!high) { sampled_low = 1; return; }          // ноль держится — решит подъём
  waiting = 0;                                     // линия свободна — решает последний подъём
  if (!started) return;
  if (after_reset) {                               // короткий импульс сразу после сброса
    after_reset = 0;
    emit_val(0x91, (t_rise - t_fall) >> 4, 2);
    return;
  }
  uint8_t bit = (refell || t_rise - t_fall >= T_ZERO) ? 0 : 1;
  emit_slot((refell ? 2 : 0) | bit, (t_rise - t_fall) >> 4);
}

void sniff_run(uint8_t *evmem, uint8_t *outmem) {
  ev_t = (uint32_t *)evmem;
  ev_kind = evmem + EVQ * 4;
  obuf = outmem;
  o_head = o_tail = 0;
  o_lost = 0;
  ev_head = ev_tail = ev_lost = 0;
  ovf_cnt = 0;
  slot_open = refall = 0;
  started = after_reset = waiting = sampled_low = have_rise = 0;
  t_fall = t_rise = t_last = ms_total = ms_rem = 0;

  Serial.flush();
  while (Serial.available()) Serial.read();
  uint8_t tim0 = TIMSK0;

  cli();
  TIMSK0 = 0;                                      // millis() на время захвата не нужен
  TCCR1A = 0;
  TCCR1B = _BV(CS10);
  TCNT1 = 0;
  TIFR1 = 0xFF;
  TIMSK1 = _BV(TOIE1);
  last_lvl = (PIND & _BV(2)) ? 1 : 0;
  EICRA = _BV(ISC00);                              // INT0 на любое изменение уровня
  EIFR = _BV(INTF0);
  EIMSK = _BV(INT0);
  sei();

  emit_val(0x97, last_lvl, 1);

  for (;;) {
    while (ev_tail != ev_head) {
      uint8_t i = ev_tail;
      uint32_t t = ev_t[i];
      uint8_t k = ev_kind[i];
      ev_tail = (i + 1) & (EVQ - 1);
      advance_clock(t);
      if (k == EV_FALL)      on_fall(t);
      else if (k == EV_RISE) on_rise(t);
      else                   on_sample(k != EV_S0, k == EV_S1R, t);
      uart_service();
    }
    if (ev_lost) {
      uint8_t n;
      cli(); n = ev_lost; ev_lost = 0; sei();
      emit_val(0x95, n, 1);
    }
    if (o_lost && room(3)) {
      uint16_t n = o_lost;
      o_lost = 0;
      put(0x96); put_val(n, 2);
    }
    uart_service();
    if (Serial.available()) break;                 // любой байт от ПК — стоп
  }

  cli();
  EIMSK = 0;
  EICRA = 0;
  TIMSK1 = 0;
  TIFR1 = 0xFF;
  sei();

  while (o_head != o_tail) uart_service();
  while (!(UCSR0A & _BV(UDRE0))) ;
  UCSR0A |= _BV(TXC0);                             // сбросить старый флаг «передача завершена»
  UDR0 = 0xFF;
  while (!(UCSR0A & _BV(TXC0))) ;

  TIMSK0 = tim0;
  while (Serial.available()) Serial.read();
  timer1_free();
}
