# Arduino Sensör İstasyonu — kurulum ve test

Dosyalar: [sensor_istasyonu.ino](sensor_istasyonu/sensor_istasyonu.ino) · [sensor_panel.py](sensor_panel.py)

Güncel durum (yazılım 1.3.0): OLED kendiliğinden döner, 30 sn bilgisayar (işlemci ve ekran kartı modeli, CPU/RAM/GPU yüzdeleri, RAM ve VRAM GB, GPU sıcaklığı), 5 sn sensör (sıcaklık, nem, basınç). IR kumanda projeden çıkarıldı. Bilgisayar verisi panelden gelir; panel kapalıysa OLED yalnız sensör sayfasında kalır.

## A. Donanım denetimi

### Sensör modülü: BME280 diye satılmış, içindeki çip BMP280
Modül kendini çip kimliği 88 (0x58) ile tanıtıyor; bu BMP280'dir. BMP280 sıcaklık ve basınç ölçer, **nem ölçmez**. Kod iki çipi de kabul eder; BMP280'de nem `--` görünür, DHT11 takılırsa nem ondan gelir.

### Besleme ve lojik seviye (hâlâ açık)
Ürün sayfasına göre modülün çalışma gerilimi **1,8–3,6 V**; üstünde regülatör yok.
- VCC Uno'nun **3,3 V** pininde olmalı. 5 V'taysa hemen 3,3 V'a al; 5 V bu çipin sınırının üstünde.
- D10/D11/D13 pinleri 5 V sinyal verir, çipin pinleri en fazla VCC + 0,3 V kaldırır. Doğrudan bağlı haliyle çalışıyor ama sınırın dışında; çip zamanla bozulabilir ya da ölçümü kayabilir. Doğrusu araya 4 kanallı lojik seviye dönüştürücü (HV 5 V, LV 3,3 V) koymak. Dönüştürücüyle okuma hatası olursa `.ino` içindeki yazılım SPI satırını aç.

### OLED
SSD1306, 128×64, adres 0x3C olarak çalışıyor (donanımda doğrulandı).

### DHT11 (isteğe bağlı, nem için)
3 pinli modül (`S`, `+`, `-`) 3,3–5,5 V çalışır; 5 V uygundur. Çıplak 4 bacaklı sensörse veri hattı ile VCC arasına 10 kΩ direnç gerekir (modülde üstündedir). Doğruluğu düşüktür: nem ±5 puan, tam sayı.

### Diğer
- D13'teki kart LED'i SPI saatiyle yanıp söner, normal.
- Besleme yalnız USB. USB takılıyken 5 V pinine harici kaynak bağlama.
- Kapalı kutuda sensör ortamdan sıcak okur: delik aç, farkı `TEMP_OFFSET_C` ile düzelt.

## B. Bağlantı tablosu

| Modül | Pin | Arduino Uno | Not |
|---|---|---|---|
| OLED | GND | GND | |
| OLED | VCC | 5 V (ya da 3,3 V) | |
| OLED | SCL | A5 | I²C |
| OLED | SDA | A4 | I²C |
| BMP280 | GND | GND | |
| BMP280 | VCC | **3,3 V** | 5 V değil |
| BMP280 | SCL (sarı) | D13 (SCK) | |
| BMP280 | SDA (kırmızı) | D11 (MOSI) | |
| BMP280 | CSB (turuncu) | D10 (CS) | |
| BMP280 | SDO (kahverengi) | D12 (MISO) | |
| DHT11 | `S` / DATA | D2 | |
| DHT11 | `+` | 5 V | |
| DHT11 | `-` | GND | |

## C. Kütüphaneler

Arduino IDE → Araçlar → Kütüphaneleri Yönet üzerinden kur:

| Kütüphane | Sürüm |
|---|---|
| Adafruit BME280 Library | 2.2.4 |
| Adafruit BMP280 Library | 2.6.8 |
| Adafruit BusIO, Adafruit Unified Sensor | 1.16.2, 1.1.14 |
| U8g2 (oliver) | 2.35.30 |

DHT11 için kütüphane gerekmez, okuma kodun içinde. IRremote artık kullanılmıyor. Kart paketi: Arduino AVR Boards 1.8.6.

## F. Python bağımlılıkları

```bash
python -m pip install pyserial psutil
```

GPU bilgisi yalnız NVIDIA'da (`nvidia-smi`) gelir; AMD ve Intel'de panel "GPU bilgisi alınamıyor" yazar, OLED'de `--` görünür.

## G. Çalıştırma

1. `sensor_istasyonu/sensor_istasyonu.ino` dosyasını Arduino IDE'de aç. Kart: Arduino Uno, Port: kartın COM portu (klon Uno'da "USB-SERIAL CH340" diye görünür). Yükle.
2. Arduino IDE'nin Seri Monitörünü kapat (port aynı anda tek programa açılır).
3. Paneli aç, portu seç, Bağlan:

```bash
python sensor_panel.py
```

Paneli açmadan yalnız OLED'i beslemek için penceresiz gönderici (portu kendisi bulur, kopunca yeniden bağlanır):

```bash
pythonw sensor_panel.py --headless
```

Durdurmak için Görev Yöneticisi'nden `pythonw.exe`'yi sonlandır. Panel ve penceresiz gönderici aynı anda çalışamaz; port tek programa açılır.

Bilmen gerekenler:
- Kartı başka bir USB girişine takınca port numarası değişir (örneğin COM3 → COM5). Panelde "Yenile"ye bas; penceresiz gönderici kendisi bulur.
- Port her açıldığında Uno yeniden başlar; panel bağlanınca ekran 1–2 saniye kararır.
- OLED'in bilgisayar sayfası panel açık ve bağlıyken dolar. Panel kapanınca 5 saniye içinde sensör sayfasına döner ve orada kalır.
- Kablo çekilirse panel aynı port geri gelince kendisi bağlanır.
- Süreler `.ino` başındaki `PC_PAGE_MS` (30000) ve `SENSOR_PAGE_MS` (5000) ile değişir.

## I. Test planı

1. **OLED + sensör:** yükle, sensör sayfasında sıcaklık ve basınç görünmeli. Sensöre parmağını değdir, sıcaklık yükselmeli.
2. **DHT11:** tak, en geç 4 saniyede Nem satırı dolmalı. Üfle, nem yükselmeli.
3. **Seri JSON:** Seri Monitör 115200, "Yeni Satır" seçili. `{"cmd":"get_status"}` → `status` gelir. `{"cmd":"xyz"}` → `{"type":"error","code":"unknown_cmd"}`.
4. **Panel:** bağlan. Kartlar ve grafikler dolar; yaklaşık 5 saniye sonra OLED bilgisayar sayfasına geçer, 30 saniye sonra 5 saniyeliğine sensöre döner. Panelde "OLED ekranı" alanı aynı anda değişir.
5. **Kopma:** USB'yi çek, panel çökmez; tak, kendisi bağlanır. Paneli kapat, OLED sensör sayfasında kalır.

## J. Hata ayıklama

| Belirti | Olası neden ve çözüm |
|---|---|
| OLED boş | `hello` içinde `oled_ok:false` ise kablo ya da adres (0x3D). Görüntü kayıksa ekran SH1106'dır, `.ino`'da tek satır |
| `sensor_error`, `chip_id` 255 ya da 0 | Sensör cevap vermiyor: SDO↔D12, CSB↔D10, besleme, gevşek jumper |
| `chip_id` 88 | BMP280, normal; kod kabul eder, nem ölçmez |
| Nem `--` | BMP280 + DHT11 yok ya da DHT11 okunamıyor: `S` D2'de mi, `+`/`-` ters mi |
| Ara sıra `out_of_range` / `read_failed` | Gevşek jumper |
| Sıcaklık 1–3 °C fazla | Kutu ve yan modüllerin ısısı: `TEMP_OFFSET_C` |
| Basınç "yanlış" | Sensör bulunduğun yerin basıncını verir; hava durumu siteleri deniz seviyesine indirger (her 8 m yaklaşık 1 hPa) |
| OLED'de bilgisayar sayfası hiç gelmiyor | Panel açık ve bağlı değil |
| OLED'de model adı boş | Panel adları 10 saniyede bir yollar, bekle |
| Panelde "Port meşgul" | Seri Monitör ya da başka program portu tutuyor |
| Yükleme "avrdude" hatası | Port panelde açık; önce "Bağlantıyı kes" |
| Arduino `line_too_long` / `bad_json` yolluyor | Komut satırı 63 karakteri geçmiş ya da iki satır aralıksız gelmiş (Uno'nun alım tamponu 64 bayt) |

## Protokol

Arduino → PC, satır başına bir JSON:

| `type` | Alanlar | Ne zaman |
|---|---|---|
| `hello` | `fw`, `pages`, `oled_ok`, `sensor_ok`, `sensor_error`, `page`, `uptime_ms` | Açılışta |
| `telemetry` | `temperature_c`, `humidity_pct`, `pressure_hpa` (yoksa `null`), `sensor_ok`, `chip` (`BME280`/`BMP280`), `page`, `uptime_ms`, `samples` | Saniyede bir |
| `sensor_error` | `code` (`not_found` / `read_failed` / `out_of_range`), `chip_id`, `uptime_ms` | Hata anında ve her başarısız yeniden denemede (5 sn) |
| `page` | `page` (0 sensör, 1 bilgisayar), `source` (`auto`) | Sayfa değişince |
| `status` | `hello` ile aynı | `get_status` yanıtı |
| `error` | `code` (`bad_json` / `bad_pc` / `unknown_cmd` / `line_too_long`) | Reddedilen komut |

PC → Arduino (her satır `\n` ile biter, en fazla 63 karakter):

| Komut | Anlamı |
|---|---|
| `{"cmd":"get_status"}` | Durum iste |
| `{"cmd":"pc","cpu":17,"ram":68,"gpu":5,"gt":48}` | Yüzdeler ve GPU sıcaklığı; `gpu`, `gt` isteğe bağlı. Saniyede bir; 5 sn gelmezse OLED sensörde kalır |
| `{"cmd":"pcmem","ru":216,"rt":316,"vu":24,"vt":80}` | RAM ve VRAM kullanılan/toplam, 0,1 GB biriminde |
| `{"cmd":"pcinfo","cn":"i7-13650HX"}` ve `{"cmd":"pcinfo","gn":"RTX_4070_Laptop"}` | Model adları, ayrı satırlarda, en fazla 16 karakter, boşluk yerine `_` |

## Doğrulama durumu

- Taslak Arduino Uno için uyarısız derlenir: 27.990 bayt flash (%86), 936 bayt RAM (%45).
- Gerçek donanımda denendi: klon Uno (CH340) + BMP280 (SPI) + SSD1306 OLED + NVIDIA ekran kartlı Windows 11 bilgisayar. Sıcaklık, basınç, sayfa dönüşü ve bilgisayar sayfası çalışıyor.
- **DHT11 okuma kodu gerçek sensörle denenmedi.**
- Gerçek BME280 (çip kimliği 0x60) ile denenmedi; test edilen modül BMP280 çıktı.
- AMD ve Intel ekran kartlarında GPU bilgisi gelmez.
- Panelin kopma, bozuk satır ve meşgul port davranışı sahte seri portla denendi.
