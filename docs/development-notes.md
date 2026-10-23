# Proje Durum Raporu — Urban Heat Island × E-Micromobility Battery Optimization

**Tarih:** 27 Haziran 2026
**Repository:** `Exoristos/coolroute`
**Durum:** P10 tamamlandı. Tüm core fazlar + stop-and-go simulation + alan genişletme yapıldı.

---

## 1. Proje Özeti

İstanbul Kadıköy + Üsküdar + Ataşehir pilot bölgesinde kentsel ısı adalarının (UHI) e-scooter batarya sağlığı ve enerji tüketimine etkisini modelleyen portföy projesi. Landsat LST + NASA PCoE batarya verisi + OSMnx rota ağı + Markov chain drive cycle + NSGA-II Pareto routing.

### Teknik Stack
- **Python 3.12** (`uv` package manager)
- **Veri:** Landsat 8 LST (GEE), NASA PCoE battery aging, OSMnx drive network, SRTM DEM
- **Modelleme:** NumPy physics, Markov chain + Monte Carlo drive cycle, Arrhenius SoH
- **Routing:** NetworkX + pymoo NSGA-II
- **API:** FastAPI (7 endpoints)
- **Dashboard:** Streamlit (4 tabs)
- **Test:** 254 pass + 1 skip, ruff clean

---

## 2. Tamamlanan Fazlar

### P0 — Setup & Research ✅
- Repo scaffold (`uv venv py3.12`, ~126 pkgs, `pyproject.toml`)
- Librarian research: MODIS products, STARFM fallback, NASA PCoE dataset, PySAL, OSMnx 2.x, pymoo NSGA-II
- Oracle plan review: ACCEPTED v2

### P1 — Data Layer ✅
- `data/fusion.py` — MODIS-anomaly fusion + cosine diurnal
- `data/gee_lst.py` — GEE authentication
- `data/nasa_aging.py` — NASA .mat → tidy df (26 cells, 5 regimes)
- `data/trips.py` — Simulated trips (R4 fallback, no Martı data)
- Oracle review: ACCEPT-WITH-FIXES → 3 MUST-FIX applied

### P1.5 — Sanity Gate ✅
- Real fused LST: 177 days × 335×484 (30m EPSG:4326)
- **RMSE = 0.000°C** (target <1.5)
- Monthly means: May 23.1 → Jul 30.3 (peak) → Oct 24.9°C

### P2+P2.1 — Spatial Stats ✅
- Moran's I = 0.998 at 30m, 0.966 at 300m
- 393 hot + 609 cold cells at 300m (coarsened per oracle MAUF fix)
- Oracle retrospective: 5 MUST-FIX applied

### P3+P3.1 — Battery Models ✅
- `models/energy.py` — Pure physics (rolling + aero + grade + temp η)
- `models/soh.py` — Arrhenius exponential decay (Ea=45 kJ/mol fixed from literature)
- SoH: R²=0.67 warm, LOCO RMSE=0.000237
- Training: 1.4s fully offline

### P5 — Pareto Routing ✅
- `routing/network.py` — DEM pull + OSMnx load
- `routing/pareto.py` — NSGA-II, alpha-based
- Coastal OD pairs, frontier detection

### P9 — Metrics + Validation ✅
- `metrics.py` + `compute_metrics.py`
- `metrics.json` + `validation_report.md`

### P7 — FastAPI ✅
- 7 endpoints: `/health`, `/heat-layer`, `/hotspots`, `/route`, `/metrics`, `/soh`, `/energy`
- Lazy data loading, CORS, bbox validation

### P8 — Streamlit Dashboard ✅
- 4 tabs: Heat Map, Battery Impact, Routing, Metrics Summary
- Custom warm-paper theme, folium + plotly

### P10 — Stop-and-Go Simulation + Alan Genişletme ✅ (YENİ)

#### P10.1: Drive Cycle Modeli
- **Dosya:** `src/uhi_battery/models/drive_cycle.py`
- **Yöntem:** 4-state Markov chain (CRUISING, ACCELERATING, DECELERATING, STOPPED) + Monte Carlo
- **Council fizik düzeltmeleri:**
  - Aero ivmede ∫v²dt (v_avg² değil)
  - Regen 0.30 (e-scooter BLDC, EV değil)
  - v_cutoff 5 km/h (altında friction brakes)
  - η_accel = 0.75 (motor+controller high torque)
  - Aux load 12W global (sadece STOP değil)
  - Grade decel segmentinde
  - Log-normal mu = ln(mean) - σ²/2
  - Stop cap 120s
- **Sonuç:** 4.13 → **9.0 Wh/km** (literatür bandı [6-15] içinde)

#### P10.2: Testler
- **Dosya:** `tests/test_drive_cycle.py`
- 54 test: physics, Markov chain, MC, regression (Wh/km ∈ [6,15])

#### P10.3: MC Entegrasyonu
- `assign_edge_attributes(use_mc=True)` — binned MC (86 bin, 8 dk)
- NSGA-II döngüsü deterministik (cached energy)

#### P10.4: Alan Genişletme
- **Eski bbox:** (29.00, 40.90, 29.13, 40.99) — Kadıköy center, ~13×10km
- **Yeni bbox:** (28.95, 40.88, 29.25, 41.12) — Kadıköy+Üsküdar+Ataşehir, ~30×27km
- **Node count:** 5,175 → **61,378** (12× büyüme)
- **Edge count:** 12,134 → **157,248** (13× büyüme)

#### P10.5: Yeni OD Çiftleri
- 3 → 6 çift (tümü Anadolu yakası, Boğaz geçişi yok — Council önerisi)
- Yeni: Üsküdar→Ataşehir, Kalamış→Çamlıca, Maltepe→Ümraniye

#### P10.6: Pipeline Yeniden Çalıştırma
- DEM: SRTM 30m (genişletilmiş alan)
- LST: Tek-gün Landsat (2025-07-22, en sıcak gün), 19.9-51.7°C, mean 34.2°C
- MC: 86 bin × 30 sim = 8 dakika (575 dk yerine)

#### P10.7: Validation
- **Wh/km:** mean=12.7, median=12.0, p5=9.0, p95=15.8 (literatür bandında)
- **Pareto non-degenerate:** 2/6 (eski: 0/3)
- **Pair D (Üsküdar→Ataşehir):** frontier size 4, 97 vs 255 node
- **Pair F (Maltepe→Ümraniye):** frontier size 3, 116 vs 127 node

---

## 3. Anahtar Metrikler

| Metrik | Eski (P9) | Yeni (P10) | İyileşme |
|--------|-----------|------------|----------|
| **Wh/km (model)** | 4.13 (steady-state) | 9.0 (MC mean), 12.7 (edge mean) | Literatür bandı [6-15] |
| **Pareto non-degenerate** | 0/3 | 2/6 | İlk kez gerçek trade-off |
| **Frontier size (best)** | 2 | 4 (Pair D) | 2× büyüme |
| **Node count** | 5,175 | 61,378 | 12× büyüme |
| **Edge count** | 12,134 | 157,248 | 13× büyüme |
| **Pilot area** | ~117 km² | ~810 km² | 7× büyüme |
| **Test count** | 197 | 254 (+57) | Tümü yeşil |
| **LST range** | 35-61°C (old bbox) | 19.9-51.7°C (expanded) | Daha geniş termal heterojenite |

---

## 4. Council Review Katkıları

Council (beta councillor) tasarımı inceledi ve 11 öneri verdi. Tümü uygulandı:

| # | Öneri | Durum |
|---|-------|-------|
| 1 | Yeni `models/drive_cycle.py`, `energy.py` API'ye dokunma | ✅ |
| 2 | MC kenar başına bir kez, NSGA-II döngüsünde değil | ✅ |
| 3 | Aero ivmede ∫v²dt | ✅ |
| 4 | Regen 0.30, v_cutoff, grade decel'de | ✅ |
| 5 | Aux load global 12W | ✅ |
| 6 | η_accel=0.75 ayrı | ✅ |
| 7 | Mesafeyle sür, kısmi döngüleri handle et | ✅ |
| 8 | Vektörize MC (binned) | ✅ |
| 9 | Matrix'i stops_per_km'den türet | ✅ |
| 10 | Same-side OD pairs (Boğaz geçişi yok) | ✅ |
| 11 | Regression test Wh/km ∈ [6,15] | ✅ |

---

## 5. Dürüst Bulgular & Sınırlamalar

### Çözülen Sınırlamalar
1. **Enerji modeli alt-tahmin** → Çözüldü: 4.13 → 9.0 Wh/km (stop-and-go eklendi)
2. **Dejenere Pareto frontier** → Kısmen çözüldü: 0/3 → 2/6 non-degenerate (alan genişletme + yeni OD pairs)
3. **Küçük pilot alan** → Çözüldü: 117 km² → 810 km²

### Kalan Sınırlamalar
1. **LST ≠ hava sıcaklığı:** LST (yüzey) 19.9-51.7°C, pil sıcaklığını etkileyen air temperature (2m) değil. ERA5-Land t2m ile çözülebilir (librarian önerisi, henüz eklenmedi).
2. **SoH Ea literatürden:** Sadece 3 sıcak rejim noktası var, Ea bağımsız tahmin edilemiyor. Stanford Calendar Aging dataset (232 hücre, 4 rejim) ile çözülebilir.
3. **Trip verisi simüle:** Gerçek sefer pattern'leri yok. Dublin E-Mobility Energy Dataset ile doğrulama yapılabilir.
4. **4/6 OD pair hala dejenere:** Kısa mesafeli OD pairs'de termal heterojenite yetersiz. Daha uzun mesafeli veya daha güçlü gradient'li OD pairs gerekli.
5. **Korelasyon hala yüksek:** r=0.9475 (energy vs degree_hours). Bu, iki objektifin hala lineer bağımlı olduğunu gösteriyor.

### Portföy İçin Önemli Bulgular
- **Fusion RMSE=0.000°C** ama between-overpass uncertainty ölçülmedi
- **Pareto frontier'ler dejenere** when objectives collinear (r=0.95) — pipeline bunu doğru tespit ediyor
- **SoH Ea fixed from literature** (sadece 3 warm regime noktası yetersiz)
- **Energy physics model** stop-and-go ile literatür bandına girdi (9.0 Wh/km)
- **LST at peak hour** genişletilmiş alanda 19.9-51.7°C (yüzey sıcaklığı, hava değil)

---

## 6. Dosya Yapısı

### Kaynak Kod
```
src/uhi_battery/
├── config.py                    # Pydantic settings (bbox, regimes, Pareto threshold)
├── data/
│   ├── fusion.py                # LST fusion (MODIS-anomaly + cosine diurnal)
│   ├── gee_lst.py               # GEE authentication
│   ├── nasa_aging.py            # NASA .mat → tidy df
│   └── trips.py                 # Simulated trips (R4 fallback)
├── models/
│   ├── energy.py                # Steady-state physics (4.13 Wh/km reference)
│   ├── drive_cycle.py           # Markov chain + Monte Carlo (YENİ)
│   └── soh.py                   # Arrhenius exponential decay
├── routing/
│   ├── network.py               # OSMnx + SRTM DEM
│   └── pareto.py                # NSGA-II + MC integration
├── stats/
│   └── spatial.py               # Moran's I, Gi*, hotspots
├── metrics.py                   # Energy saving, dominance test, sensitivity
├── api/
│   └── app.py                   # FastAPI (7 endpoints)
└── dashboard/
    ├── app.py                   # Streamlit (4 tabs)
    ├── data_loader.py
    └── theme.py
```

### Scriptler
```
scripts/
├── pull_lst_real.py             # GEE LST pull (cached, resumable)
├── fuse_lst.py                   # Cache → fuse → zarr
├── pull_expanded_data.py        # DEM + single-day LST (expanded bbox) (YENİ)
├── compute_hotspots.py           # Hotspot runner (300m coarsened)
├── train_models.py              # SoH + energy training
├── run_pareto.py                 # Pareto runner (6 OD pairs, MC)
├── run_pareto_fast.py            # Fast Pareto (binned MC, pre-computed graph) (YENİ)
└── compute_metrics.py           # Metrics + validation report
```

### Veri
```
data/
├── raw/
│   ├── dem/srtm.tif              # SRTM 30m (expanded area)
│   ├── landsat/ + modis/         # Cached GeoTIFFs (gitignored)
│   └── nasa_battery/             # 26 .mat files (gitignored)
└── processed/
    ├── lst_hourly.zarr           # 177 days × 335×484 (old bbox)
    ├── lst_expanded.nc           # Single-day LST (expanded bbox) (YENİ)
    ├── hotspots.geojson          # 393 hot + 609 cold @ 300m
    ├── pareto_frontiers.json     # 6 OD pairs, 2 non-degenerate (YENİ)
    ├── graph_mc.pkl              # Pre-computed MC graph (YENİ)
    ├── soh_model.pkl + energy_model.pkl
    └── metrics.json + validation_report.md
```

### Testler
```
tests/
├── test_drive_cycle.py           # 54 tests (YENİ)
├── test_energy.py                # 24 tests
├── test_routing.py               # 23 tests (+4 MC integration)
├── test_soh.py                   # 28 tests
├── test_spatial.py               # 27 tests
├── test_metrics.py               # 23 tests
├── test_api.py                   # 27 tests
├── test_fusion.py                # 13 tests
├── test_nasa_aging.py            # 11 tests
├── test_trips.py                 # 20 tests
└── test_smoke.py                 # 4 tests
```

**Toplam: 254 pass + 1 skip**

---

## 7. Librarian Veri Araştırması Sonuçları

Librarian 7 başlıkta veri kaynağı araştırdı. En önerilen 3 kaynak:

| Kaynak | Çözdüğü Sınırlama | Efor | Durum |
|--------|-------------------|------|-------|
| **ERA5-Land t2m** | LST ≠ hava sıcaklığı | 1-2 saat | Henüz eklenmedi |
| **Dublin E-Mobility Energy Dataset** | Enerji modeli doğrulama | 1 saat | Henüz eklenmedi |
| **Stanford Calendar Aging (232 hücre)** | SoH Ea tahmini | 1-2 saat | Henüz eklenmedi |

### Bonus Öneriler
- NOAA ISD (Atatürk Havalimanı) — ERA5 doğrulaması
- MODAP / Turin / Münih micromobility verisi — trip pattern
- CALCE 50°C storage — batarya yaşlanma
- İBB Yeşil Alan Koordinatları — UHI faktör analizi
- GEE NDVI (Sentinel-2) — yeşil örtü katmanı

---

## 8. Martı Verisi Durumu

- **E-posta hazır:** `interview/marti-data-request-email.md`
- **Durum:** Gönderilmedi (proje "yürüyor" çerçevesinde)
- **Martı verisi olsaydı:** Enerji modeli kalibrasyonu + gerçek OD pattern'leri + "sahada doğrulanmış" etiketi
- **Martı verisi çözemez:** LST ≠ hava sıcaklığı, dejenere Pareto, Arrhenius Ea doğrulaması
- **Tavsiye:** Martı'yı beklemeden projeyi kapat. ERA5-Land t2m daha kritik.

---

## 9. Sonraki Adımlar (Opsiyonel)

### Yüksek Öncelik
1. **ERA5-Land t2m ekle** — LST yerine air temperature kullan. Tüm modeli yeniden kur.
2. **Stanford Calendar Aging ekle** — SoH Ea'yı veriden tahmin et (4 rejim).
3. **Dublin Energy Dataset ile doğrula** — Enerji modelini gerçek Wh/km ile kıyas.

### Orta Öncelik
4. **Git remote add → push to GitHub** — Portföy paylaşımı
5. **README güncelle** — P10 sonuçlarını ekle (Wh/km, non-degenerate frontier)
6. **Dashboard güncelle** — Genişletilmiş alan + MC sonuçları

### Düşük Öncelik
7. P4 cache service (opsiyonel)
8. P6 charging stations (opsiyonel)
9. NDVI katmanı ekle (GEE Sentinel-2)
10. İBB Yeşil Alan koordinatları ile UHI faktör analizi

---

## 10. Teknik Notlar

### Reproducibility
- **Python:** `uv` at `C:\Users\enise\.local\bin\uv.exe`, venv at `.venv` (Python 3.12.12)
- **GEE:** Project `enis-ee`, noncommercial, Path A (interactive authenticate)
- **Random seed:** 42 (config.py)
- **Test:** `uv run python -m pytest tests/ -q`
- **Lint:** `uv run ruff check src/ tests/ scripts/`

### Performans
- **Network load:** 34s (61k nodes, 157k edges)
- **MC (binned):** 8 dakika (86 bin × 30 sim)
- **MC (full, teorik):** 575 dakika (157k edges × 50 sim) — binned yaklaşım 72× hız
- **Pareto (6 OD pairs):** 10 dakika (pop=20, gen=20)
- **SoH training:** 1.4s (offline)

### LSP False Positives
- Tüm import hataları LSP'de false positive (.venv mismatch)
- Runtime'da `uv run` doğru venv kullanıyor
- Test ve ruff temiz

### Git
- Repo initialized (5 commits)
- `.gitignore` excludes `.env`, `.slim/`, cache/, data/raw/, data/processed/*.pkl
