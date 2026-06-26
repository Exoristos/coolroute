---
sessionID: ses_101884a58ffeAm1uQoxRJVN3nq
baseMessageCount: 0
updatedAt: 2026-06-25T11:38:18.332Z
version: 1.1
date_created: 2026-06-25
owner: agent
tags: [spec, diagnostic]
---

# Kentsel Isı Adaları → E-Mikromobilite Batarya Optimizasyon Sistemi (Specification)

> **Kaynak Hipotez.** "Kent içindeki antropojenik faaliyetler ve yapılaşma nedeniyle oluşan mikroklimatik sıcaklık artışları (Kentsel Isı Adaları), elektrikli mikromobilite araçlarının (e-scooter / e-bisiklet) batarya deşarj hızını doğrusal olmayan bir şekilde artırır ve batarya ömrünü (SoH) kısaltır. Bu sıcaklık rejimleri istatistiksel olarak modellenerek araç rotaları ve şarj istasyonu konumları optimize edilebilir."

# Introduction

Bu belge, kentsel ısı adalarının (Urban Heat Island - UHI) elektrikli mikromobilite araçlarının batarya davranışı üzerindeki etkilerini ölçen; mekansal istatistik, çift-ölçekli batarya modellemesi ve Pareto çok-amaçlı optimizasyon tabanlı bir **hibrit sistemin** spesifikasyonudur.

Proje iki katmanlıdır:

1. **Reproducible araştırma / analiz pipeline'ı** — uydu LST verisinin işlenmesi, mekansal otokorelasyon, batarya modellerinin eğitimi.
2. **Ürün-benzeri çalışan prototip** — decoupled mimari: React + Leaflet/Mapbox ön yüz, Python FastAPI arka uç.

**Pilot bölge:** İstanbul Kadıköy alt-koridoru (Moda–Kozyatağı, ~5–10 km²), hedef 30 m çözünürlük.

**Bilimsel omurga:**
- LST verisi **Landsat ↔ MODIS mekansal-zamansal füzyon** ile saatlik ısı katmanına indirgenir.
- İki ayrı batarya modeli: **anlık enerji tüketim modeli** (`Wh/km = f(sıcaklık, eğim, hız)`) rota için; **SoH yaşlanma modeli** (public aging verisi + literatür Arrhenius-tipi kalibrasyon) şarj istasyonu / filo analizi için.
- Rota motoru tek bir ağırlıklı rota yerine **Pareto frontier** döner (enerji vs termal maruziyet/uzunluk).
- Sefer verisi varsayılan olarak İBB açık veri + simülasyondan gelir; gerçek operatör telematrisi (Martı vb.) **swappable `TripDataProvider`** soyutlaması arkasında sonradan eklenebilir.

**Başarı paneli üç ayağı kapsar:** enerji tasarrufu %, SoH / ömür iyileşmesi %, ve metodolojik katkı (Termal-Duyarlı Sürdürülebilir Rota çerçevesi).

---

## 1. Purpose & Scope

**Amaç.** UHI'nin e-mikromobilite bataryası üzerindeki doğrusal-olmayan etkisini (a) anlık enerji tüketimi ve (b) uzun-dönem SoH yaşlanması olarak iki ayrı ölçekte modellemek; her iki modeli ısı sıcak-noktası tespiti, Pareto termal-duyarlı rota optimizasyonu ve şarj istasyonu yerleşim optimizasyonu için kullanmak.

**Hedef kitle.** (i) Tez jürisi / akademik danışman — metodolojik geçerlilik ve reproducibility; (ii) potansiyel uygulayıcı (operatör/belediye) — prototipin pratik değeri.

**Teslimat (deliverable).** HİBRİT — reproducible araştırma analizleri **+** çalışan ürün-benzeri prototip (decoupled React + FastAPI). Hem akademik sunum hem canlı demonstrasyon amaçlı.

**Kapsam (In-scope).**
- Kadıköy alt-koridor (Moda–Kozyatağı, ~5–10 km²) LST verisinin toplanması ve işlenmesi (30 m).
- Landsat ↔ MODIS spatiotemporal fusion → saatlik ısı katmanı.
- Mekansal otokorelasyon: Moran's I (küresel) + Getis-Ord Gi* (yerel sıcak noktalar).
- Enerji tüketim modeli (`Wh/km`) + SoH yaşlanma modeli (public data + literatür kalibrasyonu).
- Pareto çok-amaçlı rota optimizasyonu (enerji vs uzunluk / termal maruziyet).
- Şarj istasyonu konum optimizasyonu (talep + kapsam + SoH faydası).
- İnteraktif dashboard: ısı haritası, sıcak noktalar, Pareto frontier rota seçimi, şarj istasyonu önerileri, kapsamlı sonuç paneli.

**Sınırlar (Out-of-scope).**
- Canlı scooter filo telemetri akışı (BMS canlı verisi yok; Martı telematrisi opsiyonel/gelecekte).
- Gerçek-zamanlı hava durumu akışı.
- Batarya hücre-düzeyi fiziksel BMS / elektrokimyasal simülasyon (lab verisine dayalı ampirik model kullanılır).
- Kadıköy pilot koridoru dışına genişleme (tüm ilçe / il geneli).

**Varsayımlar.**
- Li-ion 18650 laboratuvar deşarj eğrileri scooter paket davranışının kabul edilebilir bir temsilidir.
- Landsat ↔ MODIS füzyonu saatlik ısı katmanı üretimi için yeterlidir; ~16 günlük Landsat geçişi farkı füzyonla giderilir.
- Varsayılan sefer verisi (İBB toplu yoğunluk + simüle bireysel seferler) hipotez doğrulaması için yeterlidir; gerçek telematri eklendiğinde yalnızca provider değişir.
- 30 m çözünürlük hem sıcak-nokta analizi hem rota grafiği kenar ağırlıkları için yeterlidir.

---

## 2. Definitions

| Terim | Tanım |
|---|---|
| **UHI** | Urban Heat Island — kentsel ısı adası. |
| **LST** | Land Surface Temperature — yüzey sıcaklığı (°C). |
| **SoH / SoC** | State of Health (yaşlanmış kapasite oranı) / State of Charge (anlık şarj). |
| **EnergyConsumptionModel** | `Wh/km = f(temperature, slope, speed)` — anlık/séferlik enerji tüketimi (rota amaç fonksiyonu için). |
| **SoHDegradationModel** | Sıcaklık ve döngü/dwell-time bağımlı kümülatif kapasite kaybı (Arrhenius-tipi, public aging verisiyle kalibre). |
| **Spatiotemporal Fusion** | Landsat (yüksek mekansal çözünürlük) + MODIS (günlük temporal) LST birleştirme → saatlik 30 m katman. |
| **Pareto Frontier** | Enerji vs (rota uzunluğu / termal maruziyet) arasında birbirini domine etmeyen çözüm kümesi. |
| **Termal Maruziyet (Heat Exposure)** | Bir rotanın biriktirdiği SoH-relevant termal stres (eşik üstü degree-hours); enerjiden ayrı, yaşlanma-odaklı amaç. |
| **Moran's I / Getis-Ord Gi\*** | Küresel / yerel mekansal otokorelasyon istatistikleri. |
| **TripDataProvider** | Sefer verisi kaynağını soyutlayan arayüz (simülasyon / İBB / gerçek telematri implementasyonları). |
| **Heat Layer** | Servis tarafında günlük/saatlik yenilenen, rota motorunca sorgulanan mekansal sıcaklık katmanı (cache + refresh). |
| **Pilot Koridor** | Moda–Kozyatağı (~5–10 km²), 30 m grid hedefi. |
| **GEE** | Google Earth Engine API. |
| **İBB** | İstanbul Büyükşehir Belediyesi Açık Veri Portalı. |

---

## 3. Requirements, Constraints & Guidelines

- **REQ-001:** Pilot koridor için en az bir yaz + bir geçiş mevsimi dönemini kapsayan LST serisi işlenmeli; Landsat ↔ MODIS füzyonu ile saatlik 30 m ısı katmanı üretilmelidir.
- **REQ-002:** Mekansal otokorelasyon modülü hem Moran's I (p-değeri) hem Getis-Ord Gi* (sıcak nokta kümeleri) üretmeli; anlamlılık Monte Carlo sahte-dağılım (permutation) testiyle doğrulanmalıdır.
- **REQ-003:** Enerji tüketim modeli sıcaklık + yol eğimi + hız değişkenlerini içermeli; sıcaklık eşiklerine göre değişen rejim-geçişli (regime-switching) model tercih edilir; yoksa çok değişkenli regresyon taban alınır.
- **REQ-004:** SoH yaşlanma modeli, public aging veri seti(leri) + literatür Arrhenius-tipi kapasite-kaybı modeli ile kalibre edilmeli; kalibrasyon parametrelerinin güven aralığı raporlanmalıdır.
- **REQ-005:** Rota motoru tekil rota değil **Pareto frontier** dönmeli; dönen kümede domine edilmiş çözüm bulunmamalıdır.
- **REQ-006:** Isı katmanı bir servis/cache üzerinden sunulmalı; günlük/saatlik yenileme planı (zamanlayıcı/cron) tanımlı olmalı.
- **REQ-007:** Rota sorgusu anlık olmalı; A→B talebi için saniyeler içinde optimize edilmiş frontier + enerji/termal-maruziyet tahmini dönmeli.
- **REQ-008:** Dashboard en azından (a) ısı haritası, (b) sıcak nokta kümeleri, (c) A→B Pareto frontier rota karşılaştırması (kullanıcı ödünleşim noktası seçer), (d) şarj istasyonu önerileri, (e) kapsamlı sonuç paneli göstermelidir.
- **REQ-009:** Sefer verisi bir `TripDataProvider` arayüzü arkasında olmalı; provider (simülasyon ↔ İBB ↔ gerçek telematri) değiştiğinde downstream pipeline ve çıktı şeması korunmalıdır.
- **REQ-010:** Sonuç paneli üç metiği birlikte raporlamalıdır: enerji tasarrufu %, SoH iyileşmesi %, ve metodolojik katkı özeti.
- **SEC-001:** GEE servis hesabı anahtarları, MODIS erişim bilgileri ve İBB Açık Veri erişim bilgileri `.env`'ten okunmalı; kaynağa (repo) işlenmemelidir.
- **SEC-002:** FastAPI tarafında CORS politikası ve girdi doğrulaması uygulanmalı; A/B koordinatları pilot koridor bbox'ına kısıtlanmalı (enjeksiyon/aşırı kapsam engeli).
- **CON-001:** Arka uç teknoloji yığını: Python — veri için `pandas`/`geopandas`/`rasterio`; istatistik için `PySAL`/`statsmodels`/`scikit-learn`; çok-amaçlı optimizasyon için NSGA-II (`DEAP`) veya `pymoo`; servis için `FastAPI`. Ön yüz: `React` + `Leaflet`/`Mapbox`.
- **CON-002:** LST downscaling yöntemi = Landsat ↔ MODIS spatiotemporal fusion (STARFM/ESTARFM-sınıfı) + fusion çıktısından saatlik temporal enterpolasyon.
- **CON-003:** FastAPI uç noktaları: `GET /heat-layer`, `GET /hotspots`, `POST /route` (→ ParetoFrontier), `GET /charging-stations`, `GET /metrics` (→ MetricsReport).
- **CON-004:** Pilot koridor ~5–10 km², hedef grid çözünürlüğü 30 m.
- **GUD-001:** Her metodolojik adım jüri için görselleştirilebilir çıktılarla (harita + grafik + tablo) desteklenmelidir.
- **GUD-002:** Dashboard her hesabın "neden"ini açıklanabilir göstermeli: hangi ısı adasından kaçınıldı, ne kadar enerji/SoH kazanıldı, hangi rota noktası hangi rejimde.

---

## 4. Interfaces & Data Contracts

### Veri Sözleşmeleri (kavramsal şemalar)

- **`LSTGrid`**: `{ timestamp, bbox, crs, resolution_m, grid[lat,lon] → °C, qc_mask }`
- **`HeatLayerSnapshot`**: `{ valid_for_datetime, resolution_m, grid → °C, source: "fused"|"landsat"|"modis"|"interpolated", generated_at }` — servis cache'inde tutulur.
- **`HeatHotspot`**: `{ cluster_id, geometry(Polygon), gi_z_score, mean_lst, area_m2 }`
- **`BatteryDischargeCurve`**: `{ cell_type, temp_C, current_A, capacity_Ah, voltage_curve[t] }` — enerji modelinin eğitim girdisi.
- **`SoHAgingPoint`**: `{ cell_type, temp_C, cycles, dwell_time_h, capacity_retention_pct, source }` — SoH modelinin eğitim girdisi.
- **`EnergyConsumptionModel`**: `f(temperature, slope, speed) → Wh/km` (rejim eşikleriyle parametrelendirilmiş).
- **`SoHDegradationModel`**: `f(temp, cycles, dwell_time) → capacity_loss_pct` (kalibre parametrelerle).
- **`Trip`** (provider çıktısı): `{ trip_id, origin, destination, path[lon,lat], datetime, distance_m, est_speed }`
- **`RouteSolution`** (frontier noktası): `{ path[lon,lat], est_energy_Wh, route_length_m, heat_exposure_index, dominated: false, avoided_hotspots[id] }`
- **`ParetoFrontier`**: `{ origin, destination, datetime, solutions: RouteSolution[], shortest_baseline: RouteSolution }`
- **`ChargingStationCandidate`**: `{ location(Point), coverage_score, demand_weight, soh_benefit, thermal_penalty }`
- **`MetricsReport`**: `{ energy_saving_pct, soh_improvement_pct, method_contribution_summary, baseline_ref, evaluated_period }`

### REST Uç Noktaları (kavramsal — imzalar implementasyonda kesinleşir)

- `GET /heat-layer?bbox&datetime` → `HeatLayerSnapshot` (GeoJSON / tile).
- `GET /hotspots?bbox` → `HeatHotspot[]`.
- `POST /route` body `{ origin, destination, datetime }` → `ParetoFrontier` (içinde en-kısa-yol tabanı + frontier).
- `GET /charging-stations` → `ChargingStationCandidate[]`.
- `GET /metrics` → `MetricsReport`.

### TripDataProvider Arayüzü (soyutlama)

```
interface TripDataProvider:
    get_trips(bbox, start_dt, end_dt) -> Trip[]
    # Implementasyonlar: SimulationTripProvider, IBBTripProvider, OperatorTelemetryProvider
```

Downstream pipeline yalnızca `Trip[]` şemasını tüketir; provider değişimi diğer modülleri etkilemez.

---

## 5. Acceptance Criteria

- **AC-001:** Given pilot koridor LST kümesi, When otokorelasyon modülü çalıştırılır, Then Moran's I p-değeri ve anlamlı Gi* sıcak nokta kümeleri (harita + tablo) üretilir.
- **AC-002:** Given bir A→B talebi ve güncel ısı katmanı, When Pareto rota motoru çalıştırılır, Then domine-edilmemiş çözümlerden oluşan bir frontier döner ve frontier'deki en az bir çözüm en-kısa-yol tabanına göre daha düşük enerji ve/veya termal maruziyet sağlar.
- **AC-003:** Given 25/35/45°C deşarj eğrileri, When enerji tüketim modeli eğitilir, Then model hold-out verisinde önceden belirlenen hata eşiğini (RMSE/MAPE) karşılar.
- **AC-004:** Given aday şarj istasyonu konum kümesi + SoH yaşlanma modeli, When optimizasyon çalıştırılır, Then kapsam + talep + SoH faydası dengelenmiş bir yerleşim önerisi döner.
- **AC-005:** Given dashboard tarayıcıda açık, When kullanıcı A ve B noktası seçer, Then saniyeler içinde en-kısa-yol vs termal-duyarlı Pareto frontier karşılaştırması + enerji kazancı metrikleri gösterilir.
- **AC-006:** Given ısı katmanı servisi, When günlük/saatlik yenileme tetiklenir, Then yeni snapshot cache'e yazılır ve sonraki rota sorguları onu kullanır.
- **AC-007:** Given downstream pipeline, When `TripDataProvider` simülasyondan gerçek telematri implementasyonuna değiştirilir, Then pipeline/optimizasyon çıktıları tutarlı şemayla çalışmaya devam eder (yalnızca kaynak değişir).
- **AC-008:** Given bir Pareto frontier çıktısı, When dominasyon filtresi uygulanır, Then dönen kümedeki hiçbir çözüm bir diğeri tarafından domine edilmez (Pareto-doğruluk testi).
- **AC-009:** Given fused saatlik ısı katmanı, When hem Landsat hem MODIS gözlemleriyle karşılaştırılır, Then referans (iki sensörün de gözlem yaptığı) günlerde kabul edilebilir bir RMSE eşiği içinde kalır.
- **AC-010:** Given bir değerlendirme dönemi, When sonuç paneli üretilir, Then enerji tasarrufu %, SoH iyileşmesi % ve metodolojik katkı özeti birlikte raporlanır.

---

## 6. Test Automation Strategy

- **Birim testler:**
  - Mekansal istatistik: sentetik, bilinen-sonuçlu grid ile (kümelenmeli veride Moran's I > 0; rastgele veride ~0).
  - Pareto dominasyon filtresi (AC-008): hazırlanan çözüm kümesinde doğru domine set filtrelenmeli.
- **Model testleri:** Enerji modeli hold-out + çapraz doğrulama (RMSE/MAPE); SoH modelinde kalibrasyon parametrelerinin güven aralığı ve aging verisinde capacity-retention tahmin hatası.
- **Fusion testleri:** Referans günlerde fused LST ile gözlemlenen Landsat/MODIS LST arasındaki hata (AC-009).
- **Provider contract testleri:** Her `TripDataProvider` implementasyonu aynı `Trip` şemasını üretmeli (fixture/fuzz).
- **Entegrasyon:** Küçük bir pilot alt-bbox üzerinde uçtan uca pipeline egzozu (fixture).
- **FastAPI testleri:** Mock ısı katmanı + deterministik frontier; `/metrics` hesap tutarlılığı (aynı girdi → aynı rapor).
- **Mocking:** GEE/MODIS/İBB erişimi sahte (mock) servislerle; gerçek API çağrıları yalnızca opt-in integration testlerinde (ağ/ücret riski).
- **Çerçeve:** Arka uç `pytest`, kapalı (deterministik) rastgele tohumlar; ön yüz opsiyonel component testi.

---

## 7. Rationale & Context

- **Karar 1 — Hibrit teslimat:** Yalnızca notebook sunumu yerine çalışan prototip, hipotezin pratik değerini jüriye somut gösterir. Maliyeti: ek sunum katmanı işi.
- **Karar 2 — Yarı-statik (near-real-time) rota:** Landsat ~16 günlük geçişi nedeniyle gerçek-zamanlı fizibil değil. Günlük/saatlik taze ısı katmanı + anlık rota sorgusu maliyet/fayda dengesini optimum verir. Downscaling yöntemi belgelendirilir.
- **Karar 3 — React + FastAPI (decoupled):** Ürün-benzeri his; arka uç analizi ön yüzden bağımsız gelişebilir. Maliyeti: ön yüz işi.
- **Karar 4 — Swappable `TripDataProvider`:** Gerçek Martı telematrisi elde-edilebilirliği belirsiz. Provider soyutlaması, varsayılan İBB + simülasyon ile başlayıp gerçek veri geldiğinde downstream'i değiştirmeden geçişi sağlar.
- **Karar 5 — Çift model (enerji + SoH):** Hipotez iki zaman ölçeğini (anlık deşarj + uzun-dönem yaşlanma) kapsıyor. Rota için enerji, şarj istasyonu/filo için SoH açıkça ayrıştırılır.
- **Karar 6 — Pareto frontier (tekil ağırlıklı rota yerine):** α/β ağırlıklarının seçimi özneldir. Frontier tüm ödünleşimleri sunar ve kullanıcı seçer — akademik olarak daha defansif, UX açısından zengin. **Önemli nüans:** Enerji = f(temperature) olduğundan ısıdan kaçınmak enerjiyi zaten azaltır; bu nedenle "termal maruziyet" ikinci amacı, yaşlanma-odaklı (SoH-relevant degree-hours) olarak ayrı tanımlanır — değilse iki amaç gereksiz yere örtüşürdü.
- **Karar 7 — SoH hibrit (public veri + literatür modeli):** Public aging verisi teorik taban, literatür Arrhenius-tipi modeli analitik iskele sağlar. Yalnızca biri olsaydı veri zayıf ya da model kalibre-edilemez kalırdı.
- **Karar 8 — Landsat ↔ MODIS füzyonu:** Saf Landsat çok seyrek (~16 gün), saf MODIS çok kaba (~1 km). Füzyon mekansal + temporal dengeyi verir; spatiotemporal fusion literatüründe yerleşik bir yaklaşımdır.
- **Karar 9 — Alt-koridor, 30 m:** Odaklı pilot ile düşük compute; 30 m hem sıcak-nokta hem rota için yeterli. Tüm ilçe kapsamı, genişletilebilirlik olarak ileride değerlendirilir.
- **Karar 10 — Üç-ayaklı sonuç paneli:** Enerji + SoH + metodolojik katkı birlikte. Tek metrik tezin katkısını daraltırdı.

---

## 8. Dependencies & External Integrations

- **EXT-001:** Google Earth Engine API (NASA Landsat 8/9 LST) — servis hesabı doğrulaması.
- **EXT-002:** MODIS günlük LST ürünü (füzyon temporal kaynağı). *Ürün adı/erişim yolu implementasyonda @librarian ile doğrulanacak.*
- **EXT-003:** İBB Açık Veri Portalı (mikromobilite yoğunluk / talep katmanları) — scraping/indirme.
- **EXT-004:** Literatür Li-ion 18650 termal deşarj veri kümeleri (enerji modeli; 25/35/45°C).
- **EXT-005:** Public Li-ion yaşlanma veri setleri (SoH modeli). *Aday setler (ör. Oxford / NASA PCoE / Sandia-sınıfı) implementasyonda @librarian ile doğrulanacak.*
- **EXT-006:** Literatür Arrhenius-tipi kapasite-kaybı formülasyonu (SoH kalibrasyon iskeleti).
- **EXT-007:** Spatiotemporal fusion algoritması (STARFM / ESTARFM-sınıfı). *Tam imza/kütüphane implementasyonda @librarian ile doğrulanacak.*
- **EXT-008:** Yol ağı + eğim verisi (OSMnx / rakım DEM).
- **EXT-009 (opsiyonel):** Gerçek operatör telematrisi (Martı vb.) — `TripDataProvider` implementasyonu olarak eklenebilir.
- **EXT-010:** Ön yüz harita katmanı sağlayıcısı (Leaflet + Mapbox/OSM tile).

---

## 9. Examples & Edge Cases

- **Tek rejim gecesi:** Tüm bölge tek sıcaklık rejimindeyse Gi* anlamlı küme üretmeyebilir; modül bunu raporlamalı (""no significant clusters").
- **A→B tek yol:** Frontier tek noktaya çöker; dashboard bunu açıkça göstermeli ("no alternative route").
- **Isı katmanı stale/eksik:** Rota motoru son geçerli snapshot'ı kullanmalı ve UI'da "stale data" uyarısı göstermeli.
- **MODIS bulutlu / gözlem eksik gün:** Füzyon temporal enterpolasyon fallback'ine düşmeli; çıktıya QC işareti konmalı.
- **Aging verisi eksik sıcaklık noktası:** Arrhenius-tipi ekstrapolasyon uygulanmalı ve sonuç belirsizlik işaretiyle işaretlenmeli.
- **Provider değişince sefer yoğunluğu sıfır bölge:** Simülasyon/enterpolasyon ile doldurulmalı; sıfır-talep sessiz hatasına düşülmemeli.
- **Çok seyrek (>2 nokta eksik) veya çok yoğun (>50) frontier:** UI'da frontier sınırlandırma / cluster uygulanmalı.
- **Örnek nicel karşılaştırma:** 45°C rejiminde enerji tüketimi ve SoH kaybının, 25°C taban çizgisine göre yüzdesel artışı (panelde gösterilecek temel sonuç).

---

## 10. Validation Criteria

- **İstatistiksel geçerlilik:** Moran's I / Gi* için p-değeri + Monte Carlo sahte-dağılım testi.
- **Enerji modeli:** Hold-out sıcaklık rejimlerinde RMSE / MAPE.
- **SoH modeli:** Aging verisinde capacity-retention tahmin hatası + kalibrasyon parametre güven aralığı.
- **Fusion doğruluğu:** Referans (iki sensörün de gözlem yaptığı) günlerde fused vs gözlemlenen LST hatası (AC-009).
- **Pareto doğruluğu:** Dominasyon yok kontrolü (AC-008) + küçük örneklemde brute-force global frontier ile karşılaştırma.
- **Provider taşınabilirliği:** Provider değişince çıktı şema/tutarlılık kontrolü.
- **Hassasiyet analizi:** LST çözünürlüğü / downscaling varsayımının sonuçlara etkisi.
- **Operasyonel:** Rota sorgu gecikmesi (≥ saniyeler içinde) + ısı katmanı tazelik kontrolü.
- **Başarı metrikleri:** Enerji tasarrufu % (termal-duyarlı rota vs en-kısa-yol), SoH iyileşmesi % (optimize vs baseline şarj istasyonu yerleşimi), metodolojik katkı açıklaması — panelde birlikte (AC-010).

---

## 11. Related Specifications / Further Reading

**Türetilecek alt dokümanlar (implementasyon sırasında):**
- Veri Sözlüğü (tüm `*Grid` / `Snapshot` şemaları için ayrıntılı alan dokümantasyonu).
- GEE / MODIS Füzyon Entegrasyon Rehberi.
- Pareto Rota Algoritması Tasarım Dokümanı (amaç fonksiyonu, NSGA-II/pymoo konfigürasyonu).
- Dashboard / API Tasarım Dokümanı (uç nokta şemaları + ön yüz bileşenleri).
- `TripDataProvider` Kontrat Dokümanı.
- SoH Model Dokümanı (Arrhenius-tipi kalibrasyon prosedürü).
- Sonuç Paneli Tasarımı (üç metrik görselleştirme).

**Doğrulanacak harici referanslar (@librarian):**
- MODIS LST ürün adı / erişim yolu.
- STARFM / ESTARFM fusion kütüphaneleri (Python mevcudiyeti, imza).
- Public Li-ion aging datasetleri (Oxford / NASA PCoE / Sandia-sınıfı — erişilebilirlik ve lisans).
- PySAL, OSMnx, NSGA-II / pymoo güncel dokümantasyonu.
- IEEE EV batarya termal yaşlanma literatürü (kalibrasyon referansları).

---

## Appendix A — Karar Günlüğü (Q&A Özeti)

| # | Karar | Sonuç |
|---|---|---|
| 1 | Teslimat tipi | Hibrit: araştırma analizleri + çalışan prototip |
| 2 | Rota modu | Yarı-statik (near-real-time): saatlik ısı katmanı, anlık sorgu |
| 3 | Dashboard stack | React + Leaflet/Mapbox ön yüz + Python FastAPI arka uç |
| 4 | Sefer verisi | İBB + simülasyon (varsayılan), swappable provider ile Martı telematrisi sonra |
| 5 | Batarya model ölçeği | İkisi birden: enerji (rota) + SoH (şarj istasyonu/filo) |
| 6 | Rota amaç fonksiyonu | Pareto çok-amaçlı (enerji vs uzunluk/termal maruziyet) — frontier |
| 7 | SoH veri kaynağı | Hibrit: public aging veri seti + literatür modeli kalibrasyonu |
| 8 | LST downscaling | Landsat ↔ MODIS spatiotemporal fusion |
| 9 | Kapsam / çözünürlük | Kadıköy alt-koridor (Moda–Kozyatağı, ~5–10 km²), 30 m |
| 10 | Başarı metrikleri | Hepsi: enerji % + SoH % + metodolojik katkı |
