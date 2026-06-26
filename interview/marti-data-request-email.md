# Martı Veri İstek E-postası

## Konu satırı önerileri (birini seçin)

1. **Kentsel Isı Adaları × E-Scooter Batarya Sağlığı — Portföy Projesi Veri İş Birliği**
2. **Martı Filo Verisiyle Güçlendirilebilecek Bir Isı–Batarya İçgörü Çalışması**
3. **Kadıköy Koridoru Termal Analiz — Düşük Eforlu Veri İş Birliği Teklifi**

---

## E-posta gövdesi

**Kime:** [Martı kişi adı] — Veri Bilimi Yöneticisi, Martı
**Tarih:** [tarih]

Sayın [Martı kişi adı],

Ben [İsim Soyisim]. Bağımsız bir portföy/ar-ge projesi olarak, **İstanbul'da kentsel ısı adalarının (UHI) elektrikli scooter batarya deşarjı ve uzun-dönem batarya sağlığı (SoH) üzerindeki etkisini** ölçen, termal-duyarlı bir rota optimizasyon prototipi geliştiriyorum. Martı'ya ulaşmamın nedeni, bu çalışmanın gerçek filo verisiyle doğrulanmasının, projenin inandırıcılığını ve üretebileceği içgörünün kalitesini somut biçimde yükseltecek olması.

**Projeden kısaca.** Kadıköy koridorunu (Moda–Kozyatağı) pilot bölge olarak seçtim; Landsat↔MODIS füzyonuyla 30 m / saatlik çözünürlükte yüzey sıcaklığı katmanı üretiyorum. Bu katman üzerinde iki model kuruyorum: (i) anlık enerji tüketimi (`Wh/km = f(sıcaklık, eğim, hız)`) ve (ii) Arrhenius-tipi SoH yaşlanma modeli (public aging verisetleriyle kalibre). Rota motoru, enerji ile termal-maruziyet arasında bir Pareto frontier döndürüyor — kullanıcı ödünleşimi kendisi seçiyor.

**Veri neden önemli.** Model kalibrasyonunu public hücre verisi ve literatürle yapıyorum; ancak gerçek sefer/enerji verisi olmadan doğrulama tamamen simülasyona dayanıyor. İBB açık verisi + simülasyonla çalışan bir fallback var — proje yürüyor. Fakat Martı'dan gelecek agregat bir sefer/enerji verisi, bu "lab → gerçek filo" boşluğunu tek hamlede kapatır ve üretilen içgörüyü Martı'nın kendi operasyonel kararlarında kullanılabilir kılacak güvenilirliğe taşır.

**Veri talebi — minimum → opsiyonel hiyerarşisi.** Paylaşımı olabildiğince düşük eforlu ve düşük riskli tutmak için hiyerarşi kuruyorum; **birinci seviye tek başına yeterli**:

1. **Öncelikli (bu tek başına yeterli):** Anonimleştirilmiş OD (origin–destination) matrisi — sefer başlangıç/bitiş konumu (h3 s9/s10 veya grid hücresi agregatı), zaman damgası (gün + saatlik), tahmini mesafe. Kişi/araç kimliği yok.
2. **Rahatlık varsa — orta seviye:** Map-matched OSM edge sequence veya ~50 m seyreklikli path, mesafe, ortalama hız profili, toplam sefer süresi. Araç ID hash'li, kullanıcıdan ayrıştırılmış.
3. **Elinizde hazır varsa — ideal:** Araç bazlı agregat enerji tüketimi (Wh/sefer veya kWh/km) + varsa SoC başlangıç/bitiş + şarj event'leri (süre/güç, hash'li). BMS ham verisi gerekmiyor.

**Gizlilik ve uyum.** PII (kullanıcı kimliği, plaka, ödeme) istemiyorum. Araç ID'leri hash olarak yeterli; istenirse Martı tarafı k-anonimlik (k ≥ 50) veya soyut agregat verebilir. Veri paylaşım sözleşmesi (DPA), NDA ve yalnızca-bu-portföy-projesi-kullanım şartlarını imzalamaya hazırım. Ham veri repo'ya işlenmez; KVKK/GDPR uyumlu bir protokol belirlemekten yanayım; proje sonunda veri ya anonimleştirilir ya da imha edilir.

**Martı'ya geri değer.** Bu tek yönlü bir talep değil:
- **Nicel içgörü.** "Yaz aylarında Kadıköy sıcak noktalarında SoH kaybı X% daha hızlı" gibi, filo rotasyon/şarj operasyonlarını doğrudan bilgilendirebilecek bir ısı-batarya quant'ı.
- **Metodoloji aktarımı.** Termal-duyarlı Pareto rota çerçevesi ve SoH-duyarlı şarj yerleşim modeli — filo planlama ekibinize referans olarak paylaşılabilir.
- **Sonuç paylaşımı.** Çalışma tamamlandığında bulgular Martı ile paylaşılır; uygun görülürse iç sunum veya teknik özet olarak da sunulabilir.

**Dürüst not.** Herhangi bir veri sağlanamazsa, çalışmayı İBB açık verisi ve simülasyonla tamamlamayı planlıyorum — proje yürür. Ancak bu durumda modelin gerçek filoda doğrulanması açığı kalır ve üretilen içgörü "temkinli yorum" düzeyinde kalır. Martı verisi bu farkı tek hamlede kapatır.

**Sonraki adım.** Zamanınızı korumak için canlı görüşme talep etmiyorum — süreci tamamen e-posta üzerinden yürütmek benim için tercih. (i) Hangi veri seviyesinin Martı için rahat paylaşılabilir olduğu, (ii) agregasyon/anonimleşme protokolü, (iii) zaman aralığı (yaz dönemi en değerli) konularında bu e-postaya yanıt vermeniz yeterli. Eğer bu talep doğrudan sizi ilgilendirmiyorsa, ilgili kişilere yönlendirmeniz de benim için yeterlidir.

İlginiz ve vaktiniz için teşekkür ederim.

Saygılarımla,

**[İsim Soyisim]**
E-posta: [e-posta]
Telefon: [telefon]
LinkedIn / GitHub: [opsiyonel]
