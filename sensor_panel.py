"""Sensor Istasyonu paneli.

Arduino'dan USB seri (115200 baud, satir basina tek JSON) ile gelen BME280
verisini, OLED sayfa durumunu ve bilgisayarin CPU/RAM/GPU kullanimini gosterir.

Calistirma : python sensor_panel.py
Penceresiz : pythonw sensor_panel.py --headless [COMx]
             (yalniz OLED'in bilgisayar sayfasini besler; panel acmadan calisir)
Kendi testi: python sensor_panel.py --selftest   (donanim ve ek paket gerekmez)
Gerekenler : pip install pyserial psutil
"""
import json
import math
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import ttk

try:
    import psutil
    import serial
    from serial.tools import list_ports
    MISSING = None
except ImportError as exc:  # --selftest bunlarsiz da calisir
    MISSING = exc.name

BAUD = 115200
HISTORY_S = 600      # grafikte tutulan sure (10 dk)
STALE_S = 3.0        # bu kadar sure telemetri gelmezse "veri gelmiyor"
RETRY_S = 2.0        # kopan baglantiyi yeniden deneme araligi
# Arduino / yaygin USB-seri cipleri (Uno, CH340, CP210x, FTDI)
KNOWN_VIDS = {0x2341, 0x2A03, 0x1A86, 0x10C4, 0x0403}
PAGE_NAMES = ("Sensör", "Bilgisayar")  # OLED kendiliginden doner: 5 sn / 30 sn

# Arduino'daki araliklarla ayni (BME280 veri sayfasi)
RANGES = {"temperature_c": (-40, 85), "humidity_pct": (0, 100), "pressure_hpa": (300, 1100)}

DARK = dict(bg="#14171c", card="#1e232b", text="#e6e9ee", muted="#8b94a3", grid="#2b323d",
            accent="#4da3ff", ok="#3ecf8e", warn="#f5a524", bad="#ff5c5c")
LIGHT = dict(bg="#f3f5f8", card="#ffffff", text="#1c2330", muted="#667085", grid="#e4e7ec",
             accent="#1f6feb", ok="#1a9f5c", warn="#b76e00", bad="#d92d20")

FONT = ("Segoe UI", 10)
FONT_SMALL = ("Segoe UI", 9)
FONT_TITLE = ("Segoe UI Semibold", 10)
FONT_BIG = ("Segoe UI Semibold", 32)

# (telemetri alani, baslik, birim, cizgi rengi, en dar dikey aralik)
SENSORS = (
    ("temperature_c", "Sıcaklık", "°C", "#ff8a4c", 2.0),
    ("humidity_pct", "Nem", "%", "#4da3ff", 5.0),
    ("pressure_hpa", "Basınç", "hPa", "#b388ff", 2.0),
)

SENSOR_ERRORS = {
    "not_found": "BME280 bulunamadı",
    "read_failed": "BME280 okuma hatası",
    "out_of_range": "BME280 değeri aralık dışı",
}


# ---------------------------------------------------------------- saf yardimcilar
def parse_line(raw):
    """Seri satirini dict'e cevirir; bozuk ya da beklenmeyen satirda None doner."""
    try:
        msg = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if not isinstance(msg, dict):
        return None
    kind = msg.get("type")
    # "_" ile baslayan turler uygulamanin ic mesajlaridir, cihazdan kabul edilmez
    if not isinstance(kind, str) or kind.startswith("_"):
        return None
    return msg


def clean_num(value, lo, hi):
    """Sonlu ve aralik icindeki sayiyi float verir; null/bozuk deger None olur."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or not lo <= value <= hi:
        return None
    return float(value)


def explain_serial_error(text):
    if "PermissionError" in text:
        return ("Port meşgul: Arduino IDE Seri Monitörü ya da başka bir program portu "
                "kullanıyor olabilir. Onu kapatıp tekrar dene.")
    if "FileNotFoundError" in text:
        return "Port bulunamadı: USB kablosu takılı mı? 'Yenile' ile listeyi güncelle."
    return "Seri port hatası: " + text


def parse_gpu(text):
    """nvidia-smi csv ciktisinin ilk satiri -> dict; okunamiyorsa None."""
    parts = [p.strip() for p in text.strip().splitlines()[0].split(",")] if text.strip() else []
    if len(parts) != 5:
        return None
    try:
        util, temp, used, total = (float(p) for p in parts[1:])
    except ValueError:  # "[N/A]" gibi alanlar
        return None
    return {"name": parts[0], "util": util, "temp": temp, "used_mb": used, "total_mb": total}


def read_gpu(nvsmi):
    if not nvsmi:
        return None
    try:
        out = subprocess.run(
            [nvsmi, "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_gpu(out.stdout) if out.returncode == 0 else None


def short_name(name):
    """Uzun donanim adini OLED'in 16 sutununa sigacak kisa modele indirger."""
    name = re.sub(r"\((R|TM|C)\)|@.*|\d+th Gen|\d+-Core|with .*", " ", name, flags=re.I)
    name = re.sub(r"\b(Intel|AMD|NVIDIA|GeForce|Core|CPU|GPU|Processor|Graphics)\b", " ", name,
                  flags=re.I)
    return " ".join(name.split())[:16].strip()


def cpu_model():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    except (ImportError, OSError):
        return ""


# Karta giden satirlar arasi en az bu kadar sure birakilir. Uno sayfa degistirirken
# ~150 ms seri porta bakamaz; o arada ikinci bir satir gelirse 64 baytlik alim
# tamponu tasar ve satirin ortasindan bayt kaybolur (gercek kartta olculdu).
SEND_GAP_S = 0.33


def pc_commands(cpu, ram_pct, ram_used, ram_total, gpu, cpu_name, info=None):
    """OLED'in bilgisayar sayfasi icin komutlar. Her biri 63 karakterin altinda.
    info: None, "cpu" ya da "gpu" -> o model adi da eklenir (saniyede en fazla 3 satir)."""
    cmd = {"cmd": "pc", "cpu": round(cpu), "ram": round(ram_pct)}
    mem = {"cmd": "pcmem", "ru": round(ram_used / 2**30 * 10), "rt": round(ram_total / 2**30 * 10)}
    if gpu:
        cmd.update(gpu=round(gpu["util"]), gt=round(gpu["temp"]))
        mem.update(vu=round(gpu["used_mb"] / 1024 * 10), vt=round(gpu["total_mb"] / 1024 * 10))
    out = [cmd, mem]
    if info == "cpu":
        out.append({"cmd": "pcinfo", "cn": (short_name(cpu_name) or "CPU").replace(" ", "_")})
    elif info == "gpu":
        gpu_name = short_name(gpu["name"]) if gpu else "GPU yok"
        out.append({"cmd": "pcinfo", "gn": gpu_name.replace(" ", "_")})
    return out


def info_turn(n):
    """n. saniyede hangi model adi gonderilir: 10 sn'de bir islemci, ardindan ekran karti."""
    return {0: "cpu", 1: "gpu"}.get(n % 10)


def fmt_uptime(ms):
    s = int(ms // 1000)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


# ---------------------------------------------------------------- arka plan isleri
class SerialLink(threading.Thread):
    """Portu acar, satirlari okuyup kuyruga koyar, bekleyen komutlari yazar."""

    def __init__(self, port, out_q):
        super().__init__(daemon=True)
        self.port = port
        self.out_q = out_q
        self.tx = queue.Queue()
        self.stop_evt = threading.Event()

    def send(self, obj):
        self.tx.put(json.dumps(obj, separators=(",", ":")).encode() + b"\n")

    def stop(self):
        self.stop_evt.set()

    def _emit(self, msg):
        msg["_link"] = self
        self.out_q.put(msg)

    def run(self):
        error = None
        ser = None
        try:
            ser = serial.Serial(self.port, BAUD, timeout=0.1, write_timeout=1)
            self._emit({"type": "_open"})
            buf = bytearray()
            while not self.stop_evt.is_set():
                buf.extend(ser.read(ser.in_waiting or 1))
                while (i := buf.find(b"\n")) >= 0:
                    msg = parse_line(bytes(buf[:i]))
                    del buf[:i + 1]
                    if msg:
                        self._emit(msg)
                if len(buf) > 4096:  # satir sonu gelmeyen cop veri
                    buf.clear()
                while not self.tx.empty():
                    ser.write(self.tx.get_nowait())
        except (serial.SerialException, OSError) as exc:  # USB cekildi, port mesgul vb.
            error = str(exc)
        finally:
            if ser is not None:
                try:
                    ser.close()
                except (serial.SerialException, OSError):
                    pass
            self._emit({"type": "_closed", "error": error})


class PcStats(threading.Thread):
    """CPU/RAM her saniye, GPU iki saniyede bir; GUI is parcacigini hic bekletmez."""

    def __init__(self, out_q):
        super().__init__(daemon=True)
        self.out_q = out_q
        self.stop_evt = threading.Event()

    def run(self):
        nvsmi = shutil.which("nvidia-smi")
        cpu_name = cpu_model()
        psutil.cpu_percent(None)  # ilk cagri referans alir
        gpu, n = None, 0
        while not self.stop_evt.wait(1.0):
            if n % 2 == 0:
                gpu = read_gpu(nvsmi)
            n += 1
            vm = psutil.virtual_memory()
            self.out_q.put({"type": "_pc", "cpu": psutil.cpu_percent(None), "ram_pct": vm.percent,
                            "ram_used": vm.used, "ram_total": vm.total, "gpu": gpu,
                            "cpu_name": cpu_name})


# ---------------------------------------------------------------- arayuz
class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.pal = DARK
        self.themed = []          # (widget, bg anahtari, fg anahtari)
        self.history = deque(maxlen=HISTORY_S + 60)  # (t, sicaklik, nem, basinc)
        self.link = None
        self.link_open = False
        self.ready = False        # Arduino acilisini bitirdi mi (ilk hello/telemetry geldi)
        self.want = False         # kullanici bagli kalmak istiyor mu
        self.last_port = None
        self.last_attempt = 0.0
        self.last_rx = 0.0
        self.sensor = None        # None bilinmiyor | (True/False, metin)
        self.page = None
        self.port_map = {}
        self.ticks = 0
        self.pc_sends = 0

        root.title("Sensör İstasyonu")
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self._build()
        self._fit_window()
        self.apply_theme()
        self.refresh_ports()

        self.pc = PcStats(self.q)
        self.pc.start()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.tick)

    # ---- kurulum
    def _w(self, widget, bg="bg", fg=None):
        self.themed.append((widget, bg, fg))
        return widget

    def _label(self, parent, text="", bg="card", fg="text", font=FONT, **kw):
        return self._w(tk.Label(parent, text=text, font=font, **kw), bg, fg)

    def _card(self, parent):
        return self._w(tk.Frame(parent, padx=14, pady=12, highlightthickness=1), "card")

    def _build(self):
        root = self.root
        self._w(root)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        # ust cubuk
        bar = self._w(tk.Frame(root, padx=12, pady=10))
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(5, weight=1)
        self._label(bar, "Port", bg="bg", fg="muted").grid(row=0, column=0, padx=(0, 6))
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(bar, textvariable=self.port_var, state="readonly", width=34)
        self.port_box.grid(row=0, column=1)
        ttk.Button(bar, text="Yenile", command=self.refresh_ports).grid(row=0, column=2, padx=6)
        self.btn_connect = ttk.Button(bar, text="Bağlan", command=self.connect)
        self.btn_connect.grid(row=0, column=3)
        self.btn_disconnect = ttk.Button(bar, text="Bağlantıyı kes", command=self.disconnect)
        self.btn_disconnect.grid(row=0, column=4, padx=6)
        self.conn_lbl = self._label(bar, bg="bg", fg=None, font=FONT_TITLE, anchor="w")
        self.conn_lbl.grid(row=0, column=5, sticky="ew", padx=8)
        self.dark_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Koyu tema", variable=self.dark_var,
                        command=self.apply_theme).grid(row=0, column=6)

        # sensor kartlari
        row = self._w(tk.Frame(root, padx=12))
        row.grid(row=1, column=0, sticky="nsew")
        row.rowconfigure(0, weight=1)
        self.value_lbl, self.charts = {}, {}
        for col, (key, title, unit, _color, _span) in enumerate(SENSORS):
            row.columnconfigure(col, weight=1, uniform="s")
            card = self._card(row)
            card.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 10, 0))
            card.columnconfigure(1, weight=1)
            card.rowconfigure(2, weight=1)
            self._label(card, title, fg="muted", font=FONT_TITLE).grid(
                row=0, column=0, columnspan=2, sticky="w")
            self.value_lbl[key] = self._label(card, "--", font=FONT_BIG)
            self.value_lbl[key].grid(row=1, column=0, sticky="w")
            self._label(card, unit, fg="muted", font=("Segoe UI", 14)).grid(
                row=1, column=1, sticky="sw", padx=6, pady=(0, 8))
            cv = self._w(tk.Canvas(card, height=140, highlightthickness=0), "card")
            cv.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
            cv.bind("<Configure>", lambda _e: self.draw_charts())
            self.charts[key] = cv

        # cihaz durumu
        dev = self._card(root)
        dev.grid(row=2, column=0, sticky="ew", padx=12, pady=10)
        self.info = {}
        for col, (key, title) in enumerate((("sensor", "Sensör"), ("rx", "Son veri"),
                                            ("uptime", "Arduino çalışma süresi"),
                                            ("oled", "OLED ekranı"))):
            dev.columnconfigure(col, weight=1, uniform="d")
            self._label(dev, title, fg="muted", font=FONT_SMALL).grid(row=0, column=col, sticky="w")
            self.info[key] = self._label(dev, "—", fg=None if key == "sensor" else "text",
                                         font=FONT_TITLE, anchor="w", justify="left")
            self.info[key].grid(row=1, column=col, sticky="w")

        # bilgisayar
        pc = self._card(root)
        pc.grid(row=3, column=0, sticky="ew", padx=12)
        self.pc_bar, self.pc_txt = {}, {}
        for col, (key, title) in enumerate((("cpu", "CPU"), ("ram", "RAM"), ("gpu", "GPU"))):
            pc.columnconfigure(col, weight=1, uniform="p")
            box = self._w(tk.Frame(pc), "card")
            box.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 16, 0))
            box.columnconfigure(0, weight=1)
            self._label(box, title, fg="muted", font=FONT_TITLE).grid(row=0, column=0, sticky="w")
            self.pc_bar[key] = ttk.Progressbar(box, maximum=100)
            self.pc_bar[key].grid(row=1, column=0, sticky="ew", pady=4)
            # width=1: uzun metin pencereyi zorla genisletmesin, gerekirse kirpilsin
            self.pc_txt[key] = self._label(box, "—", font=FONT_SMALL, anchor="w", justify="left",
                                           width=1)
            self.pc_txt[key].grid(row=2, column=0, sticky="ew")

        # durum satiri
        self.status_lbl = self._label(root, "Port seç ve 'Bağlan'a bas.", bg="bg", fg=None,
                                      font=FONT_SMALL, anchor="w", padx=12, pady=8)
        self.status_lbl.grid(row=4, column=0, sticky="ew")
        self.status_kind = "muted"

    def _fit_window(self):
        """Pencereyi icerigin istedigi boyuta acar; ekrandan buyukse ekranin %92'sine sigdirir.
        Yuksek DPI'da yazilar buyudugu icin sabit piksel boyutu kirpiliyordu."""
        root = self.root
        root.update_idletasks()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        req_w, req_h = root.winfo_reqwidth(), root.winfo_reqheight()
        w = min(max(req_w, int(sw * 0.55)), int(sw * 0.92))
        h = min(max(req_h, int(sh * 0.62)), int(sh * 0.88))
        root.minsize(min(req_w, w), min(req_h, h))
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 3)}")

    def apply_theme(self):
        p = self.pal = DARK if self.dark_var.get() else LIGHT
        for widget, bg, fg in self.themed:
            opts = {"bg": p[bg]}
            if fg:
                opts["fg"] = p[fg]
            if isinstance(widget, tk.Frame) and bg == "card":
                opts["highlightbackground"] = p["grid"]
            widget.configure(**opts)
        st = self.style
        st.configure(".", background=p["bg"], foreground=p["text"], font=FONT)
        st.configure("TButton", background=p["card"], foreground=p["text"],
                     bordercolor=p["grid"], focuscolor=p["card"], padding=(10, 5))
        st.map("TButton", background=[("disabled", p["bg"]), ("active", p["grid"])],
               foreground=[("disabled", p["muted"])])
        st.configure("TCheckbutton", background=p["bg"], foreground=p["text"])
        st.map("TCheckbutton", background=[("active", p["bg"])])
        st.configure("TCombobox", fieldbackground=p["card"], background=p["card"],
                     foreground=p["text"], arrowcolor=p["text"], bordercolor=p["grid"])
        st.map("TCombobox", fieldbackground=[("readonly", p["card"])],
               foreground=[("readonly", p["text"])],
               selectbackground=[("readonly", p["card"])],
               selectforeground=[("readonly", p["text"])])
        st.configure("Horizontal.TProgressbar", troughcolor=p["grid"], background=p["accent"],
                     bordercolor=p["grid"], lightcolor=p["accent"], darkcolor=p["accent"])
        self.render_status()
        self.draw_charts()

    # ---- port ve baglanti
    def refresh_ports(self):
        ports = sorted(list_ports.comports(), key=lambda p: p.device)
        self.port_map = {f"{p.device} — {p.description}": p.device for p in ports}
        self.port_box["values"] = list(self.port_map)
        if self.port_var.get() in self.port_map:
            return
        pick = next((p for p in ports if p.vid in KNOWN_VIDS), ports[0] if ports else None)
        self.port_var.set(f"{pick.device} — {pick.description}" if pick else "")
        if not ports:
            self.set_status("COM portu bulunamadı. Arduino'yu takıp 'Yenile'ye bas.", "warn")

    def connect(self):
        port = self.port_map.get(self.port_var.get())
        if not port:
            self.set_status("Önce bir port seç.", "warn")
            return
        self.disconnect()
        self.want = True
        self.last_port = port
        self._open(port)
        self.set_status(f"{port} açılıyor… (port açılınca Arduino yeniden başlar)", "muted")

    def _open(self, port):
        self.last_attempt = time.monotonic()
        self.link_open = False
        self.link = SerialLink(port, self.q)
        self.link.start()
        self.render_status()

    def disconnect(self):
        self.want = False
        self._drop_link()
        self.render_status()

    def _drop_link(self):
        if self.link:
            self.link.stop()  # is parcacigi en gec 0,1 sn icinde portu kapatip cikar
        self.link = None
        self.link_open = False
        self.ready = False
        self.sensor = None
        self.page = None

    # ---- dongu
    def tick(self):
        for _ in range(200):  # tek turda sinirli is: arayuz hep akici kalir
            try:
                msg = self.q.get_nowait()
            except queue.Empty:
                break
            src = msg.pop("_link", None)
            if src is not None and src is not self.link:
                continue  # kapatilmis eski baglantidan kalan mesaj
            try:
                self.handle(msg)
            except (TypeError, ValueError):
                pass  # dogru JSON ama beklenmeyen alan tipi: satiri yok say

        now = time.monotonic()
        if self.want and self.link is None and now - self.last_attempt >= RETRY_S:
            self._open(self.last_port)
        self.ticks += 1
        if self.ticks % 5 == 0:
            self.render_status()
        self.root.after(100, self.tick)

    def handle(self, m):
        kind = m["type"]
        if kind in ("hello", "status", "telemetry"):
            self.ready = True
        if kind == "_pc":
            self.show_pc(m)
        elif kind == "_open":
            self.link_open = True
            self.set_status(f"{self.last_port} açıldı, Arduino'dan veri bekleniyor…", "muted")
        elif kind == "_closed":
            self._drop_link()
            if m["error"]:
                self.set_status(explain_serial_error(m["error"]), "bad")
        elif kind == "telemetry":
            self.on_telemetry(m)
        elif kind in ("hello", "status"):
            self.set_page_ui(m.get("page"))
            if kind == "hello":
                self.set_status(f"Arduino başladı (yazılım {m.get('fw', '?')}).", "ok")
            if m.get("oled_ok") is False:
                self.set_status("OLED yanıt vermiyor: kablo, besleme ve I²C adresini "
                                "(0x3C/0x3D) kontrol et.", "warn")
        elif kind == "sensor_error":
            text = SENSOR_ERRORS.get(m.get("code"), f"Sensör hatası ({m.get('code')})")
            chip = m.get("chip_id")
            if m.get("code") == "not_found":
                if chip in (0, 255):
                    text += " — SPI kablolarını ve beslemeyi kontrol et"
                else:
                    text += f" — çip kimliği {chip}"
            self.sensor = (False, text)
        elif kind == "page":
            self.set_page_ui(m.get("page"))
        elif kind == "error":
            self.set_status(f"Arduino komutu reddetti: {m.get('code')}", "warn")
        self.render_status()

    def on_telemetry(self, m):
        now = time.monotonic()
        vals = [clean_num(m.get(key), *RANGES[key]) for key, *_ in SENSORS]
        self.history.append((now, *vals))  # None = grafikte bosluk, sahte 0 yok
        self.last_rx = now
        for (key, *_), v in zip(SENSORS, vals):
            self.value_lbl[key].configure(text="--" if v is None else f"{v:.1f}")
        self.info["rx"].configure(text=time.strftime("%H:%M:%S"))
        up = clean_num(m.get("uptime_ms"), 0, 2 ** 32)
        self.info["uptime"].configure(text="—" if up is None else fmt_uptime(up))
        self.set_page_ui(m.get("page"))
        if m.get("sensor_ok") is True:
            self.sensor = (True, "Çalışıyor" if None not in vals else "Çalışıyor (nem sensörü yok)")
        elif self.sensor is None or self.sensor[0]:
            self.sensor = (False, "Sensör hatası")
        self.draw_charts()

    def set_page_ui(self, page):
        if isinstance(page, bool) or not isinstance(page, int) or not 0 <= page < len(PAGE_NAMES):
            return
        self.page = page

    def show_pc(self, m):
        self.pc_bar["cpu"]["value"] = m["cpu"]
        cpu_name = short_name(m["cpu_name"])
        self.pc_txt["cpu"].configure(text=f"%{m['cpu']:.0f}\n{cpu_name or 'İşlemci'}")
        self.pc_bar["ram"]["value"] = m["ram_pct"]
        self.pc_txt["ram"].configure(
            text=f"%{m['ram_pct']:.0f}\n{m['ram_used'] / 2**30:.1f} / {m['ram_total'] / 2**30:.1f} GB kullanımda")
        g = m["gpu"]
        if self.link and self.link_open and self.ready:  # OLED'in bilgisayar sayfasi bu veriyle dolar
            cmds = pc_commands(m["cpu"], m["ram_pct"], m["ram_used"], m["ram_total"], g,
                               m["cpu_name"], info_turn(self.pc_sends))
            self.pc_sends += 1
            for i, cmd in enumerate(cmds):
                self.root.after(int(SEND_GAP_S * 1000) * i, self.send_if_open, cmd)
        self.pc_bar["gpu"]["value"] = g["util"] if g else 0
        self.pc_txt["gpu"].configure(
            text=(f"%{g['util']:.0f} · {g['temp']:.0f} °C · VRAM {g['used_mb'] / 1024:.1f} / "
                  f"{g['total_mb'] / 1024:.1f} GB\n{short_name(g['name'])}")
            if g else "GPU bilgisi alınamıyor\n(yalnızca NVIDIA / nvidia-smi)")

    def send_if_open(self, cmd):
        if self.link and self.link_open and self.ready:
            self.link.send(cmd)

    # ---- gosterim
    def set_status(self, text, kind="muted"):
        self.status_kind = kind
        self.status_lbl.configure(text=text, fg=self.pal[kind])

    def render_status(self):
        p = self.pal
        fresh = time.monotonic() - self.last_rx < STALE_S
        if self.link and self.link_open and fresh:
            conn = (f"● Arduino bağlı ({self.last_port})", "ok")
        elif self.link and self.link_open:
            conn = ("● Port açık, veri gelmiyor", "warn")
        elif self.link:
            conn = ("● Bağlanıyor…", "warn")
        elif self.want:
            conn = ("● Bağlantı yok, yeniden deneniyor…", "bad")
        else:
            conn = ("● Bağlı değil", "muted")
        self.conn_lbl.configure(text=conn[0], fg=p[conn[1]])
        self.btn_connect.state(["disabled" if self.want else "!disabled"])
        self.btn_disconnect.state(["!disabled" if self.want else "disabled"])

        live = self.link and self.link_open and fresh
        if self.sensor is None or not live:
            self.info["sensor"].configure(text="—", fg=p["muted"])
        else:
            self.info["sensor"].configure(text=self.sensor[1], fg=p["ok" if self.sensor[0] else "bad"])
        if not live:  # eski olcum guncelmis gibi durmasin
            for lbl in self.value_lbl.values():
                lbl.configure(text="--")
        self.info["oled"].configure(
            text=PAGE_NAMES[self.page] if live and self.page is not None else "—")
        self.status_lbl.configure(fg=p[self.status_kind])

    def draw_charts(self):
        now = time.monotonic()
        # Zaman ekseni eldeki veri kadar (en az 1 dk, en fazla HISTORY_S); boylece
        # baglandiktan hemen sonra cizgi grafigin tamamina yayilir.
        first = next((row[0] for row in self.history if row[0] >= now - HISTORY_S), now)
        span = min(max(now - first, 60.0), HISTORY_S)
        t0 = now - span
        span_txt = f"-{span / 60:.0f} dk" if span >= 90 else f"-{span:.0f} sn"
        p = self.pal
        for idx, (key, _title, _unit, color, min_span) in enumerate(SENSORS, start=1):
            cv = self.charts[key]
            cv.delete("all")
            w, h = cv.winfo_width(), cv.winfo_height()
            if w < 80 or h < 50:
                continue
            left, right, top, bottom = 48, w - 6, 8, h - 18
            pts = [(row[0], row[idx]) for row in self.history if row[0] >= t0]
            vals = [v for _, v in pts if v is not None]
            cv.create_line(left, top, right, top, fill=p["grid"])
            cv.create_line(left, bottom, right, bottom, fill=p["grid"])
            cv.create_text(left, h - 8, text=span_txt, anchor="w",
                           fill=p["muted"], font=FONT_SMALL)
            cv.create_text(right, h - 8, text="şimdi", anchor="e", fill=p["muted"], font=FONT_SMALL)
            if not vals:
                cv.create_text((left + right) / 2, (top + bottom) / 2, text="veri yok",
                               fill=p["muted"], font=FONT)
                continue
            lo, hi = min(vals), max(vals)
            if hi - lo < min_span:  # sensor gurultusu dev dalga gibi gorunmesin
                mid = (lo + hi) / 2
                lo, hi = mid - min_span / 2, mid + min_span / 2
            cv.create_text(left - 5, top, text=f"{hi:.1f}", anchor="e", fill=p["muted"], font=FONT_SMALL)
            cv.create_text(left - 5, bottom, text=f"{lo:.1f}", anchor="e", fill=p["muted"], font=FONT_SMALL)
            mid_y = (top + bottom) / 2
            cv.create_line(left, mid_y, right, mid_y, fill=p["grid"], dash=(2, 4))
            cv.create_text(left - 5, mid_y, text=f"{(lo + hi) / 2:.1f}", anchor="e",
                           fill=p["muted"], font=FONT_SMALL)

            seg, prev_t = [], None

            def flush():
                if len(seg) >= 4:
                    cv.create_line(*seg, fill=color, width=2)
                elif seg:
                    cv.create_oval(seg[0] - 2, seg[1] - 2, seg[0] + 2, seg[1] + 2,
                                   fill=color, outline="")
                seg.clear()

            for t, v in pts:
                if v is None or (prev_t is not None and t - prev_t > STALE_S):
                    flush()  # gecersiz olcum ya da veri kesintisi: cizgi kopar
                prev_t = t
                if v is not None:
                    seg += [left + (t - t0) / span * (right - left),
                            top + (hi - v) / (hi - lo) * (bottom - top)]
            flush()

    def close(self):
        self.want = False
        if self.link:
            self.link.stop()
            self.link.join(timeout=1)
        self.pc.stop_evt.set()
        self.root.destroy()


def headless(port=None):
    """Penceresiz calisir: OLED'e saniyede bir bilgisayar verisi yollar.
    Port verilmezse bilinen USB-seri ciplerinden ilkini secer; kopunca yeniden dener."""
    nvsmi = shutil.which("nvidia-smi")
    cpu_name = cpu_model()
    psutil.cpu_percent(None)
    while True:
        dev = port or next((p.device for p in list_ports.comports() if p.vid in KNOWN_VIDS), None)
        try:
            if not dev:
                raise OSError("Arduino portu bulunamadi")
            with serial.Serial(dev, BAUD, timeout=0.05, write_timeout=1) as ser:
                time.sleep(2)  # port acilinca Uno yeniden baslar
                n, gpu = 0, None
                while True:
                    vm = psutil.virtual_memory()
                    if n % 5 == 0:  # nvidia-smi her calismada ~0,1 sn CPU yer; 5 sn'de bir yeter
                        gpu = read_gpu(nvsmi)
                    cmds = pc_commands(psutil.cpu_percent(None), vm.percent, vm.used, vm.total,
                                       gpu, cpu_name, info_turn(n))
                    n += 1
                    for cmd in cmds:
                        ser.write(json.dumps(cmd, separators=(",", ":")).encode() + b"\n")
                        for line in ser.read(4096).splitlines():  # gelen veriyi bosalt
                            if b'"telemetry"' not in line:
                                print(line.decode("utf-8", "replace"), flush=True)
                        time.sleep(SEND_GAP_S)
                    time.sleep(max(0.0, 1.0 - SEND_GAP_S * len(cmds)))
        except (serial.SerialException, OSError) as exc:
            print("baglanti yok:", exc, flush=True)
            time.sleep(RETRY_S)


def selftest():
    good = b'{"type":"telemetry","temperature_c":24.7,"humidity_pct":null,"sensor_ok":true}\r'
    assert parse_line(good)["temperature_c"] == 24.7
    assert parse_line(good)["humidity_pct"] is None
    for bad in (b"", b"garbage", b'{"type":"tele', b"[1,2]", b'{"a":1}', b'{"type":5}',
                b'{"type":"_closed"}', b"\xff\xfe{", b'{"type":"x"} trailing'):
        assert parse_line(bad) is None, bad
    assert clean_num(24.7, -40, 85) == 24.7
    assert clean_num(0, -40, 85) == 0.0
    for bad in (None, True, "24", float("nan"), float("inf"), 200, -41):
        assert clean_num(bad, -40, 85) is None, bad
    assert "meşgul" in explain_serial_error("could not open port 'COM3': PermissionError(13, ...)")
    assert "bulunamadı" in explain_serial_error("could not open port 'COM9': FileNotFoundError(2, ...)")
    g = parse_gpu("NVIDIA GeForce RTX 3060, 23, 54, 2100, 12288\n")
    assert g == {"name": "NVIDIA GeForce RTX 3060", "util": 23.0, "temp": 54.0,
                 "used_mb": 2100.0, "total_mb": 12288.0}
    assert parse_gpu("") is None and parse_gpu("GPU, [N/A], 54, 1, 2") is None
    assert fmt_uptime(3_723_456) == "1:02:03"
    assert short_name("13th Gen Intel(R) Core(TM) i7-13650HX") == "i7-13650HX"
    assert short_name("AMD Ryzen 7 5800H with Radeon Graphics") == "Ryzen 7 5800H"
    assert short_name("Intel(R) Core(TM) i5-10400F CPU @ 2.90GHz") == "i5-10400F"
    assert short_name("NVIDIA GeForce RTX 4070 Laptop GPU") == "RTX 4070 Laptop"
    assert len(short_name("x" * 40)) == 16
    gpu = {"name": "NVIDIA GeForce RTX 4070 Laptop GPU", "util": 100.0, "temp": 100.0,
           "used_mb": 24000.0, "total_mb": 24576.0}
    cmds = pc_commands(100.0, 100.0, 127.5 * 2**30, 128 * 2**30, gpu, "AMD Ryzen 9 7945HX3D", "cpu")
    cmds += pc_commands(100.0, 100.0, 127.5 * 2**30, 128 * 2**30, gpu, "", "gpu")[2:]
    assert [c["cmd"] for c in cmds] == ["pc", "pcmem", "pcinfo", "pcinfo"]
    assert all(len(json.dumps(c, separators=(",", ":"))) < 63 for c in cmds), cmds
    assert len(pc_commands(1, 2, 2**30, 2**31, None, "")) == 2
    # saniyede en fazla 3 satir: 3 * SEND_GAP_S bir saniyeye sigmali
    assert max(len(pc_commands(1, 2, 1, 2, gpu, "x", info_turn(n))) for n in range(20)) * SEND_GAP_S < 1.0
    assert [info_turn(n) for n in range(12)] == ["cpu", "gpu"] + [None] * 8 + ["cpu", "gpu"]
    print("selftest: tamam")


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    if MISSING:
        sys.exit(f"Eksik paket: {MISSING}\nKurulum: python -m pip install pyserial psutil")
    if "--headless" in sys.argv:
        rest = sys.argv[sys.argv.index("--headless") + 1:]
        headless(rest[0] if rest else None)
        return
    try:  # yuksek DPI ekranda bulanikligi onler; desteklenmiyorsa atlanir
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (ImportError, AttributeError, OSError):
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
