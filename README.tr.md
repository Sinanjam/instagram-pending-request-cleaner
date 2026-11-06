# Instagram Bekleyen İstek Temizleyici

**Türkçe** | [English](README.md)

Hiç yazılım bilmeyen biri için anlatım: Instagram'da daha önce insanlara **takip isteği gönderdin**. Bazıları hâlâ **Requested / İstek Gönderildi** durumunda. Bu program **kendi Instagram veri arşivini** okuyarak bu istekleri inceler. Sen açıkça onay verirsen, yalnız güvenle doğrulayabildiği istekleri iptal etmeyi dener.

> [!WARNING]
> **Resmî Instagram uygulaması değildir.** Meta/Instagram ile bağlantısı yoktur. Instagram arayüzünü değiştirirse program bozulabilir ve otomasyon hesap kısıtlamasına neden olabilir. CAPTCHA, checkpoint, güvenlik kontrolü ve işlem engelleri aşılmaz. Program **emin olamadığı yerde durur**.

## En baştan: Neye ihtiyacımız var?

Bu işi **5 küçük adıma** bölelim:

1. Grafik arayüzlü **Linux** ve **flake özelliği açık Nix**.
2. Instagram'dan indireceğin **kendi JSON veri arşivin**.
3. Programa özel **ayrı Firefox profili**; Instagram'a orada kendin giriş yap.
4. Önce `--dry-run` ile **sadece bak**, `--status` ile **sadece say**.
5. Gerçekten iptal yapmak istiyorsan `--apply --limit 3` ile **küçük deneme**.

**Önce şu kelimeleri öğren:**

| Kelime | Çok basit anlamı |
| --- | --- |
| Pending / Requested | Takip isteği gönderdin, karşı taraf henüz kabul etmedi. |
| İptal / withdraw | **Kendi gönderdiğin** bekleyen isteği geri çekmek. |
| Deferred / ertelenen | Program hesabın durumundan emin olamadı; **iptal edildi demek değil**. |
| Dry run | Prova: Instagram'da hiçbir değişiklik yok. |
| Profil | Burada, Firefox'un giriş bilgilerini tuttuğu **ayrı bir klasör**. |
| Parametre | Komuta eklenen talimat: örneğin `--status` = “durumu göster”. |

## Adım 1 — Proje klasörüne gir

Örneğimizde proje `~/Projects/instagram-pending-request-cleaner` içinde. Senin klasörün farklıysa **`flake.nix` dosyasının bulunduğu yere git**.

```fish
cd ~/Projects/instagram-pending-request-cleaner
nix run . -- --help
```

**Bu komuttaki her parça ne yapıyor?**

- `nix run` = “Nix, uygulamayı aç.”
- `.` = “Şu an içinde olduğum klasördeki projeyi kullan.”
- Ortadaki tek başına `--` = “Bundan sonrakiler Nix'in değil, programın seçenekleri.”
- `--help` = “Bana kullanım kılavuzunu göster.”

Projeyi henüz indirmediysen, GitHub deposuna erişimin varken:

```fish
git clone https://github.com/OWNER/instagram-pending-request-cleaner.git
cd instagram-pending-request-cleaner
```

`OWNER` yerine GitHub depo sahibini yaz. Depo private ise erişim izni gerekir. İlk çalıştırmada Nix bazı dosyaları indirip derleyebilir.

**Desteklenen sistemler:** Grafik arayüzlü Linux; `x86_64-linux` test edildi. `aarch64-linux` tanımlı ama gerçek ARM cihazda doğrulanmadı. Firefox ile Wayland/X11 desteklenir; Hyprland şart değil. Windows/macOS sürümleri paketlenmedi/test edilmedi. Global `pip install` gerekmez.

## Adım 2 — Instagram veri arşivini indir

Instagram'da **Hesaplar Merkezi → Bilgilerini indir** akışından takipçiler/takip edilenler bağlantı bilgilerini **JSON** biçiminde iste. **HTML seçme.** Instagram menü isimlerini değiştirebilir.

Program şu üç şeyden **birini** okur:

- İçinde tam bir tane `pending_follow_requests.json` bulunan ZIP arşivi.
- Doğrudan `pending_follow_requests.json` dosyası.
- İçinde tam bir tane böyle JSON bulunan, ZIP'ten çıkarılmış klasör.

**Gerçek arşivi GitHub'a koyma!** Kullanıcı adları içeriyor; kendi bilgisayarında özel sakla.

Aşağıda dosyanın adını **örnek olarak** `instagram-export.zip` kabul edeceğiz. Sen kendi dosya adını kullan.

## Adım 3 — Fish'e arşivin yerini ve kullanıcı adını söyle

**Fish shell kullanıyorsan** sırayla yaz:

```fish
set archive "$HOME/Downloads/instagram-export.zip"
set account example_owner
ls -lh "$archive"
```

**Tane tane açıklama:**

1. `set archive ...`: Arşivin uzun yoluna `$archive` diye kısa isim veriyoruz.
2. `set account example_owner`: `example_owner` yerine **kendi Instagram kullanıcı adını** yaz. Başına `@` koyma. Artık `$account` onu temsil edecek.
3. `ls -lh "$archive"`: “Bu dosya gerçekten var mı?” diye kontrol ediyor. “No such file” görürsen adresi düzelt.

**Çok sık yapılan hata:** `set --archive ...` **YANLIŞ**. Fish'te `set archive ...` yazılır. `--archive` birazdan **programın komutunda** kullanılacak.

**Bash/Zsh kullanıyorsan:**

```bash
archive="$HOME/Downloads/instagram-export.zip"
account="example_owner"
ls -lh "$archive"
```

Değişkenleri bu şekilde tanımladıktan sonra aşağıdaki `"$archive"` ve `"$account"` örnekleri iki kabukta da kullanılabilir.

## Adım 4 — Ayrı Firefox aç, Instagram'a giriş yap

**Neden ayrı Firefox?** Program kendi tarayıcı penceresinde çalışsın; günlük Firefox oturumun etkilenmesin. Şifreni terminale yazmıyorsun; Instagram'a **kendin giriş yapıyorsun**.

**Fish ile birebir çalışan kurulum:**

```fish
set state_root "$HOME/.local/state"
if set -q XDG_STATE_HOME
    set state_root "$XDG_STATE_HOME"
end
set state_dir "$state_root/instagram-request-cleaner"
mkdir -p "$state_dir/firefox-profile"
nix develop --command firefox --no-remote --profile "$state_dir/firefox-profile"
```

**Bu satırların görevi:**

- `state_dir` = programın dosyalarının tutulacağı klasör.
- `mkdir -p` = klasör yoksa oluştur.
- Son satır = programa özel Firefox'u aç.

Firefox açılınca **kendi Instagram hesabına giriş yap**, doğru hesap olduğunu kontrol et ve **bu ayrı Firefox'u tamamen kapat**. Oturum kapanmadıkça her seferinde yeniden giriş gerekmeyebilir.

**Bash/Zsh için aynı iş:**

```bash
state_root="$HOME/.local/state"
if [ -n "$XDG_STATE_HOME" ]; then state_root="$XDG_STATE_HOME"; fi
state_dir="$state_root/instagram-request-cleaner"
mkdir -p "$state_dir/firefox-profile"
nix develop --command firefox --no-remote --profile "$state_dir/firefox-profile"
```

Günlük kullandığın Firefox profilini `--profile-dir` olarak seçme. GTK'dan gelen `nw-resize` gibi imleç uyarıları, Firefox normal çalışıyorsa genellikle önemsiz tema uyarılarıdır.

## Adım 5 — Önce hiçbir şeyi değiştirmeden kontrol et

**Arşivi oku, Instagram'a dokunma:**

```fish
nix run . -- --archive "$archive" --dry-run
```

Program arşivi inceler; en fazla 30 kullanıcı adını ve kalanların sayısını gösterir. **Firefox açılmaz, hiçbir takip düğmesine basılmaz.**

**Kaç hesap işlenmiş, bak:**

```fish
nix run . -- --archive "$archive" --status
```

İptal edilen, artık beklemeyen, ertelenen ve sıradaki hesap sayılarını görürsün. Bu işlem de **Firefox'u açmaz**.

> [!IMPORTANT]
> **Eski sürümle daha önce istek iptal ettiysen**, yeni sürümün kayıt klasörü farklı olabilir. Eski iptalleri bilmeyen programa aynı eski ZIP ile gözü kapalı `--apply` yaptırma. Önce `--status` kullan, mümkünse Instagram'dan **yeni arşiv** indir.

## Adım 6 — Tek bir hesabın durumunu kontrol et (tıklama yok)

```fish
nix run . -- --archive "$archive" --account "$account" --inspect-user example_user_1
```

`example_user_1` yerine **arşivde bulunan** bir hesabın adını yaz. Program önce senin oturumunu doğrular, sonra o kişinin durumunu okur. **Follow/Unfollow düğmesine basmaz.**

## Adım 7 — Gerçek iptal: ilk sefer sadece 3 hesap!

**Bu komut gerçek takip isteklerini geri çekebilir.** Hazır değilsen çalıştırma:

```fish
nix run . -- --archive "$archive" --account "$account" --apply --limit 3
```

Normalde terminalde **`APPLY`** yazman istenir. Tam olarak büyük harfle yazınca gerçek işlem için onay vermiş olursun.

**Program ne yapıyor?**

1. Instagram'da giriş yapılan hesap, `--account` ile eşleşiyor mu kontrol eder.
2. Hedef gerçekten **Requested** mı bakar.
3. Açılan onay penceresinin **o hedefin isteğini geri çekmekten** söz ettiğini doğrular.
4. Yalnız güvenli biçimde doğruladığı onay düğmesine Selenium ile basar.
5. İşlem sonrası **Follow** durumunu doğrular.
6. Bir şey belirsizse **durur**, kafasına göre tıklamaz.

`--limit 3` = “en fazla 3 hesabı **denemeye al**”; kesin 3 iptal garantisi değil. Daha sonra **aynı arşiv ve kayıt klasörüyle** devam edebilirsin. Tamamlananlar tekrar işlenmez.

**Güvenlik nedeniyle durursa hemen tekrar çalıştırma.** Önce nedenini incele.

## Adım 8 — Ertelenen (deferred) hesaplar nasıl incelenir?

Ertelenen = programın güvenli şekilde çözemediği hesap. **İptal edilmiş sayılmaz.**

**Henüz incelenmemiş ertelenenlerden en fazla 5 tanesine bak:**

```fish
nix run . -- --archive "$archive" --account "$account" --deferred-review --limit 5
```

Bu işlem Instagram'daki takip durumunu **değiştirmez**, fakat sonuçları **yerel inceleme kaydına yazar**.

**İnceleme raporlarını oluştur:**

```fish
nix run . -- --archive "$archive" --deferred-report
```

**Daha önce `active_requested` olarak güvenle doğrulanmış ertelenenlerde küçük gerçek iptal grubu:**

```fish
nix run . -- --archive "$archive" --account "$account" --apply-deferred --limit 3
```

Bu komut **“ertelenenlerin hepsini zorla sil”** değildir. Durumu tekrar doğrular, belirsizlikte durur. Güvenlik kontrolünü gevşetme.

## BÜTÜN PARAMETRELER — hiç bilmeyen için sözlük

**Parametre**, programa verdiğin kısa talimat. Örneğin `--limit 3` = “Bu çalışmada en fazla 3 hesabı dene.”

Aşağıdaki tabloda son sütun **tam komut değil**, `nix run . --` sonuna eklenecek parçadır. Normal kullanımda `--archive "$archive"` da gerekir. Firefox açan modlarda ayrıca `--account "$account"` gerekir. Bazı seçenekler aynı anda kullanılamaz.

### Her gün kullanacağın kolay seçenekler

| Parametre | Basit Türkçesi | Örnek parça |
| --- | --- | --- |
| `-h` / `--help` | “Bana kullanım bilgisini göster.” | `--help` |
| `--archive YOL` | “Instagram ZIP/JSON dosyam burada.” Normal kullanımda zorunlu. | `--archive "$archive"` |
| `--account AD` | “Tarayıcıda giriş yapmış olması gereken hesap bu.” Firefox gereken modlarda zorunlu. | `--account "$account"` |
| `--dry-run` | “**Sadece oku**, Instagram'da işlem yapma.” | `--dry-run` |
| `--status` | “Kaç hesap bitti, say.” Firefox açılmaz. | `--status` |
| `--apply` | “Gerçekten bekleyen istekleri iptal etmeyi dene.” Genellikle `APPLY` onayı ister. | `--apply` |
| `--limit SAYI` | “Bu tur en fazla şu kadar hesap dene.” `0` = **sınırsız**; ilk sefer 1–3 seç. | `--limit 3` |
| `--state-dir YOL` | “İlerleme, log, rapor ve varsayılan Firefox profili bu klasörde olsun.” | `--state-dir "$HOME/cleaner-state"` |
| `--profile-dir YOL` | “Programın **ayrı** Firefox profilini buradan aç.” Kişisel profili kullanma. | `--profile-dir "$HOME/cleaner-profile"` |

### Hesap kontrolü ve ertelenenler

| Parametre | Basit Türkçesi | Örnek parça |
| --- | --- | --- |
| `--inspect-user AD` | “Arşivimdeki şu hesaba **sadece bak**, takip düğmesine basma.” | `--inspect-user example_user_1` |
| `--diagnose` | “Okuyamadığın arşiv kayıtlarının **biçimini** anlat.” Gerçek bilgileri gizler; arşivi düzeltmez. | `--diagnose` |
| `--defer-user AD` | “Bu arşiv hesabını şimdilik kenara ayır.” Instagram açılmaz, fakat **yerel erteleme kaydı değişir**. | `--defer-user example_user_1` |
| `--deferred-review` | “Henüz incelenmemiş ertelenenleri **salt okunur** kontrol edip sonucu kaydet.” | `--deferred-review --limit 5` |
| `--deferred-report` | “Kaydedilmiş incelemelerden JSON, CSV ve Markdown raporları oluştur.” Firefox açılmaz. | `--deferred-report` |
| `--apply-deferred` | “Önceden `active_requested` diye doğrulanan ertelenenler için gerçek iptal dene.” Her şey yeniden kontrol edilir. | `--apply-deferred --limit 3` |
| `--deferred-allowlist YOL` | “`--apply-deferred` adaylarını, arşiv özetiyle eşleşen doğrulanmış JSON listesiyle **daralt**.” Yeni hesap ekleyemez. | `--deferred-allowlist /path/to/allowlist.json` |
| `--reconcile-user AD` | “Bir erteleneni **tıklamadan** tekrar kontrol et; kesin bekleyen istek yoksa yerel kaydı düzelt.” | `--reconcile-user example_user_1` |
| `--retry-deferred` | “Ertelenenleri eski `--apply` yöntemiyle dene.” **İleri seviye**; önce inceleme + `--apply-deferred` tercih et. | `--apply --retry-deferred --limit 1` |

### İleri seviye: bekleme, durdurma, otomasyon

| Parametre | Basit Türkçesi | Örnek parça |
| --- | --- | --- |
| `--defer-unverified` | `--apply` sırasında bazı belirsiz/erişilemeyenleri **iptal saymadan erteleme kaydına al**. Riskli durum yine durdurur. | `--apply --defer-unverified` |
| `--min-delay SANİYE` | Hesaplar arasında **en az** kaç saniye beklensin? En az `5`, varsayılan `8`. | `--min-delay 8` |
| `--max-delay SANİYE` | **En çok** kaç saniye beklensin? Varsayılan `15`; minimumdan az olamaz. | `--max-delay 15` |
| `--non-interactive` | **İleri seviye gözetimsiz mod.** İzin verilen işlerde `APPLY` yazma sorusunu atlar. Önceden yetki vermeden kullanma. | `--apply --defer-unverified --non-interactive` |
| `--deadline TARİH` | “Bu gelecek tarih/saatten sonra yeni hesaba başlama.” **Saat dilimiyle** yaz. Eski `next-istanbul-0800` seçeneği de var. | `--deadline "2030-01-01T08:00:00+03:00"` |

**Yanlış seçenek birleşimleri:**

- `--apply` ve `--dry-run` **birlikte olmaz**.
- `--status` diğer işlem/inceleme modlarıyla kullanılmaz.
- `--deferred-review`, `--deferred-report`, `--apply-deferred` üç **ayrı moddur**.
- `--deferred-allowlist` yalnız `--apply-deferred` ile çalışır.
- `--retry-deferred` ve `--defer-unverified`, `--apply` gerektirir.
- `--non-interactive` tüm güvenlik kurallarını kaldıran seçenek **değildir**.
- `--limit 0` = **bütün adaylar**. İlk gerçek denemede kullanma.

## LOG komutları: İşlemler nereden izlenir?

İki şey var: **hızlı sayılar** ve **kaydedilmiş işlemlerin günlüğü**.

**1. Kaç istek iptal edildi, kaç kaldı?**

```fish
nix run . -- --archive "$archive" --status
```

**2. Son 30 işlem kaydını göster:** (Adım 4'teki `state_dir` değişkenini kullan)

```fish
tail -n 30 "$state_dir/actions.jsonl"
```

**3. Yeni kayıt geldikçe canlı izle:**

```fish
tail -f "$state_dir/actions.jsonl"
```

Log izlemeyi kesmek için **Ctrl+C**. Bu, **başka terminalde çalışan** temizleyiciyi durdurmaz. Henüz olay kaydı yoksa dosya da oluşmamış olabilir.

**4. Salt okunur önizlemenin ekran çıktısını aynı zamanda dosyaya yaz:**

```fish
nix run . -- --archive "$archive" --dry-run 2>&1 | tee "$state_dir/preview.log"
```

**Dikkat:** Loglar, raporlar ve önizlemeler gerçek kullanıcı adları içerebilir. GitHub'a yükleme, sansürsüz paylaşma.

Varsayılan kayıt dizini: `~/.local/state/instagram-request-cleaner`. Farklı `--state-dir` seçtiysen logları da orada ara.

| Dosya | Ne saklıyor? |
| --- | --- |
| `progress.json` | Tamamlanan hesaplar. |
| `deferred.json` | Ertelenen, henüz kesin sonuçlanmayan hesaplar. |
| `actions.jsonl` | İşlem günlüğü; her satır bir JSON kaydı. |
| `deferred-review.json` | Ertelenen inceleme kayıtları. |
| `deferred-review-report.csv` / `.md` | İnsan tarafından okunabilen raporlar. |
| `firefox-profile/` | **Gizli** Firefox giriş oturumu. |
| `last-modal-diagnostic.json` ve benzerleri | Sorunlu onay penceresi tanıları; kişisel bilgi içerebilir. |

## Hata gördüm, ne yapayım?

| Hata | Kolay çözüm |
| --- | --- |
| Fish: `set: --archive: unknown option` | `set --archive` değil, `set archive` yaz. |
| Fish'te Bash değişken sözdizimi hatası | Adım 4'ün **Fish** komutlarını kullan. |
| “No such file” | `ls -lh "$archive"` ile dosya yolunu kontrol et. |
| Terminal `APPLY` istiyor | Gerçek işlem başlayabilir. İstemiyorsan onaylama. |
| Yanlış hesap / kimlik doğrulama hatası | Özel Firefox'u açıp doğru hesaba giriş yap, tamamen kapat. |
| Marionette / geckodriver sorunu | Yalnız programın Firefox'unu kapat; kişisel Firefox'a dokunma. |
| Pencere gizliyken takılıyor | Hyprland'da pasif workspace render ayarını düzelt veya pencereyi görünür yap. |
| Belirsiz “Unfollow” penceresi | **Dur!** Kesin iptal açıklaması yoksa düğmeye basma. |
| CAPTCHA / checkpoint / challenge / engel | Otomasyonu durdur; Instagram'dan manuel hallet. |
| “Page isn't available” | Hesap erişilemiyor; **silinmiş/dondurulmuş olduğu kanıtlanmaz**. |
| GTK `nw-resize` uyarısı | Firefox çalışıyorsa genellikle imleç teması uyarısıdır. |

**Çıkış kodları:** `0` başarılı; `1` geçici/işletim hatası; `2` veri/ayar hatası; `3` **güvenlik duruşu**. **Kod 3 sonrası otomatik yeniden başlatma!** Önce logları incele.

## Hyprland kullanıyorsan (zorunlu değil)

Uygulamanın Firefox'u özel `ig-request-cleaner-firefox` sınıfını kullanır. [examples/hyprland.conf](examples/hyprland.conf) örneğini kendine uyarlayabilirsin. Workspace `9` zorunlu değildir. `render_unfocused` arka planda görünmeyen pencerenin çizimini korumaya yardımcı olur. Normal `firefox` sınıfına genel kural koyma; kişisel Firefox'un da etkilenir.

## Geliştirici komutları (meraklısına)

`flake.nix` dosyasının bulunduğu klasörde:

```fish
nix build
nix flake check
nix develop --command python -m unittest discover -s tests -v
nix develop --command python scripts/privacy_scan.py
nix develop --command gitleaks dir . --redact
nix develop --command gitleaks git . --redact --log-opts="--all"
```

- `nix build` = uygulamayı paketle.
- `nix flake check` = Nix kontrollerini/testleri çalıştır.
- `nix develop` = geliştirme araçlarını hazırla.
- Gizlilik taraması/Gitleaks = yanlışlıkla özel dosya yayımlanmasını önlemeye yardımcı olur.

GitHub Actions testleri **gerçek Instagram hesabında değil**, sentetik verilerle çalışır. Ayrıca [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [MIT Lisansı](LICENSE) dosyalarına bak.

## Sınırlamalar (lütfen oku)

Program, desteklenen resmî Instagram API'siyle değil, Instagram **web arayüzüyle** çalışır. Instagram görünümü/metinleri değişebilir, otomasyonu yavaşlatabilir veya kısıtlayabilir. Program şüpheli işlemleri bilerek reddeder; **her isteği iptal edeceğini garanti edemez**. Gerçek arşivin, logların ve Firefox oturumun **özel kalmalı**.

## Vibe coding bilgilendirmesi

**Bu projenin tamamı vibe coding ile geliştirilmiştir.** Kodlar, testler ve belgeler yalnızca geleneksel elle yazılmış kodlama süreciyle değil, yapay zekâ araçlarına verilen ve tekrar tekrar iyileştirilen komutlarla oluşturulmuştur. Testlerin geçmesi **bağımsız güvenlik denetimi yapıldığı** veya yazılımın kusursuz olduğu anlamına gelmez. Kodu incele, yedek al, küçük gruplarla dene ve kullanırken riskleri değerlendir.
