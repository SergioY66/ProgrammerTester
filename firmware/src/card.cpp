// ======================================================================
//  Тест карты Flash — перенесён из CardTester (src/main.cpp, src/Tests.cpp)
//  без изменения логики обмена.
//
//  Последовательная передача на основе I2C, но без квитирования.
//  SCL генерирует Timer1 (OC1A = D9), каждый фронт SCL ловит PCINT1,
//  по нему выставляется/читается SDA (D4).
//  Тест: послать 30 30 00, прочитать 2 байта, ожидается 01 14.
// ======================================================================

#include "common.h"
#include "card.h"
#include <util/delay.h>

#define TIMER_START                        \
  TCCR1A = (1 << COM1A0) | (1 << WGM10);   \
  TCCR1B = (1 << CS11) | (1 << WGM13)
#define TIMER_STOP \
  TCCR1A = 0;      \
  TCCR1B = 0

#define SDA_OUT    DDRD |= (1 << SDA_BIT); SDA_LO
#define SDA_IN     DDRD &= ~(1 << SDA_BIT)
#define SDA_IN_PUP DDRD &= ~(1 << SDA_BIT); PORTD |= (1 << SDA_BIT)
#define SDA_LO     PORTD &= ~(1 << SDA_BIT)
#define CHECK_SDA  ((PIND & (1 << SDA_BIT)) ? 1 : 0)

#define SCL_OUT    DDRB |= (1 << SCL_BIT)
#define SCL_IN     DDRB &= ~(1 << SCL_BIT)
#define SCL_LO     PORTB &= ~(1 << SCL_BIT)
#define CHECK_SCL  ((PINB & (1 << SCL_BIT)) ? 1 : 0)

#define SCL_INT_EN  PCICR = (1 << PCIE0); PCMSK0 |= (1 << PCINT1)
#define SCL_INT_DIS PCICR = 0; PCMSK0 &= ~(1 << PCINT1)

// state
#define START    0
#define DATA_IN  1
#define DATA_OUT 2
#define STOP     3
#define IDLE     4
#define END      5
#define LAST_BIT 6

static const uint8_t message[] = {0x30, 0x30, 0x00};
#define BYTES_TO_WRITE 3
#define BYTES_TO_READ  2

static volatile uint8_t state;
static volatile uint8_t bitIndex, dataIndex;
static volatile uint8_t bytesToRead, bytesToWrite;
static volatile uint8_t dataIn[4];
static const uint8_t *transmitterBuffer;

static void MessagePrep(const uint8_t *messToSend, uint8_t lenSend, uint8_t lenGet) {
  transmitterBuffer = messToSend;
  bytesToRead = lenGet;
  bytesToWrite = lenSend;
  for (uint8_t i = 0; i < sizeof(dataIn); i++) dataIn[i] = 0;
  bitIndex = 0;
  dataIndex = 0;
}

uint8_t card_test(uint8_t *out) {
  pins_release();
  _delay_ms(500);                                  // пауза покоя перед обменом (карта питается постоянно)
  SDA_IN_PUP;
  SCL_OUT;

  MessagePrep(message, BYTES_TO_WRITE, BYTES_TO_READ);

  state = START;
  TCNT1 = 0;
  SCL_LO;
  OCR1A = 400;
  SCL_INT_EN;
  TIMER_START;

  uint16_t ms = 0;
  while (state != IDLE && ms < 3000) { _delay_ms(1); ms++; }

  SCL_INT_DIS;
  TIMER_STOP;
  SCL_IN;
  SDA_IN;
  pins_release();

  out[0] = dataIn[0];
  out[1] = dataIn[1];
  return state == IDLE ? ST_OK : ST_CARD_TIMEOUT;
}

// SCL interrupt
ISR(PCINT0_vect) {
  if (CHECK_SCL == 1) {                            // SCL в «1»
    switch (state) {
    case START:
      OCR1A = 100;
      _delay_us(70);
      SDA_OUT;                                     // старт: SDA в «0»
      SDA_LO;
      state = DATA_OUT;
      break;

    case DATA_IN:
      if (CHECK_SDA) dataIn[dataIndex] |= (1 << bitIndex);
      if (bitIndex == 7) {
        if (dataIndex == (bytesToRead - 1)) {
          bytesToRead = 0;
          state = STOP;
        } else {
          dataIndex++;
          bitIndex = 0;
        }
      } else
        bitIndex++;
      break;

    case STOP:
      _delay_us(30);
      SDA_IN_PUP;
      if (bytesToRead > 0) {
        bitIndex = 0;
        dataIndex = 0;
        state = DATA_IN;
      } else {
        state = END;
      }
      break;

    case END:
      state = IDLE;
      break;
    }
  } else {                                         // SCL в «0»
    switch (state) {
    case DATA_OUT:
      _delay_us(30);
      if (!(transmitterBuffer[dataIndex] & (1 << bitIndex))) {
        SDA_OUT;
        SDA_LO;
      } else {
        SDA_IN_PUP;
      }
      if (bitIndex < 7) {
        bitIndex++;
      } else {
        bitIndex = 0;
        if (dataIndex < (bytesToWrite - 1))
          dataIndex++;
        else
          state = STOP;
      }
      break;

    case LAST_BIT:
      state = STOP;
      SDA_OUT;
      SDA_LO;
      break;
    }
  }
}
