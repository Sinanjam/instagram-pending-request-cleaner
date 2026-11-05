# Instagram Bekleyen İstek Temizleyici

Kendi Instagram veri arşivinizdeki **gönderilmiş, bekleyen** takip isteklerini
yerel Selenium/Firefox ile inceleyen ve açık onayla geri çeken, güvenli duruş
odaklı bir araçtır.

> [!WARNING]
> Bu proje Instagram veya Meta ile bağlantılı, sponsorlu ya da onaylanmış
> değildir. Web arayüzü değişebilir ve otomasyon hesap kısıtlamasına yol
> açabilir. CAPTCHA, checkpoint, challenge veya işlem engelleri aşılmaz.

## Güvenlik yaklaşımı

- Varsayılan davranış salt okunur önizlemedir; değişiklik için `--apply` ve
  terminalde açık `APPLY` onayı gerekir.
- Aktif hesap, `--account` ile profil URL'si, başlık ve yalnız hesap sahibinde
  bulunan Profili Düzenle kontrolü üzerinden doğrulanır.
- Hedef işlemden hemen önce yeniden `Requested` olmalıdır.
- Dialog hedef kullanıcıya özgü istek geri çekme açıklamasını, görünür tek bir
  onay düğmesini ve Cancel eşini içermedikçe tıklanmaz.
- `Follow` hiçbir zaman tıklanmaz; `Following` hesaplar takipten çıkarılmaz.
- Tıklamalar yalnız Selenium WebElement `.click()` ile yapılır.
- Belirsizlik, kimlik kaybı, görünmez/örtülü düğme, eksik render veya Instagram
  güvenlik uyarısı bütün çalışmayı durdurur.
- Telemetri, analitik, harici AI veya sunucuya veri aktarımı yoktur.

## Desteklenen sistemler

Nix flake Linux `x86_64` ve `aarch64` paketleri tanımlar. Firefox/geckodriver
aynı kilitli Nixpkgs sürümünden gelir. Tam derleme ve test paketi
`x86_64-linux` üzerinde çalıştırılmıştır; `aarch64-linux` tanımlı fakat yerel
donanımda henüz doğrulanmamıştır. Wayland ve X11 kullanılabilir; Hyprland
isteğe bağlıdır. macOS ve Windows paketlenmemiş ve test edilmemiştir.

## Nix kurulumu

```console
nix run . -- --help
nix build
nix flake check
nix develop
nix profile install .
```

Global pip kurulumu gerekmez. Çalışma verileri Nix store'a değil
`$XDG_STATE_HOME/instagram-request-cleaner` dizinine yazılır.

## Veri arşivi ve Firefox profili

Instagram/Accounts Center içindeki resmi **Bilgilerini indir** akışından
followers/following bağlantı verisini JSON biçiminde alın. ZIP özel veridir;
Git'e eklemeyin. Araç ZIP'i, doğrudan JSON'u veya açılmış arşiv klasörünü kabul
eder.

Özel Firefox profilini ilk kez elle açıp giriş yapın:

```console
nix develop --command firefox --no-remote \
  --profile "${XDG_STATE_HOME:-$HOME/.local/state}/instagram-request-cleaner/firefox-profile"
```

Uygulama şifre almaz, girişi otomatikleştirmez ve kişisel Firefox profilinden
çerez kopyalamaz.

## Kullanım

Salt okunur önizleme ve durum:

```console
nix run . -- --archive /veri/export.zip --dry-run
nix run . -- --archive /veri/export.zip --status
```

Tek hesap kontrolü ve küçük gerçek grup:

```console
nix run . -- --archive /veri/export.zip \
  --account example_owner --inspect-user example_user_1
nix run . -- --archive /veri/export.zip \
  --account example_owner --apply --limit 3
```

Deferred inceleme, rapor ve yalnız doğrulanmış adayların uygulanması:

```console
nix run . -- --archive /veri/export.zip \
  --account example_owner --deferred-review --limit 10
nix run . -- --archive /veri/export.zip --deferred-report
nix run . -- --archive /veri/export.zip \
  --account example_owner --apply-deferred --limit 3
```

`--deferred-allowlist` yalnız güvenli aday kümesini daraltabilir; incelenmemiş
veya manuel dışlanmış hesabı ekleyemez.

## İlerleme, resume ve raporlar

Progress arşiv JSON'unun SHA-256 özetiyle bağlanır. Tamamlanan ve deferred
kayıtlar çakışamaz; değişiklik yapan çalışma öncesinde bütünlük doğrulanır.
Aynı arşiv tekrar çalıştırıldığında tamamlanan hesaplar atlanır. Farklı bir
arşiv için ayrı `--state-dir` kullanın.

Durum dizini kullanıcı adları içerebilen progress, deferred, audit, tanı,
rapor ve özel Firefox profilini barındırır. Bu dizini özel tutun ve yedekleyin.

## Hyprland

Selenium Firefox sınıfı `ig-request-cleaner-firefox` olarak ayrılır.
[`examples/hyprland.conf`](examples/hyprland.conf) örneğindeki workspace `9`
değerini değiştirin. `silent`, `no_initial_focus`, `focus_on_activate = false`
ve `render_unfocused = true` arka planda render ve odak güvenliği içindir.
Kişisel pencereleri etkileyen genel `class=firefox` kuralı eklemeyin.

## Sorun giderme ve sınırlamalar

- Hesap doğrulanmazsa özel profili elle açıp doğru hesapta Profili Düzenle
  kontrolünü doğrulayın.
- Marionette hatasında yalnız temizleyiciye ait Firefox'u kapatın; diğer Firefox
  süreçlerine dokunmayın.
- Görünmeyen veya belirsiz dialogu zorlamayın; durum dizinindeki tanıları okuyun.
- Challenge/CAPTCHA/checkpoint/action block durumunda otomasyonu durdurun.
- “Page isn't available” dondurulma, silinme veya ad değişikliği kanıtı değildir.

Instagram bu iş akışı için desteklenen resmi bir API sunmaz; arayüz değişiklikleri
aracı bozabilir. Tüm kararlar yerel çalışır, fakat audit ve raporlar kullanıcı adı
içerebilir. Ayrıntılar: [SECURITY.md](SECURITY.md).

## Geliştirme

```console
nix develop
python -m unittest discover -s tests -v
python scripts/privacy_scan.py
gitleaks detect --no-git --source . --redact
```

CI yalnız sentetik fixture kullanır. Katkılar güvenli duruş davranışını
gevşetmemelidir. Proje [MIT](LICENSE) lisanslıdır.
