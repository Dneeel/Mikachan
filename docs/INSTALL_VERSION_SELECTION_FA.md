# نصب نسخهٔ انتخابی و آزمایشی Mikachan

این قابلیت ابتدا **فقط در شاخهٔ `v2-test`** عرضه می‌شود. شاخهٔ اصلی `v2` تغییر نمی‌کند.

## نصب انتخابی (روی VPS تازه)

```bash
curl -fsSL https://raw.githubusercontent.com/Dneeel/Mikachan/v2-test/deploy/bootstrap.sh | sudo bash
```

اسکریپت فهرست واقعی شاخه‌های مجاز موجود در GitHub را می‌خواند و شمارهٔ نسخه را می‌پرسد. **هیچ نسخه‌ای از پیش انتخاب نشده است.** شاخه‌های مجاز `v1`, `v2`, `v3`, ... و `v2-test` هستند. برای لغو `q` وارد کنید.

## نصب مستقیم شاخهٔ تست (روی VPS تازه)

```bash
curl -fsSL https://raw.githubusercontent.com/Dneeel/Mikachan/v2-test/deploy/bootstrap-test.sh | sudo bash
```

این مسیر همیشه `v2-test` را انتخاب می‌کند، حتی اگر `--branch` دیگری به آن داده شود. اسکریپت به شاخهٔ اصلی `v2` نمی‌رود.

## انتخاب بدون منو و نصب Commit مشخص

```bash
curl -fsSL https://raw.githubusercontent.com/Dneeel/Mikachan/v2-test/deploy/bootstrap.sh | sudo bash -s -- --branch v2-test
```

برای نصب نسخه‌ای تاریخی و مشخص از همان شاخه، `--commit` را با SHA کامل ۴۰کاراکتری اضافه کنید. موتور مدیریت بررسی می‌کند که Commit واقعاً از اجداد شاخهٔ انتخابی باشد؛ هر SHA دلخواه پذیرفته نمی‌شود.

## نصب کد در برابر فعال‌سازی ربات

بدون تنظیمات تلگرام، نصب‌کننده کد و سرویس systemd را آماده می‌کند و سپس پنل ترمینال را برای **Guided setup** باز می‌کند. **ربات تا زمان واردکردن تنظیمات معتبر و شروع موفق سرویس فعال نمی‌شود.** پس از Setup، پنل سرویس را روشن می‌کند.

برای نصب و فعال‌سازی در همان اجرای اول، یک فایل تنظیمات معتبر با دسترسی `0600` آماده کنید و مسیر آن را با `--config` بدهید؛ مثال:

```bash
sudo chmod 600 /root/mikachan-test.env
curl -fsSL https://raw.githubusercontent.com/Dneeel/Mikachan/v2-test/deploy/bootstrap.sh | sudo bash -s -- --branch v2-test --config /root/mikachan-test.env
```

## ادامهٔ نصب آزمایشی پس از شکست آزمون‌ها

اگر نصب اولیه در `pytest` متوقف شده و `mikachan status` مقدار
`"incomplete_install": true` و `"installed": false` را نشان می‌دهد،
همان نصب را از طریق مدیر موجود ادامه دهید. این دستور شاخهٔ `v2-test` را دوباره
واکشی می‌کند، commit جدید را با استفاده از مخزن و کش قبلی آماده می‌کند و
آزمون‌ها را اجرا می‌کند. فایل‌های تنظیمات، داده و بکاپ را حذف نمی‌کند.

```bash
sudo mikachan status
sudo mikachan install --branch v2-test --code-only
sudo mikachan status
```

در نصب‌هایی که مدیر پنل از commit قدیمی کپی شده است، پس از نصب موفق، فایل
تأییدشدهٔ مدیر را یک بار از release جدید به پنل منتقل کنید تا `preflight`
مسیر `uv` داخلی پنل را درست نشان دهد:

```bash
sudo install -m 0644 /opt/Mikachan/current/deploy/manager.py /opt/Mikachan/panel/deploy/manager.py
sudo mikachan preflight
sudo mikachan
```

در پنل **Guided setup** را با توکن و شناسهٔ مالک آزمایشی تکمیل کنید. فعال‌سازی
سرویس فقط پس از موفقیت آزمون‌ها و اعتبارسنجی تنظیمات انجام می‌شود. سپس
`sudo mikachan diagnostics --live` را اجرا کنید. اگر `install` باز هم خطا داد،
متن همان خطا را بررسی کنید؛ `bootstrap-test.sh` برای نصب ناقص موجود مناسب نیست،
چون از بازنویسی ریشهٔ نصب جلوگیری می‌کند. `recover` برای تراکنش فعال‌سازیِ
ناتمام پس از ایجاد manifest است و در این حالت کمکی نمی‌کند. `abort-install`
هم برای این مسیر لازم نیست.

توکن و Session را در دستور ترمینال ننویسید. برای تست هم‌زمان با نسخهٔ اصلی، از **بات تلگرام و توکن جداگانه** استفاده کنید تا دو Poller برای یک بات فعال نشوند.

## محافظت از نسخهٔ اصلی

- این Bootstrap فقط برای نصب **روی VPS تازه** طراحی شده است.
- اگر `/opt/Mikachan/manifest.json`، نشانگر نصب مدیریت‌شده، یا سرویس `mikachan.service` از قبل وجود داشته باشد، **پیش از نصب و تغییر فایل‌ها متوقف می‌شود**.
- **نصب آزمایشی روی همان VPS فعال، با این اسکریپت پشتیبانی نمی‌شود.** برای این کار به نصب مستقل با مسیر، سرویس، دیتابیس و توکن جداگانه نیاز است.
- از شاخهٔ `v2-test` هیچ Merge یا Deploy خودکاری به `v2` انجام نمی‌شود.
- دستور `mikachan update` فقط شاخهٔ فعالی را که در Manifest ثبت شده به‌روز می‌کند؛ اگر `v2-test` نصب باشد، روی همان شاخه می‌ماند.

## بررسی فهرست نسخه‌ها بدون نصب

```bash
curl -fsSL https://raw.githubusercontent.com/Dneeel/Mikachan/v2-test/deploy/bootstrap.sh | bash -s -- --list-versions
```

این حالت فقط GitHub را می‌خواند، به `sudo` و `systemd` نیاز ندارد و هیچ فایلی روی سرور تغییر نمی‌دهد. `git` باید نصب باشد.

## نکات مهم

- شاخهٔ `v2-test` آزمایشی است؛ اجرای تست آفلاین، معادل تأیید روی VPS واقعی یا Telegram API نیست.
- `--commit` یک نسخهٔ تاریخی را انتخاب می‌کند؛ استفاده از Commit قدیمی با دیتابیس جدید ممکن است ناسازگار باشد. برای تست اولیه روی VPS تازه مناسب‌تر است.
- کد دریافت‌شده از GitHub قبل از اجرا باید مورد اعتماد باشد. برای استقرار حساس، URL را به Commit مشخص پین کنید و اسکریپت را قبل از اجرای `sudo` بازبینی کنید.
