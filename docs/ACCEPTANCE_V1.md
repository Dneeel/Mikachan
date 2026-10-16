# V1 acceptance ledger — 2026-10-16

This records every checkbox in the owner-supplied checklist. `PASS` requires a real provider/Telegram result or a deterministic local behavior test. `UNVERIFIED` means a path exists but a representative live outcome has not been confirmed. `NOT IMPLEMENTED` means the requested behavior is absent. Source-limited failures are `BLOCKED`. The source checklist ends with an incomplete sentence on its final line.

# MIKA NEXUS V1 — COMPLETE FEATURE & USER ACCEPTANCE CHECKLIST

## دستور اصلی برای Codex

# 1. راه‌اندازی و دسترسی کاربران

- **PASS** (source line 25): بات با Token معتبر اجرا شود و در Telegram پاسخ بدهد.
- **PASS** (source line 26): اجرای اولیه در Public Bot API Mode با Bot Token و شناسه مالک امکان‌پذیر باشد.
- **PASS** (source line 27): بات بعد از Restart مجدداً بالا بیاید.
- **UNVERIFIED** (source line 28): فقط شناسه‌های عددی مجاز اجازه استفاده داشته باشند.
- **PASS** (source line 29): مالک همیشه در Allowlist باقی بماند.
- **PASS** (source line 30): مجموع کاربران مجاز حداکثر چهار نفر باشد.
- **UNVERIFIED** (source line 31): کاربر غیرمجاز نتواند هیچ Command، دانلود یا جست‌وجویی اجرا کند.
- **UNVERIFIED** (source line 32): کاربر غیرمجاز نتواند از طریق Inline Button به قابلیت‌ها دسترسی پیدا کند.
- **UNVERIFIED** (source line 33): اضافه‌شدن یک کاربر مجاز به گروه، دسترسی سایر اعضای گروه را آزاد نکند.
- **UNVERIFIED** (source line 34): کاربران مجاز نتوانند Job، Search Session یا اطلاعات خصوصی یکدیگر را مدیریت کنند.
- **UNVERIFIED** (source line 35): Allowlist خالی یا خراب باعث بازشدن دسترسی عمومی نشود.
- **UNVERIFIED** (source line 36): هیچ Bot Token، Cookie یا Credential در پیام‌ها و Logها نمایش داده نشود.
# 2. تمام Commandهای نسخه اول

- **UNVERIFIED** (source line 42): `/start` — صفحه اصلی مرتب، کوتاه و دارای دکمه‌های واقعی.
- **UNVERIFIED** (source line 43): `/help` — نمایش راهنمای دسته‌بندی‌شده تمام قابلیت‌های موجود.
- **UNVERIFIED** (source line 44): `/search` — ورود به جست‌وجوی یکپارچه موسیقی و پادکست.
- **UNVERIFIED** (source line 45): `/search Radiohead` — شروع فوری جست‌وجوی Radiohead.
- **UNVERIFIED** (source line 46): `/music` — ورود به جست‌وجوی موسیقی.
- **UNVERIFIED** (source line 47): `/music Radiohead` — جست‌وجوی مستقیم موسیقی.
- **UNVERIFIED** (source line 48): `/podcast` — ورود به جست‌وجوی پادکست.
- **UNVERIFIED** (source line 49): `/podcast science` — جست‌وجوی مستقیم پادکست.
- **UNVERIFIED** (source line 50): `/youtube` — راهنما و امکانات اختصاصی YouTube.
- **UNVERIFIED** (source line 51): `/tiktok` — راهنما و امکانات اختصاصی TikTok.
- **UNVERIFIED** (source line 52): `/instagram` — راهنما و امکانات اختصاصی Instagram.
- **UNVERIFIED** (source line 53): `/twitter` — امکانات و راهنمای Twitter/X.
- **NOT IMPLEMENTED** (source line 54): `/settings` — تنظیمات شخصی و ترجیحات دانلود.
- **UNVERIFIED** (source line 55): `/queue` — نمایش وظایف فعال و در انتظار همان کاربر.
- **UNVERIFIED** (source line 56): `/status` — وضعیت دانلودها و سلامت کلی سرویس بدون افشای اطلاعات محرمانه.
- **UNVERIFIED** (source line 57): `/cancel` — امکان انتخاب و لغو Jobهای خود کاربر.
- **UNVERIFIED** (source line 58): `/cancel JOB_ID` — لغو دقیق همان Job متعلق به کاربر.
- **UNVERIFIED** (source line 59): `/admin` — منوی مدیریت مخصوص مالک.
# 3. قانون واکنش بات به پیام‌ها

- **UNVERIFIED** (source line 67): بات در حالت عادی فقط به Commandهای شناخته‌شده و پیام‌های دارای لینک معتبر HTTP/HTTPS واکنش نشان دهد.
- **UNVERIFIED** (source line 68): پیام معمولی `Hello` بدون Search Session نادیده گرفته شود.
- **UNVERIFIED** (source line 69): پیام معمولی `Radiohead` بدون Search Session نادیده گرفته شود.
- **UNVERIFIED** (source line 70): عکس بدون Command نادیده گرفته شود.
- **UNVERIFIED** (source line 71): ویدئو بدون Command نادیده گرفته شود.
- **UNVERIFIED** (source line 72): Voice، آهنگ، فایل و Sticker بدون Command نادیده گرفته شوند.
- **UNVERIFIED** (source line 73): Forward بدون لینک معتبر نادیده گرفته شود.
- **UNVERIFIED** (source line 74): متن معمولی پس از `/music` یا `/search` فقط در Search Session فعال به‌عنوان Query قبول شود.
- **UNVERIFIED** (source line 75): پس از منقضی‌شدن Search Session، متن‌های معمولی دوباره نادیده گرفته شوند.
- **UNVERIFIED** (source line 76): اگر در Search Session لینکی ارسال شود، قانون تأیید لینک بر جست‌وجوی متنی اولویت داشته باشد.
# 4. رفتار اجباری لینک و دکمه‌های Yes / No

- **UNVERIFIED** (source line 82): ارسال لینک معتبر باعث نمایش یک پیام تأیید شود.
- **UNVERIFIED** (source line 83): پیام تأیید ترجیحاً Reply به همان لینک کاربر باشد.
- **UNVERIFIED** (source line 84): در پیام تأیید فقط دو دکمه `✅ Yes` و `❌ No` وجود داشته باشد.
- **UNVERIFIED** (source line 85): قبل از Yes، هیچ دانلود، جست‌وجو، استخراج Metadata از شبکه یا درخواست به سایت مبدأ انجام نشود.
- **UNVERIFIED** (source line 86): اگر کاربر No بزند، پیام تأیید خود بات حذف شود.
- **UNVERIFIED** (source line 87): با No، پیام لینک اصلی کاربر حذف نشود.
- **UNVERIFIED** (source line 88): با No، Job ساخته نشود.
- **UNVERIFIED** (source line 89): با No، پیام اضافی مانند «لغو شد» ارسال نشود.
- **UNVERIFIED** (source line 90): اگر حذف پیام تأیید ممکن نبود، دکمه‌ها غیرفعال یا حذف شوند.
- **UNVERIFIED** (source line 91): با Yes، درخواست فقط یک‌بار پذیرفته شود.
- **UNVERIFIED** (source line 92): با Yes، همان پیام به وضعیت Preparing یا Processing تبدیل شود.
- **UNVERIFIED** (source line 93): برای لینک ویدئوی تکی، دانلود مطابق تنظیمات پیش‌فرض آغاز شود.
- **UNVERIFIED** (source line 94): برای لینک پروفایل، صفحه اطلاعات و مرور محتوای قابل‌دسترسی باز شود.
- **UNVERIFIED** (source line 95): برای لینک Playlist یا Album، فهرست و انتخاب آیتم‌ها باز شود.
- **UNVERIFIED** (source line 96): برای لینک پادکست، اطلاعات و انتخاب اپیزود نمایش داده شود.
- **UNVERIFIED** (source line 97): برای لینک فایل مستقیم، دریافت فایل آغاز شود.
- **UNVERIFIED** (source line 98): برای لینک نامعتبر یا غیرقابل‌دسترسی، پس از Yes خطای واضح نمایش داده شود.
- **UNVERIFIED** (source line 99): پس از پذیرش Yes، سؤال تأیید برای همان رویداد دوباره تکرار نشود.
- **UNVERIFIED** (source line 100): دکمه Yes چند بار پشت‌سرهم، چند Job یکسان نسازد.
- **UNVERIFIED** (source line 101): رقابت هم‌زمان Yes و No باعث رفتار متناقض نشود.
- **UNVERIFIED** (source line 102): دکمه‌های منقضی‌شده نتوانند دانلود جدیدی آغاز کنند.
- **UNVERIFIED** (source line 103): دکمه تأییدِ کاربر A توسط کاربر B قابل استفاده نباشد.
- **UNVERIFIED** (source line 104): ارسال چند لینک در یک پیام تا سقف تنظیم‌شده پشتیبانی شود.
- **UNVERIFIED** (source line 105): هر لینک از پیام چندلینکی، تأیید Yes/No مستقل داشته باشد.
- **UNVERIFIED** (source line 106): لینک‌های اضافی بیشتر از سقف مجاز، بدون ایجاد صف نامحدود کنترل شوند.
- **UNVERIFIED** (source line 107): بعد از Restart، دریافت دوباره همان Update باعث ایجاد Prompt یا Job تکراری نشود.
# 5. جست‌وجوی یکپارچه Music & Podcast

## 5.1 رفتار اصلی جست‌وجو

- **UNVERIFIED** (source line 113): `/search Radiohead` جست‌وجوی واقعی را آغاز کند.
- **UNVERIFIED** (source line 114): `/music Radiohead` جست‌وجوی واقعی موسیقی را آغاز کند.
- **UNVERIFIED** (source line 115): `/podcast science` جست‌وجوی واقعی پادکست را آغاز کند.
- **UNVERIFIED** (source line 116): `/music` بدون Query از کاربر متن جست‌وجو بخواهد.
- **UNVERIFIED** (source line 117): نوشتن `Radiohead` در Session فعال، همان جست‌وجو را اجرا کند.
- **UNVERIFIED** (source line 118): حداکثر ۱۰ پیشنهاد در هر صفحه نمایش داده شود.
- **UNVERIFIED** (source line 119): هر نتیجه شماره و دکمه انتخاب خودش را داشته باشد.
- **UNVERIFIED** (source line 120): نام پلتفرم و نوع محتوا کنار نتیجه مشخص باشد.
- **UNVERIFIED** (source line 121): انتخاب نتیجه ۱ دقیقاً همان نتیجه را باز کند.
- **UNVERIFIED** (source line 122): انتخاب نتیجه ۷ دقیقاً همان نتیجه را باز کند.
- **UNVERIFIED** (source line 123): هیچ نتیجه ساختگی برای پرکردن ۱۰ جایگاه تولید نشود.
- **UNVERIFIED** (source line 124): اگر فقط سه نتیجه معتبر وجود دارد، فقط همان سه نتیجه نمایش داده شوند.
- **FAIL** (source line 125): نتایج نامرتبط صرفاً برای تکمیل صفحه نمایش داده نشوند.
## 5.2 فیلتر نوع محتوا — Types

- **UNVERIFIED** (source line 131): Track
- **UNVERIFIED** (source line 132): Album
- **UNVERIFIED** (source line 133): Playlist
- **UNVERIFIED** (source line 134): Artist / Creator
- **UNVERIFIED** (source line 135): Podcast / Show
- **UNVERIFIED** (source line 136): Episode
- **UNVERIFIED** (source line 140): فقط Track فعال باشد: هیچ Album یا Playlist یا Podcast نمایش داده نشود.
- **UNVERIFIED** (source line 141): Track و Album فعال باشند: هر دو نوع بتوانند در یک صفحه کنار هم قرار گیرند.
- **UNVERIFIED** (source line 142): فقط Album فعال باشد: نتایج Track حذف شوند.
- **UNVERIFIED** (source line 143): فقط Podcast فعال باشد: آهنگ و آلبوم نمایش داده نشود.
- **UNVERIFIED** (source line 144): Podcast و Episode فعال باشند: هر دو نوع مجاز باشند.
- **UNVERIFIED** (source line 145): فعال‌بودن تمام نوع‌ها امکان‌پذیر باشد.
- **UNVERIFIED** (source line 146): با غیرفعال‌کردن همه نوع‌ها، جست‌وجوی بیهوده اجرا نشود و پیام راهنما نمایش داده شود.
- **UNVERIFIED** (source line 147): تغییر فیلتر واقعاً جست‌وجوی جدید با شرایط تازه اجرا کند، نه صرفاً حذف ظاهری چند نتیجه.
- **UNVERIFIED** (source line 148): با تغییر فیلتر، صفحه‌بندی به صفحه اول برگردد.
## 5.3 فیلتر پلتفرم‌ها — Sources

- **BLOCKED** (source line 154): Spotify
- **PASS** (source line 155): SoundCloud
- **PASS** (source line 156): YouTube Music
- **PASS** (source line 157): Apple Music
- **PASS** (source line 158): Deezer
- **PASS** (source line 159): Apple Podcasts
- **NOT IMPLEMENTED** (source line 160): منابع عمومی Podcast/RSS که واقعاً قابلیت جست‌وجو دارند
- **UNVERIFIED** (source line 164): فقط Spotify فعال باشد: نتیجه‌ای با برچسب SoundCloud نمایش داده نشود.
- **UNVERIFIED** (source line 165): فقط SoundCloud فعال باشد: فقط داده‌های واقعی آن منبع نمایش داده شوند.
- **BLOCKED** (source line 166): Spotify و SoundCloud هم‌زمان فعال باشند: نتایج هر دو بتوانند در همان ۱۰ پیشنهاد حضور داشته باشند.
- **UNVERIFIED** (source line 167): افزودن YouTube Music به منابع، جست‌وجوی همان منبع را نیز فعال کند.
- **UNVERIFIED** (source line 168): حذف یک پلتفرم، نتایج آن را از صفحه جدید حذف کند.
- **UNVERIFIED** (source line 169): روشن و خاموش‌کردن منابع مختلف مستقل از فیلتر نوع محتوا کار کند.
- **UNVERIFIED** (source line 170): نتیجه Spotify هرگز با برچسب SoundCloud یا برعکس نمایش داده نشود.
- **UNVERIFIED** (source line 171): اگر پلتفرم به Credential نیاز دارد، وضعیت واقعی آن مشخص باشد.
- **UNVERIFIED** (source line 172): اگر منبعی موقتاً خراب است، سایر منابع سالم همچنان نتیجه بدهند.
- **UNVERIFIED** (source line 173): اگر منبعی واقعاً قابل جست‌وجو نیست، بات وانمود نکند که آن را جست‌وجو کرده است.
## 5.4 نسخه‌های مختلف آهنگ

- **UNVERIFIED** (source line 179): نسخه Original آهنگ جداگانه شناسایی شود.
- **UNVERIFIED** (source line 180): نسخه Slowed جداگانه نمایش داده شود، اگر واقعاً وجود دارد.
- **UNVERIFIED** (source line 181): نسخه Slowed + Reverb با Original ادغام اشتباه نشود.
- **UNVERIFIED** (source line 182): Remix به‌عنوان نسخه متفاوت شناخته شود.
- **UNVERIFIED** (source line 183): Live به‌عنوان نسخه متفاوت شناخته شود.
- **UNVERIFIED** (source line 184): Cover به‌عنوان نسخه متفاوت شناخته شود.
- **UNVERIFIED** (source line 185): Acoustic، Instrumental و Remastered در صورت وجود از هم قابل‌تمایز باشند.
- **UNVERIFIED** (source line 186): نتیجه‌ای فقط به‌دلیل داشتن عنوان مشابه Official فرض نشود.
- **UNVERIFIED** (source line 187): نتایج متفاوت از چند پلتفرم صرفاً به‌دلیل مشابه‌بودن عنوان حذف نشوند.
- **UNVERIFIED** (source line 188): برای آزمون، جست‌وجوی `Radiohead Creep` انجام شود و وجود نتایج رسمی و نسخه‌های غیررسمیِ واقعاً قابل‌دسترسی بررسی شود؛ نبودن نسخه Slowed نباید با ساخت نتیجه جعلی جبران شود.
## 5.5 صفحه‌بندی و انتخاب نتیجه

- **UNVERIFIED** (source line 192): Next Page کار کند.
- **UNVERIFIED** (source line 193): Previous Page کار کند.
- **UNVERIFIED** (source line 194): Refresh کار کند.
- **UNVERIFIED** (source line 195): Back کار کند.
- **UNVERIFIED** (source line 196): Close کار کند.
- **UNVERIFIED** (source line 197): انتخاب یک نتیجه، صفحه جزئیات صحیح باز کند.
- **UNVERIFIED** (source line 198): عنوان، Artist، نوع محتوا، پلتفرم و اطلاعات موجود نمایش داده شوند.
- **UNVERIFIED** (source line 199): گزینه دانلود یا یافتن منبع صوتی واقعی متناسب با نتیجه وجود داشته باشد.
- **UNVERIFIED** (source line 200): انتخاب Album فهرست Trackهای آن را در صورت دسترسی نمایش دهد.
- **NOT IMPLEMENTED** (source line 201): انتخاب Artist اطلاعات و محتواهای مرتبط قابل‌دسترسی را باز کند.
- **UNVERIFIED** (source line 202): انتخاب Playlist امکان انتخاب آهنگ‌های مشخص را فراهم کند.
- **UNVERIFIED** (source line 203): انتخاب Podcast فهرست اپیزودها را باز کند.
- **UNVERIFIED** (source line 204): دکمه قدیمی یا Session منقضی‌شده نتیجه اشتباه باز نکند.
- **UNVERIFIED** (source line 205): تغییر سریع فیلترها نتیجه جست‌وجوی قدیمی را روی UI جدید ننویسد.
- **UNVERIFIED** (source line 206): تنظیمات انتخاب‌شده پس از بازکردن صفحه جزئیات، هنگام Back حفظ شوند.
# 6. YouTube Downloader

- **UNVERIFIED** (source line 210): دریافت لینک ویدئوی معمولی.
- **UNVERIFIED** (source line 211): دریافت YouTube Shorts.
- **UNVERIFIED** (source line 212): دانلود ویدئو بعد از Yes.
- **PASS** (source line 213): دانلود Audio Only.
- **UNVERIFIED** (source line 214): ارائه Best Original در صورت امکان.
- **NOT IMPLEMENTED** (source line 215): ارائه نسخه مناسب پخش تلگرام در صورت امکان.
- **NOT IMPLEMENTED** (source line 216): نمایش کیفیت‌های واقعی موجود.
- **NOT IMPLEMENTED** (source line 217): انتخاب 480p، 720p، 1080p و کیفیت‌های دیگر فقط در صورت موجودبودن.
- **NOT IMPLEMENTED** (source line 218): دریافت عنوان و نام کانال.
- **UNVERIFIED** (source line 219): حفظ لینک منبع.
- **NOT IMPLEMENTED** (source line 220): نمایش یا دریافت Thumbnail در صورت دسترسی.
- **NOT IMPLEMENTED** (source line 221): دریافت زیرنویس‌های موجود.
- **NOT IMPLEMENTED** (source line 222): نمایش Chapters در صورت وجود.
- **UNVERIFIED** (source line 223): تشخیص Playlist و نمایش فهرست برای انتخاب.
- **UNVERIFIED** (source line 224): عدم دانلود خودکار کل Playlist بدون انتخاب کاربر.
- **UNVERIFIED** (source line 225): مدیریت خطای `Sign in to confirm you're not a bot`.
- **UNVERIFIED** (source line 226): نمایش خطای انسانی به‌جای stderr خام yt-dlp.
- **PASS** (source line 227): نصب و عملکرد yt-dlp همراه با JavaScript Runtime و EJS سازگار.
- **UNVERIFIED** (source line 228): مدیریت ویدئوی حذف‌شده، Private یا غیرقابل‌دسترسی بدون Crash.
# 7. TikTok Downloader

- **PASS** (source line 232): دانلود Video عمومی.
- **PASS** (source line 233): دانلود Photo Slideshow.
- **UNVERIFIED** (source line 234): حفظ ترتیب تصاویر.
- **NOT IMPLEMENTED** (source line 235): دریافت متن کپشن در صورت دسترسی.
- **NOT IMPLEMENTED** (source line 236): دریافت Sound Metadata در صورت دسترسی.
- **UNVERIFIED** (source line 237): استخراج صدای ویدئو.
- **NOT IMPLEMENTED** (source line 238): استفاده از نسخه بدون واترمارک، فقط وقتی منبع واقعی آن را ارائه می‌کند.
- **UNVERIFIED** (source line 239): تشخیص لینک Creator Profile.
- **NOT IMPLEMENTED** (source line 240): نمایش اطلاعات عمومی پروفایل.
- **UNVERIFIED** (source line 241): دانلود Avatar در صورت دسترسی.
- **UNVERIFIED** (source line 242): نمایش تعداد محدود پست‌های اخیر، در صورت پشتیبانی معتبر.
- **UNVERIFIED** (source line 243): انتخاب یک پست مشخص از فهرست.
- **UNVERIFIED** (source line 244): مدیریت لینک‌های کوتاه و Redirectهای TikTok با رعایت امنیت URL.
- **UNVERIFIED** (source line 245): اعلام صریح محدودیت در قابلیت‌های خراب یا نیازمند دسترسی.
# 8. Instagram Downloader

- **PASS** (source line 249): دانلود Reel عمومی.
- **BLOCKED** (source line 250): دانلود Post تصویری.
- **UNVERIFIED** (source line 251): دانلود Post ویدئویی.
- **UNVERIFIED** (source line 252): دانلود Carousel چندتصویری.
- **UNVERIFIED** (source line 253): دانلود Carousel ترکیبی تصویر و ویدئو.
- **UNVERIFIED** (source line 254): حفظ ترتیب دقیق رسانه‌های Carousel.
- **UNVERIFIED** (source line 255): عدم حذف عکس یا ویدئو از پست ترکیبی.
- **UNVERIFIED** (source line 256): دریافت کپشن و نام سازنده در صورت دسترسی.
- **UNVERIFIED** (source line 257): تشخیص لینک Profile.
- **NOT IMPLEMENTED** (source line 258): نمایش Avatar و اطلاعات عمومی در صورت دسترسی.
- **BLOCKED** (source line 259): دریافت Stories فقط در محدوده دسترسی معتبر.
- **BLOCKED** (source line 260): دریافت Highlights فقط در محدوده دسترسی معتبر.
- **UNVERIFIED** (source line 261): اعلام نیاز به Cookie یا Authentication هنگام ضرورت.
- **UNVERIFIED** (source line 262): عدم ادعای دسترسی به محتوای خصوصی بدون مجوز.
- **UNVERIFIED** (source line 263): مدیریت لینک‌های Share و Tracking Parameters.
# 9. Twitter / X Downloader

- **UNVERIFIED** (source line 267): فرمان `/twitter` به‌درستی کار کند.
- **UNVERIFIED** (source line 268): لینک‌های `twitter.com` شناسایی شوند.
- **UNVERIFIED** (source line 269): لینک‌های `x.com` شناسایی شوند.
- **UNVERIFIED** (source line 270): لینک‌های کوتاه `t.co` با اعتبارسنجی Redirect مدیریت شوند.
- **UNVERIFIED** (source line 271): ویدئوهای عمومی قابل‌دسترسی دانلود شوند.
- **UNVERIFIED** (source line 272): تصاویر پست دانلود شوند.
- **NOT IMPLEMENTED** (source line 273): GIF/Animated Media در صورت پشتیبانی دریافت شوند.
- **NOT IMPLEMENTED** (source line 274): پست دارای چند رسانه، همه آیتم‌های قابل‌دسترسی را حفظ کند.
- **NOT IMPLEMENTED** (source line 275): متن و لینک پست در خروجی حفظ شوند.
- **UNVERIFIED** (source line 276): نبود مجوز یا محدودیت منبع به‌وضوح اعلام شود.
# 10. سایر شبکه‌های اجتماعی

- **UNVERIFIED** (source line 280): Reddit Video و Audio در صورت موجودبودن درست ترکیب شوند.
- **UNVERIFIED** (source line 281): Reddit Image و Gallery دریافت شوند.
- **UNVERIFIED** (source line 282): Pinterest Image دریافت شود.
- **UNVERIFIED** (source line 283): Pinterest Video در صورت دسترسی دریافت شود.
- **NOT IMPLEMENTED** (source line 284): مجموعه‌های Pinterest فقط با انتخاب محدود و مشخص پردازش شوند.
- **UNVERIFIED** (source line 285): Facebook Video عمومی در صورت پشتیبانی موتور دریافت شود.
- **UNVERIFIED** (source line 286): لینک سایر سایت‌های پشتیبانی‌شده توسط yt-dlp یا gallery-dl با Router ایمن بررسی شوند.
- **UNVERIFIED** (source line 287): بات موفقیت دانلود همه سایت‌ها را بدون تست واقعی تضمین نکند.
# 11. Music Downloader و کیفیت تطبیق

- **PASS** (source line 291): لینک Spotify Track به جزئیات واقعی آهنگ برسد.
- **PASS** (source line 292): لینک Spotify Album فهرست قطعات واقعی را باز کند.
- **BLOCKED** (source line 293): لینک Spotify Playlist امکان انتخاب قطعات بدهد.
- **NOT IMPLEMENTED** (source line 294): لینک Spotify Artist اطلاعات هنرمند را نشان دهد.
- **UNVERIFIED** (source line 295): لینک SoundCloud Track در صورت دسترسی دانلود شود.
- **UNVERIFIED** (source line 296): لینک SoundCloud Playlist قابل مرور باشد.
- **PASS** (source line 297): لینک YouTube Music قابل شناسایی و پردازش باشد.
- **UNVERIFIED** (source line 298): لینک Apple Music و Deezer به داده‌های کاتالوگ مرتبط برسند، اگر Adapter واقعاً در دسترس است.
- **PASS** (source line 299): بات فایل DRMدار Spotify/Apple/Deezer را به‌عنوان فایل دانلودشده مستقیم ادعا نکند.
- **UNVERIFIED** (source line 300): هنگام یافتن منبع صوتی جایگزین، عنوان و Artist تطبیق داده شوند.
- **UNVERIFIED** (source line 301): مدت آهنگ در مقایسه لحاظ شود.
- **NOT IMPLEMENTED** (source line 302): ISRC در صورت موجودبودن استفاده شود.
- **UNVERIFIED** (source line 303): Original و Live اشتباه جایگزین نشوند.
- **UNVERIFIED** (source line 304): Original و Slowed اشتباه جایگزین نشوند.
- **UNVERIFIED** (source line 305): هنگام تطبیق نامطمئن، کاربر بتواند گزینه مناسب را انتخاب کند.
- **NOT IMPLEMENTED** (source line 306): کیفیت واقعی منبع از Bitrate خروجی تبدیل‌شده متمایز باشد.
- **NOT IMPLEMENTED** (source line 307): کاور، عنوان، Artist و Album در فایل صوتی تا حد امکان صحیح ثبت شوند.
- **NOT IMPLEMENTED** (source line 308): Lyrics معمولی یا همگام در صورت موجودبودن از منبع مجاز نمایش داده شوند.
- **UNVERIFIED** (source line 309): Lyrics ساختگی، متعلق به آهنگ اشتباه یا با برچسب منبع اشتباه نمایش داده نشوند.
- **UNVERIFIED** (source line 310): هیچ Playlist بزرگی بدون انتخاب صریح کاربر به‌طور کامل دانلود نشود.
# 12. Podcast Downloader

- **PASS** (source line 314): جست‌وجوی عنوان Podcast کار کند.
- **PASS** (source line 315): جست‌وجوی Episode در منابع دارای قابلیت جست‌وجو کار کند.
- **PASS** (source line 316): Apple Podcasts Search در صورت دسترسی واقعی کار کند.
- **PASS** (source line 317): لینک RSS عمومی پردازش شود.
- **UNVERIFIED** (source line 318): لینک Castbox عمومی در صورت دسترسی معتبر شناسایی شود.
- **PASS** (source line 319): انتخاب یک Podcast فهرست اپیزودها را باز کند.
- **UNVERIFIED** (source line 320): عنوان و تاریخ انتشار اپیزود صحیح باشد.
- **NOT IMPLEMENTED** (source line 321): کاور و مدت اپیزود در صورت وجود نمایش داده شوند.
- **UNVERIFIED** (source line 322): انتخاب یک Episode باعث دریافت همان قسمت شود.
- **UNVERIFIED** (source line 323): پخش و ارسال فایل Podcast در تلگرام درست کار کند.
- **UNVERIFIED** (source line 324): Podcast دارای محتوای غیرقابل‌دسترسی خطای واضح نشان دهد.
- **PASS** (source line 325): دانلود خودکار تمام اپیزودهای یک مجموعه ممنوع باشد.
# 13. File Downloader و Cloud

- **UNVERIFIED** (source line 329): لینک مستقیم HTTPS دانلود شود.
- **NOT IMPLEMENTED** (source line 330): نام اصلی فایل تا حد امکان حفظ شود.
- **UNVERIFIED** (source line 331): پسوند و نوع فایل اشتباه تشخیص داده نشوند.
- **UNVERIFIED** (source line 332): فایل بزرگ به‌صورت Stream دانلود شود، نه بارگذاری کامل در RAM.
- **NOT IMPLEMENTED** (source line 333): Resume برای سرورهای دارای HTTP Range کار کند.
- **UNVERIFIED** (source line 334): سروری که Range ندارد باعث فساد فایل نشود.
- **UNVERIFIED** (source line 335): Redirect معتبر درست دنبال شود.
- **UNVERIFIED** (source line 336): Google Drive Public File در صورت دسترسی دانلود شود.
- **NOT IMPLEMENTED** (source line 337): لینک Public MEGA با Adapter معتبر در صورت فعال‌بودن پردازش شود.
- **UNVERIFIED** (source line 338): فایل‌هاست‌های دیگر فقط در صورت پشتیبانی واقعی فعال باشند.
- **UNVERIFIED** (source line 339): محدودیت حجم قبل و هنگام دانلود کنترل شود.
- **NOT IMPLEMENTED** (source line 340): فضای دیسک کافی قبل از عملیات سنگین بررسی شود.
- **UNVERIFIED** (source line 341): فایل ناقص به‌عنوان دانلود موفق ارسال نشود.
- **UNVERIFIED** (source line 342): فایل بزرگ‌تر از حد Telegram به‌درستی مدیریت شود.
- **NOT IMPLEMENTED** (source line 343): اگر Multipart فعال است، همه بخش‌ها و اطلاعات بازسازی صحیح باشند.
- **UNVERIFIED** (source line 344): فایل‌های موقت پس از پایان و انقضای کش طبق سیاست پاک‌سازی شوند.
# 14. کیفیت خروجی و اعتبارسنجی رسانه

- **UNVERIFIED** (source line 348): بهترین کیفیت قابل‌تحویل به‌صورت پیش‌فرض انتخاب شود.
- **NOT IMPLEMENTED** (source line 349): Best Original از Telegram-Compatible Output قابل‌تمایز باشد.
- **UNVERIFIED** (source line 350): تبدیل غیرضروری فرمت انجام نشود.
- **UNVERIFIED** (source line 351): Remux بدون Encode مجدد در شرایط مناسب ترجیح داده شود.
- **NOT IMPLEMENTED** (source line 352): اگر خروجی به تبدیل نیاز دارد، دلیل آن روشن باشد.
- **UNVERIFIED** (source line 353): ویدئوهای دارای Stream صوتی و تصویری کامل شناسایی شوند.
- **NOT IMPLEMENTED** (source line 354): ویدئوی واقعاً Silent با ویدئوی ناقص اشتباه نشود.
- **NOT IMPLEMENTED** (source line 355): فایل Corrupt یا Truncated شناسایی شود.
- **NOT IMPLEMENTED** (source line 356): فایل خروجی با محتوای درخواستی تطبیق داده شود.
- **NOT IMPLEMENTED** (source line 357): شناسه ویدئو یا Track اشتباه باعث تحویل فایل نامرتبط نشود.
- **NOT IMPLEMENTED** (source line 358): سلامت فایل با `VERIFIED`، `UNVERIFIED` یا `INVALID` طبقه‌بندی شود.
- **NOT IMPLEMENTED** (source line 359): `UNVERIFIED` هرگز به‌عنوان سالمِ قطعی معرفی نشود.
- **NOT IMPLEMENTED** (source line 360): Thumbnail و Caption به رسانه اشتباه متصل نشوند.
- **NOT IMPLEMENTED** (source line 361): Caption اصلی و Source URL در صورت امکان حفظ شوند.
- **NOT IMPLEMENTED** (source line 362): متن طولانی Caption بدون خراب‌شدن ارسال مدیریت شود.
- **UNVERIFIED** (source line 363): آلبوم‌های ۱، ۲، ۹، ۱۰، ۱۱، ۱۲، ۲۰، ۲۱ و ۳۱ رسانه‌ای درست تقسیم و ارسال شوند.
- **UNVERIFIED** (source line 364): گروه تک‌عضوی اشتباهاً با `sendMediaGroup` ارسال نشود.
- **UNVERIFIED** (source line 365): ترتیب عکس‌ها و ویدئوها بعد از تقسیم گروه‌ها حفظ شود.
# 15. رابط کاربری Telegram

- **UNVERIFIED** (source line 369): رابط کاملاً Native Telegram باشد.
- **UNVERIFIED** (source line 370): هیچ Mini App یا وب‌پنل موردنیاز نباشد.
- **UNVERIFIED** (source line 371): دکمه‌ها واقعاً عملکرد داشته باشند.
- **UNVERIFIED** (source line 372): هیچ دکمه Placeholder به کاربر نشان داده نشود.
- **UNVERIFIED** (source line 373): لینک تکی بدون پیمودن چندین منوی اضافی پردازش شود.
- **NOT IMPLEMENTED** (source line 374): پیشرفت دانلود در پیام موجود به‌روزرسانی شود.
- **UNVERIFIED** (source line 375): کاربر با پیام‌های مکرر پیشرفت Spam نشود.
- **NOT IMPLEMENTED** (source line 376): مرحله Resolving، Downloading، Processing و Uploading از هم قابل‌تمایز باشند.
- **UNVERIFIED** (source line 377): پیشرفت نامعلوم با درصد جعلی نمایش داده نشود.
- **UNVERIFIED** (source line 378): دکمه Cancel هنگام وجود Job واقعی فعال باشد.
- **UNVERIFIED** (source line 379): دکمه‌های مرتبط با محتوا نمایش داده شوند؛ مثلاً MP3 فقط برای محتوای دارای صدا.
- **UNVERIFIED** (source line 380): Back/Next در Explorerها کار کند.
- **UNVERIFIED** (source line 381): Keyboardها بعد از منقضی‌شدن Session رفتار امن داشته باشند.
- **UNVERIFIED** (source line 382): Callbackهای طولانی از محدودیت ۶۴ بایت Telegram عبور نکنند.
- **UNVERIFIED** (source line 383): خطای Telegram `message is not modified` باعث Crash نشود.
- **NOT IMPLEMENTED** (source line 384): خطای Rate Limit تلگرام به‌درستی مدیریت شود.
- **UNVERIFIED** (source line 385): خروجی فارسی/انگلیسی و کاراکترهای خاص باعث خراب

## Reproduction and repair for FAIL/BLOCKED

- **Generic search relevance (source line 125, FAIL):** Search a broad query and inspect the bottom of the result pages. Provider responses are displayed without a global relevance cutoff. Add provider-aware ranking and confirm with representative text queries.
- **Spotify search filter (lines 154/166, BLOCKED):** Select only Spotify in Sources and search. The adapter reports missing `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`. Supply a Spotify developer client ID/secret for the official catalog search; cross-source music search remains available.
- **Spotify playlist (line 293, BLOCKED):** Open a public playlist link and approve it. The tested public embed endpoint returned HTTP 500. Add valid Spotify API credentials or another stable metadata endpoint; never auto-download a full list.
- **Instagram photo/story/highlight (lines 250/259/260, BLOCKED):** The owner-submitted public photo timed out after 45 seconds in gallery-dl; a story previously requested login. A valid authorized Instagram session may be needed for these source routes. Retest a public fixture with that session before marking PASS.

The complete live results and provider limitations are in [TEST_REPORT.md](TEST_REPORT.md) and [PROVIDER_MATRIX.md](PROVIDER_MATRIX.md).
