# 🏛️ Etimad Tenders Scraper → Supabase

سحب يومي لمنافسات اعتماد النشطة وتخزينها على Supabase.

## ⚠️ مهم تقراه قبل ما تبدأ

1. **الكود ده MVP للتجربة السريعة.** قبل ما تبيع المنتج لعملاء، راجع شروط استخدام اعتماد ولو نجح المنتج، قدّم على الـ API الرسمي من `apiportal.etimad.sa`.

2. **الـ HTML selectors في الـ scraper تخمين.** أول مرة تشغّله، افتح `tenders.etimad.sa/Tender/AllTendersForVisitor` في المتصفح، اضغط F12، شوف الـ class names الفعلية، وعدّل `extract_tender_from_card()` في `scraper.py`. مفيش بديل عن ده.

3. **متشغّلش بـ rate عالي.** الـ delays في الكود معمولة عشان كده، متعدّلهاش.

## 🚀 خطوات الـ Setup

### 1) أنشئ Supabase project
- روح [supabase.com](https://supabase.com) وأنشئ project
- من `Settings → API` انسخ:
  - `Project URL` → `SUPABASE_URL`
  - `service_role` key (مش الـ anon) → `SUPABASE_SERVICE_KEY`
  - ⚠️ الـ service_role key بيتجاوز الـ RLS، احفظه في secrets بس

### 2) أنشئ الجداول
- في Supabase Dashboard → SQL Editor → New Query
- الصق محتوى `01_supabase_schema.sql` و اضغط Run

### 3) اختبر محلياً
```bash
pip install -r requirements.txt

export SUPABASE_URL="https://xxxxx.supabase.co"
export SUPABASE_SERVICE_KEY="eyJ..."

python scraper.py
```

شغّله، شوف الـ logs، وافتح Supabase → Table Editor → `tenders` وشوف الداتا. 

**لو الجدول فاضي أو الأسماء غلط:** الـ selectors محتاجة تعديل. افتح صفحة المنافسات في المتصفح، اعمل inspect على card واحد، وعدّل الـ CSS selectors في `extract_tender_from_card()`.

### 4) Deploy على GitHub Actions
```bash
# في الـ repo بتاعك:
mkdir -p .github/workflows
cp 04_github_action.yml .github/workflows/scrape.yml
cp 02_scraper.py scraper.py
cp 03_requirements.txt requirements.txt
git add . && git commit -m "Add etimad scraper" && git push
```

ثم في GitHub:
- Repo Settings → Secrets and variables → Actions → New repository secret
- أضف: `SUPABASE_URL` و `SUPABASE_SERVICE_KEY`
- Actions tab → Daily Etimad Scrape → Run workflow (لاختبار أول مرة)

بعد كده الـ scrape هيشتغل تلقائي كل يوم 6 صباحاً بتوقيت السعودية.

## 📊 إزاي تستخدم الداتا في منتجك

### query للمنافسات النشطة
```sql
select * from active_tenders
where last_offer_date > now()
order by last_offer_date asc
limit 50;
```

### بحث نصي بالعربي
```sql
select tender_name, agency_name, last_offer_date
from tenders
where tender_name % 'صيانة'  -- trigram similarity
  and is_active = true
order by similarity(tender_name, 'صيانة') desc
limit 20;
```

### إحصائيات حسب الجهة
```sql
select agency_name, count(*) as active_count
from tenders
where is_active = true
group by agency_name
order by active_count desc;
```

### تتبع تاريخ الـ scrapes
```sql
select * from scrape_runs order by started_at desc limit 10;
```

## 🔧 مشاكل شائعة

| المشكلة | الحل |
|---------|------|
| `0 tenders found` | الـ HTML structure اتغيّر. افتح الصفحة في browser، اعمل inspect، عدّل الـ selectors |
| `HTTP 403` أو CAPTCHA | الـ IP اتبلّك. جرّب من جهاز تاني أو استخدم proxy. GitHub Actions IPs عادة شغّالة |
| `Connection timeout` | السيرفر بطيء. الـ retry logic بيتعامل مع ده، بس لو استمر، زوّد `TIMEOUT` |
| داتا ناقصة في بعض الحقول | طبيعي — مش كل المنافسات بتنشر كل البيانات. الـ `raw_data` field فيه الـ HTML الكامل |

## 📄 سحب التفاصيل

الـ scraper بيمشي على مرحلتين:

1. **Listing pass** — بياخد الـ cards من صفحة `/AllTendersForVisitor` (اسم، جهة، تاريخ آخر تقديم، إلخ).
2. **Details pass** — لكل منافسة جديدة أو لسه ما اتسحبتش تفاصيلها (يعني `reference_number IS NULL`)، بيفتح صفحة `/Tender/DetailsForVisitor?STenderId=…` وبيعبّي:
   - `tender_purpose`, `submitting_method`, `tender_status`
   - أي حقل ناقص من الـ listing (مثلاً `condition_booklet_price` لما الـ listing بيرجّعه 0)

> **ملاحظة عن المرفقات:** الـ visitor view ما بيعرضش روابط تحميل لأي حد مش مسجّل كمورد. الـ endpoint `/Tender/GetAttachmentsViewComponenet` بيرجّع fragment فاضي دايماً. لو محتاج المرفقات، الحل الصحيح هو الـ API الرسمي على `apiportal.etimad.sa`.

⚠️ الـ Arabic labels في `DETAIL_FIELD_LABELS` (في `scraper.py`) تخمين زي الـ listing selectors. لو شفت حقول فاضية:
- بُص في `raw_data.detail_html_snippet` لأي صف، هتلاقي أول 5KB من الـ HTML.
- قارن الـ label فيه مع المفاتيح في `DETAIL_FIELD_LABELS` وعدّل.

المنطق ده بيمنع إعادة سحب تفاصيل ثابتة كل يوم — أول run بيتسحب كل اللي ناقص، وبعدها بس الجديد.

## 🛣️ خطوات بعدية لو المنتج نجح

1. **Notifications** — لما منافسة جديدة في قطاع العميل تطلع، ابعتله email/WhatsApp
2. **بحث متقدم بالـ embeddings** — استخدم `pgvector` على Supabase + OpenAI embeddings للبحث semantically
3. **Dashboard** — Next.js + Supabase client → عرض الداتا للعملاء
4. **التحول للـ API الرسمي** — قدّم على `apiportal.etimad.sa` للوصول الرسمي والمستدام
