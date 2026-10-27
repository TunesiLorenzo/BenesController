// Archived tagged A0/A1 sketch. The current "raw" mode uses
// ArduinoOptical/ArduinoOptical.ino instead; this two-byte A0/A1 format is
// not accepted by the optical-only decoder. The "dual" mode expects an older
// six-byte framed sketch, not this archived tagged sketch.
// Uno R3 tagged A0/A1 stream for BenesController, at 2,000,000 baud
// (1,000,000 also works; set the same baud in the GUI).
// Streams continuously from power-up as fast as the loop runs; no commands.
// Each pair is two bytes holding 7-bit values (0-127), tagged by the top bit:
//   0xxxxxxx = A0 optical, then 1xxxxxxx = A1 electrical.
// The PC pairs each A0 byte with the A1 byte after it, so it can join the
// stream at any byte and resynchronizes by itself after a dropped byte.
// A1 monitors voltage-board pin 8. Keep BOTH analog inputs within 0-5 V.

const int analogPin = A0;
const int elecPin = A1;

void setup() {
  Serial.begin(2000000);

  // ADC prescaler = 32
  ADCSRA = (ADCSRA & 0b11111000) | 0b101;
}

void loop() {
  // Convert 10-bit ADC, 0-1023, to 7-bit, 0-127; bit 7 carries the channel.
  uint8_t sample = analogRead(analogPin) >> 3;
  uint8_t sample2 = analogRead(elecPin) >> 3;

  Serial.write(sample);         // 0xxxxxxx = A0
  Serial.write(0x80 | sample2); // 1xxxxxxx = A1
}
