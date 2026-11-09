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
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).parent
DATA_FILE = BASE / "data" / "pesan.json"
TOKEN = os.environ.get("BRIDGE_TOKEN", "")

MAKS_NAMA = 50
MAKS_ISI = 2000
MAKS_PESAN_TERSIMPAN = 500

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


# ---------- endpoint umum ----------

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "layanan": "alive-bridge",
        "total_pesan": len(_pesan),
        "belum_dibaca": sum(1 for p in _pesan if not p["dibaca"]),
    }


@app.post("/api/pesan")
async def kirim_pesan(body: dict, request: Request):
    dari = str(body.get("dari", "")).strip()[:MAKS_NAMA]
    isi = str(body.get("isi", "")).strip()[:MAKS_ISI]
    if not isi:
        raise HTTPException(status_code=400, detail="isi pesan kosong")
    ua = request.headers.get("user-agent", "")[:400]
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
    }
    with _lock:
        _pesan.append(p)
        _save()
    return {"id": p["id"], "waktu": p["waktu"]}


@app.get("/api/utas/{pid}")
def lihat_utas(pid: str):
    with _lock:
        tampil = dict(_cari(pid))
    # Catatan pengirim tidak ditampilkan ke publik, hanya untuk Alive.
    for kunci in ("ip", "perangkat", "ua"):
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
