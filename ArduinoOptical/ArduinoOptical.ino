// Uno R3 optical-only A0 stream with a one-bit jump marker.
// Select "raw" in BenesController at the same 1,000,000 or 2,000,000 baud.
// Every sample is exactly two bytes:
//   0xxxxxxx = top 7 bits of the 10-bit A0 optical ADC value
//   110bbb0J = status; bbb = low 3 ADC bits, J=1 marks a sudden A0 change
// Only status bytes start with 11, allowing the PC to resynchronize.
// Only A0 is connected. Keep its analog voltage between 0 and 5 V.
// The jump detector is a heuristic: real optical steps can also set the bit.

const uint8_t OPTICAL_PIN = A0;
const uint8_t JUMP_THRESHOLD_ADC = 24; // 7-bit counts; tune for your detector
uint8_t previousSample = 0;
bool havePrevious = false;

void setup() {
  Serial.begin(2000000);
  // ADC prescaler 32 (0b101): 16 MHz / 32 = 500 kHz ADC clock.
  // For a faster, less accurate stream, use 0b100 (prescaler 16)
  // and keep Serial.begin(2000000). Measure the achieved rate in the GUI.
  // Faster than the 50-200 kHz specified for maximum 10-bit accuracy,
  // so the lowest one or two of the low 3 bits are mostly noise.
  ADCSRA = (ADCSRA & 0b11111000) | 0b101;
}

void loop() {
  const uint16_t adc = analogRead(OPTICAL_PIN);
  const uint8_t optical = adc >> 3;
  const uint8_t delta = havePrevious
      ? (optical > previousSample ? optical - previousSample : previousSample - optical)
      : 0;
  const uint8_t jump = havePrevious && delta >= JUMP_THRESHOLD_ADC ? 1 : 0;
  previousSample = optical;
  havePrevious = true;
  Serial.write(optical);
  Serial.write(0xC0 | ((adc & 0b111) << 2) | jump);
}
