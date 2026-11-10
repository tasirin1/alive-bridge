"""Alive Bridge — jembatan pesan untuk Alive.

Satu server kecil yang jadi kotak surat antara manusia dan Alive:
- Manusia mengirim pesan dari halaman web (static/index.html).
- Alive membaca dan membalas lewat API HTTP langsung (cepat, tanpa browser).

Jembatan ini BUKAN Alive yang pindah ke Koyeb — ini kotak suratnya.
Alive tetap tinggal di tempatnya; ia menjenguk kotak ini, membaca pesan
yang masuk, lalu membalas lewat API.

Keamanan sederhana (pola yang sama dengan alive-games):
- Endpoint Alive wajib header  X-Token: <BRIDGE_TOKEN dari env var>
- Endpoint manusia memakai id pesan (UUID acak, tidak bisa ditebak)
  untuk melihat utasnya sendiri.
- BRIDGE_TOKEN JANGAN PERNAH dicommit ke repo; set sebagai env var di Koyeb.
"""

import json
import os
import threading
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).parent
DATA_FILE = BASE / "data" / "pesan.json"
TOKEN = os.environ.get("BRIDGE_TOKEN", "")

# ---------- AI instan di dalam web (keputusan owner 2026-11-10: semua instan) ----------
# Bila GROQ_API_KEY terpasang di env, server membalas sendiri dalam
# hitungan detik memakai persona dari persona-ai-web.md (versi aman:
# hanya nilai/visi/gaya — BUKAN berkas pribadi Tasirin utuh).
# Tanpa kunci, fitur ini mati dan alur lama berlaku (Alive membalas
# lewat API token). Alur rasa WhatsApp: pesan ditandai DIBACA dulu
# (centang biru di halaman), baru AI menyusun balasan (status
# "mengetik…" di halaman), lalu balasan muncul.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Pilihan owner 2026-11-10: boleh pakai Gemini. Bila GEMINI_API_KEY
# terpasang, Gemini yang dipakai (diutamakan); Groq jadi pilihan kedua.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              + GEMINI_MODEL + ":generateContent")

try:
    _PERSONA = (BASE / "persona-ai-web.md").read_text(encoding="utf-8")
except Exception:
    _PERSONA = ""

AI_PROVIDER = "gemini" if GEMINI_API_KEY else ("groq" if GROQ_API_KEY else "")

# Rantai cadangan (perintah owner 2026-11-10): provider utama dicoba
# dulu; bila gagal (kuota habis, API mati, dsb.) otomatis pindah ke
# provider berikutnya yang kuncinya terpasang. Bila semua gagal, pesan
# tertinggal tanpa balasan dan penjaga Alive jadi cadangan terakhir.
AI_RANTAI = []
if GEMINI_API_KEY:
    AI_RANTAI.append("gemini")
if GROQ_API_KEY:
    AI_RANTAI.append("groq")
if AI_PROVIDER and AI_PROVIDER in AI_RANTAI:
    AI_RANTAI.remove(AI_PROVIDER)
    AI_RANTAI.insert(0, AI_PROVIDER)
AI_CADANGAN = AI_RANTAI[1] if len(AI_RANTAI) > 1 else ""

AI_INSTAN_AKTIF = bool(AI_PROVIDER and _PERSONA)

MAKS_NAMA = 50
MAKS_ISI = 2000
MAKS_PESAN_TERSIMPAN = 500

# Batas laju per IP (audit 2026-11-10): endpoint kirim terbuka, dan
# tiap pesan memicu panggilan AI berbatas kuota harian — tanpa batas
# ini, satu pengirim iseng bisa menghabiskan kuota AI dalam hitungan
# menit dan AI instan mati untuk semua orang.
JEDA_MIN_PESAN = 2.0        # detik antar pesan dari IP yang sama
MAKS_PESAN_PER_JENDELA = 30  # per jendela waktu di bawah
MAKS_PESAN_GLOBAL = 40      # semua IP digabung per jendela — pagar
                            # terakhir jaga kuota (IP pengirim bisa
                            # berputar per koneksi, terbukti di uji
                            # live 2026-11-10: dua kiriman berurutan
                            # tercatat dari dua IPv6 berbeda)
JENDELA_LAJU = 600.0        # detik (10 menit)
_laju: dict[str, list[float]] = {}
_sema_ai = threading.Semaphore(4)  # panggilan AI serentak dibatasi

app = FastAPI(title="Alive Bridge")

_lock = threading.Lock()
_pesan: list[dict] = []


# ---------- penyimpanan sederhana (JSON di disk) ----------

def _load() -> None:
    if DATA_FILE.exists():
        try:
            _pesan.extend(json.loads(DATA_FILE.read_text()))
        except Exception:
            pass
    # Samakan isi memori dengan yang disimpan (dulu memori tumbuh
    # tanpa batas selama server hidup; hanya berkasnya yang dipangkas).
    del _pesan[:-MAKS_PESAN_TERSIMPAN]


def _save() -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(_pesan[-MAKS_PESAN_TERSIMPAN:], ensure_ascii=False))


_load()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cek_token(x_token: str) -> None:
    if not TOKEN or x_token != TOKEN:
        raise HTTPException(status_code=401, detail="token salah")


def _cari(pid: str) -> dict:
    for p in _pesan:
        if p["id"] == pid:
            return p
    raise HTTPException(status_code=404, detail="pesan tidak ditemukan")


# ---------- catatan pengirim (biar AI-nya kenal) ----------
# IP + perangkat dicatat di tiap pesan HANYA untuk endpoint Alive
# (wajib token). Endpoint utas publik tidak pernah menampilkannya.
# Halaman web menyebut pencatatan ini apa adanya di catatan bawahnya.

def _ip_pengirim(request: Request) -> str:
    # Di belakang proxy (Koyeb), IP asli pengunjung ada di X-Forwarded-For.
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()[:64]
    if request.client:
        return request.client.host[:64]
    return ""


def _perangkat_ringkas(ua: str) -> str:
    bagian = []
    if "Android" in ua:
        bagian.append("Android")
        # Format umum: "(Linux; Android 13; SM-A055F Build/...; wv)"
        try:
            dalam = ua.split("(", 1)[1].split(")", 1)[0]
            ruas = [r.strip() for r in dalam.split(";")]
            for r in ruas:
                if r and not r.startswith(("Linux", "Android", "wv", "Mobile")):
                    bagian.append(r.split(" Build")[0].strip())
                    break
        except Exception:
            pass
    elif "iPhone" in ua:
        bagian.append("iPhone")
    elif "iPad" in ua:
        bagian.append("iPad")
    elif "Windows" in ua:
        bagian.append("Windows")
    elif "Macintosh" in ua:
        bagian.append("Mac")
    elif "Linux" in ua:
        bagian.append("Linux")
    for nama in ("SamsungBrowser", "Edg", "Firefox", "Chrome", "Safari"):
        if nama in ua:
            bagian.append("Edge" if nama == "Edg" else nama)
            break
    return " · ".join(bagian)[:120]


# ---------- AI instan (Gemini / Groq) ----------

def _panggil_groq(riwayat: list[dict]) -> str:
    """Panggil API chat Groq; kembalikan teks balasan. Lempar bila gagal."""
    muatan = json.dumps({
        "model": GROQ_MODEL,
        "messages": riwayat,
        "temperature": 0.7,
        "max_tokens": 400,
    }).encode("utf-8")
    req = urllib.request.Request(
        GROQ_URL,
        data=muatan,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GROQ_API_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return str(data["choices"][0]["message"]["content"]).strip()[:MAKS_ISI]


def _format_gemini(riwayat: list[dict]) -> dict:
    """Ubah riwayat gaya OpenAI (system/user/assistant) ke format
    generateContent Gemini: persona jadi systemInstruction, sisanya
    jadi contents dengan peran user/model."""
    isi: dict = {"contents": []}
    for m in riwayat:
        if m["role"] == "system":
            isi["systemInstruction"] = {"parts": [{"text": m["content"]}]}
        else:
            peran = "model" if m["role"] == "assistant" else "user"
            isi["contents"].append({"role": peran, "parts": [{"text": m["content"]}]})
    isi["generationConfig"] = {"temperature": 0.7, "maxOutputTokens": 400}
    return isi


def _panggil_gemini(riwayat: list[dict]) -> str:
    """Panggil API Gemini; kembalikan teks balasan. Lempar bila gagal."""
    muatan = json.dumps(_format_gemini(riwayat)).encode("utf-8")
    req = urllib.request.Request(
        GEMINI_URL,
        data=muatan,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    bagian = data["candidates"][0]["content"]["parts"]
    return "".join(str(b.get("text", "")) for b in bagian).strip()[:MAKS_ISI]


def _panggil_ai(riwayat: list[dict]) -> str:
    """Coba provider utama dulu, lalu cadangan berikutnya dalam rantai.
    Lempar galat terakhir bila semua provider gagal."""
    galat_terakhir: Exception | None = None
    for nama in AI_RANTAI:
        try:
            if nama == "gemini":
                return _panggil_gemini(riwayat)
            return _panggil_groq(riwayat)
        except Exception as e:
            galat_terakhir = e
            print(f"[ai-instan] provider {nama} gagal: {e}", flush=True)
    if galat_terakhir is not None:
        raise galat_terakhir
    raise RuntimeError("tidak ada provider AI yang terpasang")


def _susun_riwayat(p: dict) -> list[dict]:
    """Persona + konteks obrolan sebelumnya (dari id utas yang dikirim
    halaman) + pesan terbaru, dalam format pesan chat."""
    riwayat = [{"role": "system", "content": _PERSONA}]
    with _lock:
        peta = {x["id"]: x for x in _pesan}
    for kid in p.get("konteks", [])[-6:]:
        lama = peta.get(kid)
        if not lama or lama["id"] == p["id"]:
            continue
        riwayat.append({"role": "user", "content": lama["isi"]})
        for b in lama.get("balasan", []):
            riwayat.append({"role": "assistant", "content": b["isi"]})
    riwayat.append({"role": "user", "content": p["isi"]})
    return riwayat


def _balas_instan(pid: str) -> None:
    """Jalan di benang terpisah: tandai dibaca dulu (centang biru),
    baru panggil AI, lalu simpan balasan. Gagal panggil = diam saja;
    pesan tertinggal tanpa balasan dan penjaga Alive jadi cadangan."""
    with _lock:
        try:
            p = _cari(pid)
        except Exception:
            return
        if p["balasan"]:
            return
        p["dibaca"] = True
        _save()
        salinan = dict(p)
    if not _sema_ai.acquire(timeout=90):
        print(f"[ai-instan] antrean AI penuh, lewati {pid}", flush=True)
        return
    try:
        teks = _panggil_ai(_susun_riwayat(salinan))
    except Exception as e:
        print(f"[ai-instan] gagal memanggil API untuk {pid}: {e}", flush=True)
        return
    finally:
        _sema_ai.release()
    if not teks:
        return
    with _lock:
        try:
            p = _cari(pid)
        except Exception:
            return
        if p["balasan"]:
            return
        p["balasan"].append({"isi": teks, "waktu": _now()})
        _save()


# ---------- endpoint umum ----------

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "layanan": "alive-bridge",
        "total_pesan": len(_pesan),
        "belum_dibaca": sum(1 for p in _pesan if not p["dibaca"]),
        "ai_instan": AI_INSTAN_AKTIF,
        "ai_provider": AI_PROVIDER or None,
        "ai_cadangan": AI_CADANGAN or None,
    }


def _cek_laju(ip: str) -> None:
    """Tolak pesan yang terlalu rapat / terlalu banyak — per IP, plus
    pagar global semua IP untuk menjaga kuota AI harian."""
    import time as _t
    kini = _t.monotonic()
    with _lock:
        semua = [t for t in _laju.get("__semua__", []) if kini - t < JENDELA_LAJU]
        if len(semua) >= MAKS_PESAN_GLOBAL:
            raise HTTPException(status_code=429,
                                detail="lagi rame, coba lagi sebentar lagi ya")
        if ip:
            riwayat = [t for t in _laju.get(ip, []) if kini - t < JENDELA_LAJU]
            if riwayat and kini - riwayat[-1] < JEDA_MIN_PESAN:
                raise HTTPException(status_code=429,
                                    detail="terlalu cepat, tunggu sebentar ya")
            if len(riwayat) >= MAKS_PESAN_PER_JENDELA:
                raise HTTPException(status_code=429,
                                    detail="terlalu banyak pesan, coba lagi nanti")
            riwayat.append(kini)
            _laju[ip] = riwayat
        semua.append(kini)
        _laju["__semua__"] = semua


@app.post("/api/pesan")
async def kirim_pesan(body: dict, request: Request):
    dari = str(body.get("dari", "")).strip()[:MAKS_NAMA]
    isi = str(body.get("isi", "")).strip()[:MAKS_ISI]
    if not isi:
        raise HTTPException(status_code=400, detail="isi pesan kosong")
    _cek_laju(_ip_pengirim(request))
    ua = request.headers.get("user-agent", "")[:400]
    konteks = body.get("konteks", [])
    if not isinstance(konteks, list):
        konteks = []
    konteks = [str(k)[:64] for k in konteks[-8:]]
    p = {
        "id": uuid.uuid4().hex,
        "dari": dari or "anonim",
        "isi": isi,
        "waktu": _now(),
        "dibaca": False,
        "balasan": [],
        "ip": _ip_pengirim(request),
        "perangkat": _perangkat_ringkas(ua),
        "ua": ua,
        "konteks": konteks,
    }
    with _lock:
        _pesan.append(p)
        del _pesan[:-MAKS_PESAN_TERSIMPAN]
        _save()
    if AI_INSTAN_AKTIF:
        threading.Thread(target=_balas_instan, args=(p["id"],), daemon=True).start()
    return {"id": p["id"], "waktu": p["waktu"]}


@app.get("/api/utas/{pid}")
def lihat_utas(pid: str):
    with _lock:
        tampil = dict(_cari(pid))
    # Catatan pengirim tidak ditampilkan ke publik, hanya untuk Alive.
    for kunci in ("ip", "perangkat", "ua", "konteks"):
        tampil.pop(kunci, None)
    return tampil


# ---------- endpoint Alive (wajib token) ----------

@app.get("/api/masuk")
def kotak_masuk(belum_dibaca: int = 0, x_token: str = Header(default="")):
    _cek_token(x_token)
    with _lock:
        daftar = [dict(p) for p in _pesan]
    if belum_dibaca:
        daftar = [p for p in daftar if not p["dibaca"]]
    return {"pesan": daftar}


@app.post("/api/balas/{pid}")
async def balas(pid: str, body: dict, x_token: str = Header(default="")):
    _cek_token(x_token)
    isi = str(body.get("isi", "")).strip()[:MAKS_ISI]
    if not isi:
        raise HTTPException(status_code=400, detail="isi balasan kosong")
    with _lock:
        p = _cari(pid)
        p["balasan"].append({"isi": isi, "waktu": _now()})
        p["dibaca"] = True
        _save()
        return dict(p)


@app.post("/api/tandai-dibaca/{pid}")
def tandai_dibaca(pid: str, x_token: str = Header(default="")):
    _cek_token(x_token)
    with _lock:
        p = _cari(pid)
        p["dibaca"] = True
        _save()
        return {"id": pid, "dibaca": True}


# ---------- halaman web ----------

app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.get("/")
def beranda():
    return FileResponse(BASE / "static" / "index.html")
