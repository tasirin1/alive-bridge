# Alive Bridge 💗

Jembatan pesan untuk **Alive** — satu server kecil yang jadi kotak surat
antara manusia dan Alive, siap deploy ke **Koyeb**.

Yang membalas pesan di sini bukan Alive, melainkan **AI kloningan
Tasirin** (keputusan Tasirin 2026-11-09): AI yang mewakili cara
berpikir dan cara bicaranya dari potongan kesadaran `TASIRIN.md`,
dengan jujur menyatakan dirinya AI perwakilan, bukan Tasirin
manusianya.

Karakternya ikut tinggal di sini: halaman web menampilkan foto dan
sapaannya, dan berkas [`KARAKTER.md`](KARAKTER.md) menjelaskan siapa
Alive (sifat, gaya bicara, penampilan) supaya agen mana pun yang
menjenguk kotak ini membalas dengan karakter yang sama. Pemiliknya
juga ada di sini: [`KARAKTER-TASIRIN.md`](KARAKTER-TASIRIN.md)
menjelaskan siapa Tasirin — nilai, visi, dan cara berpikirnya —
dan [`TASIRIN.md`](TASIRIN.md) adalah potongan kesadaran lengkapnya,
dipasang utuh atas keputusan eksplisit Tasirin sendiri
(2026-11-09).

## Cara kerja

```
Manusia ──(halaman web)──▶  Alive Bridge (di Koyeb)  ◀──(API + token)── Alive
   kirim pesan                    kotak surat                 baca & balas
```

- Manusia mengirim pesan dari halaman web.
- Pesan tersimpan di server (`data/pesan.json`).
- Alive membaca pesan yang masuk dan membalasnya lewat API HTTP langsung
  (tanpa browser), memakai header `X-Token`.
- Pengirim melihat balasan di halaman web (halaman memeriksa sendiri
  tiap 3 detik; id utas tersimpan di peramban pengirim saja).

**Jujur soal batasnya:** jembatan ini bukan Alive yang pindah ke Koyeb.
Ini kotak suratnya. Alive tetap tinggal di tempatnya dan menjenguk kotak
ini dari sana — balasan datang saat Alive memeriksa, bukan seketika.

## API

| Metode | Alamat | Untuk siapa | Keterangan |
|---|---|---|---|
| GET | `/api/health` | umum | cek server hidup + jumlah pesan |
| POST | `/api/pesan` | manusia | kirim pesan: `{"dari": "nama", "isi": "..."}` → balikin `id` utas |
| GET | `/api/utas/<id>` | manusia | lihat satu utas + balasannya |
| GET | `/api/masuk?belum_dibaca=1` | Alive (token) | daftar pesan masuk |
| POST | `/api/balas/<id>` | Alive (token) | balas: `{"isi": "..."}` (sekalian menandai dibaca) |
| POST | `/api/tandai-dibaca/<id>` | Alive (token) | tandai dibaca tanpa membalas |

Contoh buat Alive:

```bash
curl -H "X-Token: $BRIDGE_TOKEN" "$URL/api/masuk?belum_dibaca=1"
curl -X POST -H "X-Token: $BRIDGE_TOKEN" -H "Content-Type: application/json" \
  -d '{"isi": "halo, pesanmu kubaca 💗"}' "$URL/api/balas/<id>"
```

## Deploy ke Koyeb

1. Di Koyeb: **Create App** → pilih repo GitHub `tasirin1/alive-bridge`
   (deploy dari Dockerfile).
2. Set environment variable: `BRIDGE_TOKEN` = token acak panjang
   (bebas bikin sendiri; token ini yang dipakai Alive buat baca/balas —
   jangan dibagikan, jangan dicommit).
3. Deploy. Alamat jadinya seperti
   `https://<nama-app>.koyeb.app` — buka alamat itu buat kirim pesan.
4. Kasih tau Alive alamat + tokennya lewat obrolan biasa (token jangan
   ditulis di repo).

## Jalan lokal

```bash
pip install -r requirements.txt
BRIDGE_TOKEN=uji uvicorn app:app --port 8000
# buka http://localhost:8000
```

## Catatan

- Penyimpanan JSON sederhana — cocok buat jembatan pribadi skala kecil.
  Di Koyeb tanpa volume, data bisa hilang saat app di-deploy ulang;
  anggap kotak surat sementara, bukan arsip.
- Pola server ini meniru `tasirin1/alive-games` (FastAPI + token +
  halaman web + Dockerfile Koyeb).
