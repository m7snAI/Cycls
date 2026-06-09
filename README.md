# 🏛️ Etimad Tenders → Supabase

سحب منافسات اعتماد (`tenders.etimad.sa`) وتخزينها في Supabase: المنافسات النشطة يومياً،
backfill تاريخي كامل (~283k منافسة)، وبيانات الترسية (المتقدمين + الفائزين) للمنافسات المغلقة.

## 📁 بنية المشروع

```
etimad_tender/
├─ README.md                 ← الملف ده
├─ requirements.txt          ← الـ Python dependencies
├─ run_backfill.py           ← orchestrator للـ backfill (awards → details)
├─ db/
│  └─ schema.sql             ← جداول Supabase (شغّله مرة واحدة في الـ SQL Editor)
├─ scrapers/
│  ├─ daily.py               ← السحب اليومي: الـ active set + المنافسات الجديدة + التفاصيل
│  ├─ parallel.py            ← الـ backfill التاريخي المتوازي + سحب الترسية (MODE=awards)
│  └─ probe.py               ← أداة probe لاكتشاف حد الـ pagination (تشخيص)
├─ data/
│  ├─ activities.json        ← شجرة الأنشطة (للـ sharding في parallel.py)
│  ├─ activity_totals.json
│  └─ sub_activity_totals.json
├─ tools/
│  └─ scrape_details_in_browser.js   ← سحب التفاصيل من console المتصفح (fallback، فيه الـ key)
└─ .github/workflows/
   ├─ scrape.yml             ← يومي 6ص توقيت السعودية (scrapers/daily.py)
   ├─ awards-scrape.yml      ← أسبوعي الإثنين (scrapers/parallel.py MODE=awards)
   ├─ parallel-scrape.yml    ← يدوي: backfill المتوازي
   └─ probe-pagination-cap.yml ← يدوي: probe
```

## 🚀 الـ Setup

### 1) Supabase project
- أنشئ project على [supabase.com](https://supabase.com).
- من `Settings → API` انسخ `Project URL` و `service_role` key (مش الـ anon).
- ⚠️ الـ service_role key بيتجاوز الـ RLS — احفظه في secrets/`.env` بس، ومتـ commit-هوش.

### 2) الجداول
- Dashboard → SQL Editor → New Query → الصق محتوى `db/schema.sql` → Run.
- بينشئ `tenders`, `scrape_runs`, `tender_awards`, والـ view `active_tenders`.

### 3) الـ env
حط الاتنين دول في `.env` (محلياً) و في GitHub repo secrets (للـ Actions):
```
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_SERVICE_KEY=eyJ...
```

```bash
pip install -r requirements.txt
```

## ▶️ التشغيل

**السحب اليومي** (active set + الجديد + التفاصيل، وبيعطّل اللي اختفى):
```bash
python scrapers/daily.py
```
بيشتغل تلقائي كل يوم عن طريق `scrape.yml` (cron 6ص السعودية).

**الـ backfill التاريخي** (awards الأول، بعدها details — resumable من الـ DB):
```bash
USE_PROXY=false python run_backfill.py > backfill.log 2>&1
```
الـ orchestrator بيشغّل `scrapers/parallel.py` على مراحل ويعيد التشغيل لو في crash.
tunables: `AWARD_CONCURRENCY`, `DETAIL_CONCURRENCY`, `DETAIL_BATCH`, `SKIP_AWARDS`, `SKIP_DETAILS`.

**الترسية بس** (المتقدمين + الفائزين للمنافسات حالتها "تم اعتماد الترسية"):
```bash
MODE=awards python scrapers/parallel.py
```
بيشتغل تلقائي أسبوعياً عن طريق `awards-scrape.yml`. بيستهدف الجديد أوتوماتيك
(`get_ids_needing_awards`) و `awards_last_checked` بيمنع إعادة فحص اللي طلعت فاضية.

> **DNS:** على ويندوز محلياً ممكن تشوف `getaddrinfo failed` متقطّع تحت الضغط — حط DNS
> ثابت (`1.1.1.1`). كل المراحل resumable فمش بتخسر تقدّم.

## 📊 استخدام الداتا

```sql
-- المنافسات النشطة
select * from active_tenders where last_offer_date > now()
order by last_offer_date asc limit 50;

-- بحث نصي بالعربي (trigram)
select tender_name, agency_name, last_offer_date from tenders
where tender_name % 'صيانة' and is_active = true
order by similarity(tender_name, 'صيانة') desc limit 20;

-- المتقدمين والفائزين لمنافسة
select bidder_name, offer_value, tech_evaluation, award_value, role
from tender_awards where etimad_tender_id = '<id>' order by role, offer_value;

-- تتبع الـ scrapes
select * from scrape_runs order by started_at desc limit 10;
```

## 🔧 مشاكل شائعة

| المشكلة | الحل |
|---------|------|
| `HTTP 429 / Too Many Requests` | الـ IP وصل حد الـ rate. الـ scripts فيها rate-limiter + retries؛ قلّل الـ concurrency أو استنى |
| `HTTP 403` / redirect لـ login | بعض الـ endpoints auth-walled للزوار — طبيعي، الكود بيتعامل معاه |
| `getaddrinfo failed` | DNS محلي بيتقطّع — حط `1.1.1.1`. resumable فمفيش خسارة |
| `place` فاضي في صفوف كتير | الـ relations endpoint بيـ 429 تحت الـ concurrency؛ الـ daily scraper (sequential) بيعبّيه للنشطة |

## 📝 ملاحظات

- **المرفقات:** الـ visitor view ما بيعرضش روابط تحميل لغير الموردين المسجّلين. للوصول الرسمي
  والمستدام قدّم على الـ API الرسمي: `apiportal.etimad.sa`.
- **`tender_purpose`** بييجي من صفحة التفاصيل (موثوق)، **`place`** من الـ relations endpoint
  (بيـ rate-limit تحت الـ backfill — أعلى تغطية في الـ daily scraper).
- الـ Arabic labels للحقول في `DETAIL_FIELD_LABELS` (في `scrapers/parallel.py` و `scrapers/daily.py`)
  ممكن تحتاج تعديل لو اعتماد غيّرت الـ HTML.

## 🛣️ خطوات بعدية

1. **Notifications** — تنبيه العميل (email/WhatsApp) لما منافسة في قطاعه تطلع.
2. **بحث semantic** — `pgvector` على Supabase + embeddings.
3. **Dashboard** — Next.js + Supabase client.
4. **التحول للـ API الرسمي** — `apiportal.etimad.sa`.
