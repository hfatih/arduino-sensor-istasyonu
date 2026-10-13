/*
   Sensor Istasyonu - Arduino Uno R3

     BME280  -> donanim SPI  (CS D10, MOSI D11, MISO D12, SCK D13)
     OLED    -> I2C          (SDA A4, SCL A5)  SSD1306 128x64 varsayildi
     DHT11   -> D2           (istege bagli; BMP280'de eksik olan nemi verir)
     USB seri-> 115200 baud, satir basina tek JSON

   OLED kendiliginden doner: 30 sn bilgisayar (CPU/RAM/GPU), 5 sn sensor.
   Bilgisayar verisi panelden gelir; panel kapaliysa yalnizca sensor gosterilir.

   BME280 diye satilan modullerin bir kismi BMP280 cikar (cip kimligi 0x58).
   Kod ikisini de kabul eder; BMP280'de nem olcumu yoktur, "--" gorunur.

   Kutuphaneler: Adafruit BME280 Library 2.2.4, Adafruit BMP280 Library 2.6.8,
                 U8g2 2.35.x (U8x8 kismi)

   DIKKAT: BME280 cipi 3,3 V'tur. Modulde regulator + seviye donusturucu yoksa
   5 V'luk Uno pinlerine dogrudan baglanmaz. Bkz. KURULUM.md, bolum A.
*/
#include <SPI.h>
#include <Wire.h>
#include <Adafruit_BME280.h>
#include <Adafruit_BMP280.h>
#include <U8x8lib.h>

// ===================== AYARLAR =====================
#define FW_VERSION "1.3.0"

const uint8_t PIN_BME_CS = 10;
const uint8_t PIN_DHT = 2;

// OLED: 0x3C en yaygin adres, bazi modullerde 0x3D. SH1106 (1,3") ekranda
// asagidaki sinifi U8X8_SH1106_128X64_NONAME_HW_I2C yap.
const uint8_t OLED_I2C_ADDR = 0x3C;
U8X8_SSD1306_128X64_NONAME_HW_I2C oled(U8X8_PIN_NONE);

// Modul kendi isisiyla 1-2 C fazla gosterebilir; referans termometreye gore ayarla.
const float TEMP_OFFSET_C = 0.0f;
// DHT11 nemde +-5 puan sapabilir; referans nem olcere gore ayarla.
const float DHT_HUM_OFFSET = 0.0f;

const uint32_t SAMPLE_PERIOD_MS = 1000;    // olcum + telemetri + ekran
const uint32_t RETRY_PERIOD_MS = 5000;     // sensor yeniden baslatma araligi
const uint32_t PC_LINK_TIMEOUT_MS = 5000;  // bu sure PC verisi gelmezse "PC yok"
const uint32_t PC_PAGE_MS = 30000;         // bilgisayar sayfasinin ekranda kalma suresi
const uint32_t SENSOR_PAGE_MS = 5000;      // sensor sayfasinin ekranda kalma suresi
const uint8_t PAGE_SENSOR = 0, PAGE_PC = 1;
const uint8_t PAGE_COUNT = 2;
// ===================================================

Adafruit_BME280 bme(PIN_BME_CS);  // tek parametre = donanim SPI, 1 MHz
Adafruit_BMP280 bmp(PIN_BME_CS);  // ayni pinler; cip BMP280 cikarsa bu kullanilir
bool isBmp = false;
float dhtHum = NAN;       // DHT11'den son gecerli nem; takili degilse NAN
uint8_t dhtFails = 3;
// Seviye donusturucu 1 MHz'te kenarlari yumusatip okuma hatasi veriyorsa ayni
// pinlerde yavas (yazilim) SPI: ustteki satir yerine
// Adafruit_BME280 bme(PIN_BME_CS, 11, 12, 13);

enum SensorErr : uint8_t { ERR_NONE, ERR_NOT_FOUND, ERR_READ, ERR_RANGE };

float tempC = NAN, humPct = NAN, presHpa = NAN;
bool sensorReady = false;
SensorErr sensorErr = ERR_NOT_FOUND;
uint32_t sampleCount = 0;
uint32_t lastGoodMs = 0;
uint32_t lastInitMs = 0;
uint32_t lastSampleMs = 0;

uint8_t page = PAGE_SENSOR;
uint32_t pageSinceMs = 0;
bool oledOk = false;

// Panelden gelen bilgisayar verisi; -1 = bilinmiyor
int16_t pcCpu = -1, pcRam = -1, pcGpu = -1, pcGpuTemp = -1;
int16_t pcRamUsed = -1, pcRamTotal = -1, pcVramUsed = -1, pcVramTotal = -1;  // 0,1 GB
char pcCpuName[17] = "", pcGpuName[17] = "";

uint32_t lastCmdMs = 0;
bool pcSeen = false;

char rxBuf[64];
uint8_t rxLen = 0;
bool rxOverflow = false;

// ---------------- yardimcilar ----------------
bool sensorOk() {
  return sensorReady && sensorErr == ERR_NONE;
}

const __FlashStringHelper *errName(SensorErr e) {
  switch (e) {
    case ERR_NOT_FOUND: return F("not_found");
    case ERR_READ: return F("read_failed");
    case ERR_RANGE: return F("out_of_range");
    default: return F("none");
  }
}

const __FlashStringHelper *boolName(bool b) {
  return b ? F("true") : F("false");
}

// Gecersiz olcum 0 degil null olarak gider.
void printNum(float v) {
  if (isnan(v)) Serial.print(F("null"));
  else Serial.print(v, 1);
}

// ---------------- JSON cikisi ----------------
void sendTelemetry(uint32_t now) {
  Serial.print(F("{\"type\":\"telemetry\",\"temperature_c\":"));
  printNum(tempC);
  Serial.print(F(",\"humidity_pct\":"));
  printNum(humPct);
  Serial.print(F(",\"pressure_hpa\":"));
  printNum(presHpa);
  Serial.print(F(",\"sensor_ok\":"));
  Serial.print(boolName(sensorOk()));
  Serial.print(F(",\"chip\":\""));
  Serial.print(isBmp ? F("BMP280") : F("BME280"));
  Serial.print(F("\",\"page\":"));
  Serial.print(page);
  Serial.print(F(",\"uptime_ms\":"));
  Serial.print(now);
  Serial.print(F(",\"samples\":"));
  Serial.print(sampleCount);
  Serial.println('}');
}

// chip_id: 96 (0x60) BME280, 88 (0x58) BMP280 (nem yok), 0/255 kablo sorunu.
void sendSensorError() {
  Serial.print(F("{\"type\":\"sensor_error\",\"code\":\""));
  Serial.print(errName(sensorErr));
  Serial.print(F("\",\"chip_id\":"));
  Serial.print(isBmp ? 0x58 : (int)bme.sensorID());
  Serial.print(F(",\"uptime_ms\":"));
  Serial.print(millis());
  Serial.println('}');
}

void sendStatus(const __FlashStringHelper *type) {
  Serial.print(F("{\"type\":\""));
  Serial.print(type);
  Serial.print(F("\",\"fw\":\"" FW_VERSION "\",\"pages\":"));
  Serial.print(PAGE_COUNT);
  Serial.print(F(",\"oled_ok\":"));
  Serial.print(boolName(oledOk));
  Serial.print(F(",\"sensor_ok\":"));
  Serial.print(boolName(sensorOk()));
  Serial.print(F(",\"sensor_error\":\""));
  Serial.print(errName(sensorOk() ? ERR_NONE : sensorErr));
  Serial.print(F("\",\"page\":"));
  Serial.print(page);
  Serial.print(F(",\"uptime_ms\":"));
  Serial.print(millis());
  Serial.println('}');
}

void sendPage(const __FlashStringHelper *source) {
  Serial.print(F("{\"type\":\"page\",\"page\":"));
  Serial.print(page);
  Serial.print(F(",\"source\":\""));
  Serial.print(source);
  Serial.println(F("\"}"));
}

void sendError(const __FlashStringHelper *code) {
  Serial.print(F("{\"type\":\"error\",\"code\":\""));
  Serial.print(code);
  Serial.println(F("\"}"));
}

// ---------------- OLED ----------------
// U8x8 karakter modu: RAM'de kare tamponu yok, yalnizca yazilan hucre guncellenir.
// 16 sutun x 8 satir. Fontta Turkce harf ve derece isareti yok.
void fmtVal(char *buf, float v, uint8_t width) {
  if (isnan(v)) {
    memset(buf, ' ', width);
    buf[width - 2] = '-';
    buf[width - 1] = '-';
    buf[width] = '\0';
  } else {
    dtostrf(v, width, 1, buf);
  }
}

void oledText(uint8_t x, uint8_t y, const __FlashStringHelper *s) {
  oled.setCursor(x, y);
  oled.print(s);
}

void fmtInt(char *buf, int16_t v) {
  if (v < 0) strcpy_P(buf, PSTR(" --"));
  else snprintf_P(buf, 4, PSTR("%3d"), v);
}

// "kullanilan/toplam GB" (degerler 0,1 GB biriminde), width sutuna saga yasli.
void drawGb(uint8_t x, uint8_t y, uint8_t width, int16_t used, int16_t total) {
  char txt[20], out[17];
  if (used < 0 || total < 0) strcpy_P(txt, PSTR("--"));
  else snprintf_P(txt, sizeof(txt), PSTR("%d.%d/%d.%d GB"), used / 10, used % 10, total / 10, total % 10);
  uint8_t len = strlen(txt);
  if (len > width) len = width;
  memset(out, ' ', width);
  memcpy(out + width - len, txt, len);
  out[width] = '\0';
  oled.drawString(x, y, out);
}

// Model adini 16 sutuna tamamlayarak yazar (eski yazinin kalintisi kalmasin).
void drawName(uint8_t y, const char *name) {
  char out[17];
  memset(out, ' ', 16);
  memcpy(out, name, strlen(name));
  out[16] = '\0';
  oled.drawString(0, y, out);
}

void drawStatic() {
  if (!oledOk) return;
  oled.clearDisplay();
  oled.setInverseFont(1);
  oledText(0, 0, page == PAGE_PC ? F("   BILGISAYAR   ") : F("SENSOR ISTASYONU"));
  oled.setInverseFont(0);

  if (page == PAGE_PC) {
    drawName(1, pcCpuName);
    oledText(0, 2, F("CPU"));
    oledText(15, 2, F("%"));
    oledText(0, 3, F("RAM"));
    oledText(15, 3, F("%"));
    drawName(5, pcGpuName);
    oledText(0, 6, F("GPU"));
    oledText(8, 6, F("%"));
    oledText(15, 6, F("C"));
    oledText(0, 7, F("VRAM"));
  } else {
    oledText(0, 5, F("Nem"));
    oledText(15, 5, F("%"));
    oledText(0, 6, F("Basinc"));
    oledText(13, 6, F("hPa"));
    oledText(0, 7, F("Sensor:"));
  }
}

void drawValues() {
  if (!oledOk) return;
  char buf[17];

  if (page == PAGE_PC) {
    fmtInt(buf, pcCpu);
    oled.drawString(11, 2, buf);
    fmtInt(buf, pcRam);
    oled.drawString(11, 3, buf);
    drawGb(0, 4, 16, pcRamUsed, pcRamTotal);
    fmtInt(buf, pcGpu);
    oled.drawString(4, 6, buf);
    fmtInt(buf, pcGpuTemp);
    oled.drawString(11, 6, buf);
    drawGb(5, 7, 11, pcVramUsed, pcVramTotal);
  } else {
    fmtVal(buf, tempC, 6);
    strcat_P(buf, PSTR(" C"));
    oled.draw2x2String(0, 2, buf);
    fmtVal(buf, humPct, 5);
    oled.drawString(9, 5, buf);
    fmtVal(buf, presHpa, 6);
    oled.drawString(7, 6, buf);
    oledText(8, 7, sensorOk() ? F("OK  ") : F("HATA"));
  }
}

bool probeOled() {
  Wire.beginTransmission(OLED_I2C_ADDR);
  return Wire.endTransmission() == 0;
}

void startOled() {
  oledOk = true;
  oled.setI2CAddress(OLED_I2C_ADDR << 1);
  oled.begin();
  oled.setFont(u8x8_font_chroma48medium8_r);
  drawStatic();
}

void setPage(uint8_t p) {
  page = p;
  pageSinceMs = millis();
  drawStatic();
  drawValues();
  sendPage(F("auto"));
}

// 30 sn bilgisayar, 5 sn sensor. PC verisi kesildiyse sensorde kalir.
void rotatePage(uint32_t now) {
  bool pcLink = pcSeen && (now - lastCmdMs < PC_LINK_TIMEOUT_MS);
  if (page == PAGE_PC) {
    if (!pcLink || now - pageSinceMs >= PC_PAGE_MS) setPage(PAGE_SENSOR);
  } else if (pcLink && now - pageSinceMs >= SENSOR_PAGE_MS) {
    setPage(PAGE_PC);
  }
}

// ---------------- DHT11 ----------------
// Tek hat protokolu, kutuphanesiz. Baslatma 20 ms + okuma ~5 ms surer.
// Kesmeler acik kalir (seri veri kaybolmasin); bozulan okuma saglama
// toplamindan doner ve yok sayilir.
void readDht() {
  uint8_t d[5] = {0, 0, 0, 0, 0};
  bool ok = true;
  pinMode(PIN_DHT, OUTPUT);
  digitalWrite(PIN_DHT, LOW);
  delay(20);
  pinMode(PIN_DHT, INPUT_PULLUP);
  if (pulseIn(PIN_DHT, HIGH, 1000) == 0) ok = false;  // sensorun 80 us'lik yaniti
  for (uint8_t i = 0; ok && i < 40; i++) {
    uint16_t us = pulseIn(PIN_DHT, HIGH, 1000);  // ~27 us = 0, ~70 us = 1
    if (us == 0) ok = false;
    else if (us > 45) d[i / 8] |= 0x80 >> (i % 8);
  }
  if (ok && (uint8_t)(d[0] + d[1] + d[2] + d[3]) == d[4] && d[0] <= 100) {
    dhtHum = constrain(d[0] + DHT_HUM_OFFSET, 0, 100);
    dhtFails = 0;
  } else if (dhtFails < 3 && ++dhtFails == 3) {
    dhtHum = NAN;  // ust uste 3 hata: eski degeri guncelmis gibi gosterme
  }
}

// ---------------- BME280 ----------------
bool initSensor() {
  lastInitMs = millis();
  isBmp = false;
  if (!bme.begin()) {  // cip kimligi 0x60 (BME280) degilse false
    if (bme.sensorID() != 0x58 || !bmp.begin()) return false;
    // BMP280: surekli mod, saniyede bir olcum. Kutuphanenin zorlanmis modu
    // zaman asimsiz bekledigi icin kullanilmadi (kablo koparsa kilitlenirdi).
    bmp.setSampling(Adafruit_BMP280::MODE_NORMAL, Adafruit_BMP280::SAMPLING_X1,
                    Adafruit_BMP280::SAMPLING_X1, Adafruit_BMP280::FILTER_OFF,
                    Adafruit_BMP280::STANDBY_MS_1000);
    isBmp = true;
    return true;
  }
  // Zorlanmis mod: saniyede tek olcum, arada uyku. Kendi kendini isitmaz.
  bme.setSampling(Adafruit_BME280::MODE_FORCED,
                  Adafruit_BME280::SAMPLING_X1, Adafruit_BME280::SAMPLING_X1,
                  Adafruit_BME280::SAMPLING_X1, Adafruit_BME280::FILTER_OFF);
  return true;
}

void setSensorError(SensorErr e) {
  tempC = humPct = presHpa = NAN;
  sensorReady = false;  // RETRY_PERIOD_MS sonra bastan baslatilir
  sensorErr = e;
  sendSensorError();
}

void sampleSensor(uint32_t now) {
  if (!sensorReady) {
    if (now - lastInitMs < RETRY_PERIOD_MS) return;
    if (!initSensor()) {
      setSensorError(ERR_NOT_FOUND);
      return;
    }
    sensorReady = true;
  }
  // MISO kopuksa kutuphane burada en fazla 2 sn bekler; hata sonrasi
  // sensorReady=false oldugu icin bu her saniye tekrarlanmaz.
  float t, h, p;
  if (isBmp) {
    t = bmp.readTemperature() + TEMP_OFFSET_C;
    h = dhtHum;  // BMP280 nem olcmez; DHT11 varsa ondan, yoksa null
    p = bmp.readPressure() / 100.0f;
  } else {
    if (!bme.takeForcedMeasurement()) {
      setSensorError(ERR_READ);
      return;
    }
    t = bme.readTemperature() + TEMP_OFFSET_C;
    h = bme.readHumidity();
    p = bme.readPressure() / 100.0f;
    if (isnan(h) || h < 0 || h > 100) {
      setSensorError(isnan(h) ? ERR_READ : ERR_RANGE);
      return;
    }
  }
  if (isnan(t) || isnan(p)) {
    setSensorError(ERR_READ);
    return;
  }
  // veri sayfasi calisma araligi
  if (t < -40 || t > 85 || p < 300 || p > 1100) {
    setSensorError(ERR_RANGE);
    return;
  }
  tempC = t;
  humPct = h;
  presHpa = p;
  sensorErr = ERR_NONE;
  sampleCount++;
  lastGoodMs = now;
}

// ---------------- PC komutlari ----------------
// rxBuf icinde "anahtar": ardindan gelen 0-9999 arasi tam sayi; yoksa/null ise -1.
int16_t jsonInt(PGM_P key) {
  const char *p = strstr_P(rxBuf, key);
  if (!p) return -1;
  p += strlen_P(key);
  if (*p < '0' || *p > '9') return -1;
  int16_t v = 0;
  for (uint8_t n = 0; n < 4 && *p >= '0' && *p <= '9'; n++) v = v * 10 + (*p++ - '0');
  return v;
}

// "anahtar":"metin" -> dest (en fazla 16 karakter). Seri okuyucu bosluklari
// attigi icin panel bosluk yerine '_' yollar; burada geri cevrilir.
void jsonName(PGM_P key, char *dest) {
  const char *p = strstr_P(rxBuf, key);
  if (!p) return;
  p += strlen_P(key);
  uint8_t n = 0;
  for (; n < 16 && *p && *p != '"'; n++, p++) dest[n] = (*p == '_') ? ' ' : *p;
  dest[n] = '\0';
}

// Kabul edilenler:
//   {"cmd":"get_status"}
//   {"cmd":"pc","cpu":0..100,"ram":0..100,"gpu":0..100,"gt":0..150}   gpu, gt istege bagli
//   {"cmd":"pcmem","ru":N,"rt":N,"vu":N,"vt":N}   RAM ve VRAM kullanilan/toplam, 0,1 GB
//   {"cmd":"pcinfo","cn":"islemci_adi","gn":"ekran_karti_adi"}
// Her satir 63 karakterin altinda tutulur: Uno'nun seri alim tamponu 64 bayttir,
// dongu ekran cizerken gelen daha uzun satirin sonu kaybolurdu.
void handleCommand(uint32_t now) {
  if (rxBuf[0] != '{' || rxBuf[rxLen - 1] != '}') {
    sendError(F("bad_json"));
    return;
  }
  if (strstr_P(rxBuf, PSTR("\"cmd\":\"pcinfo\""))) {
    jsonName(PSTR("\"cn\":\""), pcCpuName);
    jsonName(PSTR("\"gn\":\""), pcGpuName);
    if (page == PAGE_PC && oledOk) {
      drawName(1, pcCpuName);
      drawName(5, pcGpuName);
    }
  } else if (strstr_P(rxBuf, PSTR("\"cmd\":\"pc\""))) {
    int16_t cpu = jsonInt(PSTR("\"cpu\":")), ram = jsonInt(PSTR("\"ram\":"));
    int16_t gpu = jsonInt(PSTR("\"gpu\":")), gt = jsonInt(PSTR("\"gt\":"));
    if (cpu < 0 || cpu > 100 || ram < 0 || ram > 100 || gpu > 100 || gt > 150) {
      sendError(F("bad_pc"));
      return;
    }
    pcCpu = cpu;
    pcRam = ram;
    pcGpu = gpu;
    pcGpuTemp = gt;
    lastCmdMs = now;
    pcSeen = true;
  } else if (strstr_P(rxBuf, PSTR("\"cmd\":\"pcmem\""))) {
    pcRamUsed = jsonInt(PSTR("\"ru\":"));
    pcRamTotal = jsonInt(PSTR("\"rt\":"));
    pcVramUsed = jsonInt(PSTR("\"vu\":"));
    pcVramTotal = jsonInt(PSTR("\"vt\":"));
  } else if (strstr_P(rxBuf, PSTR("\"cmd\":\"get_status\""))) {
    sendStatus(F("status"));
  } else {
    sendError(F("unknown_cmd"));
  }
}

void readSerial(uint32_t now) {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      if (rxOverflow) {
        sendError(F("line_too_long"));
      } else if (rxLen) {
        rxBuf[rxLen] = '\0';
        handleCommand(now);
      }
      rxLen = 0;
      rxOverflow = false;
    } else if (c == ' ' || c == '\r' || c == '\t') {
      // bosluklar atilir, boylece {"cmd": "x"} de eslesir
    } else if (rxLen < sizeof(rxBuf) - 1) {
      rxBuf[rxLen++] = c;
    } else {
      rxOverflow = true;
    }
  }
}

// ---------------- ana akis ----------------
void setup() {
  Serial.begin(115200);

  Wire.begin();
  Wire.setWireTimeout(25000, true);  // gevsek jumper I2C'yi kilitlemesin
  if (probeOled()) startOled();

  sensorReady = initSensor();
  if (sensorReady) sensorErr = ERR_NONE;
  sendStatus(F("hello"));
  if (!sensorReady) sendSensorError();
}

void loop() {
  uint32_t now = millis();
  readSerial(now);
  rotatePage(now);

  if (now - lastSampleMs >= SAMPLE_PERIOD_MS) {
    lastSampleMs = now;
    if ((now / SAMPLE_PERIOD_MS) % 2 == 0) readDht();  // DHT11 en sik 1 sn'de bir okunur
    sampleSensor(now);

    // Ekran sonradan takildiysa ya da kablo oynadiysa yeniden baslat.
    bool present = probeOled();
    if (present && !oledOk) startOled();
    oledOk = present;

    sendTelemetry(now);
    drawValues();
  }
}
