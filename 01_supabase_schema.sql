-- =============================================================
-- Etimad Tenders — Supabase Schema
-- =============================================================
-- شغّل ده مرة واحدة في Supabase SQL Editor
-- Dashboard → SQL Editor → New Query → الصق ده → Run

-- Extensions لازم تتفعّل الأول (الـ trigram index بيعتمد عليها)
create extension if not exists pg_trgm;

-- جدول المنافسات الرئيسي
create table if not exists public.tenders (
    -- Primary key — معرّف داخلي عندنا
    id bigserial primary key,

    -- ده المعرّف الفريد بتاع اعتماد (من الـ URL: ?STenderId=xxxx)
    etimad_tender_id text not null unique,

    -- بيانات المنافسة الأساسية
    tender_name text not null,
    tender_number text,                    -- رقم المنافسة
    reference_number text,                 -- الرقم المرجعي
    agency_name text,                      -- الجهة الحكومية
    tender_type text,                      -- نوع المنافسة
    tender_purpose text,                   -- الغرض من المنافسة

    -- بيانات مالية وزمنية
    last_offer_date timestamptz,           -- آخر موعد لتقديم العروض
    last_enquiry_date timestamptz,         -- آخر موعد للاستفسارات
    offers_opening_date timestamptz,       -- موعد فتح المظاريف
    publish_date timestamptz,              -- تاريخ النشر

    condition_booklet_price numeric(15,2), -- ثمن كراسة الشروط
    invitation_cost numeric(15,2),         -- تكلفة الدعوة

    -- بيانات جغرافية
    place text,                            -- مكان التنفيذ
    branch_name text,                      -- اسم الفرع

    -- حالة المنافسة
    tender_status text,                    -- نشطة / مغلقة / إلخ
    submitting_method text,                -- طريقة التقديم

    -- روابط ومرفقات
    detail_url text,                       -- لينك صفحة التفاصيل في اعتماد
    has_attachments boolean default false,

    -- ميتاداتا للـ pipeline بتاعنا
    raw_data jsonb,                        -- الـ JSON الخام (لأي حقول مش في الـ schema)
    first_seen_at timestamptz default now() not null,
    last_seen_at timestamptz default now() not null,
    scraped_at timestamptz default now() not null,
    is_active boolean default true,        -- لو المنافسة لسه ظاهرة في الـ scrape الأخير

    -- آخر مرة عملنا فيها check على الترسية (الـ awards). بنستخدمها عشان مانعملش
    -- re-fetch للمنافسات اللي اعتمدت ترسيتها بس اعتماد لسه مانشرش بيانات المتقدمين.
    awards_last_checked timestamptz
);

-- للـ DBs الموجودة بالفعل (create table if not exists مش بيضيف الـ column ده):
alter table public.tenders add column if not exists awards_last_checked timestamptz;

-- Indexes للأداء
create index if not exists idx_tenders_status on public.tenders(tender_status);
create index if not exists idx_tenders_agency on public.tenders(agency_name);
create index if not exists idx_tenders_last_offer on public.tenders(last_offer_date);
create index if not exists idx_tenders_is_active on public.tenders(is_active);
create index if not exists idx_tenders_publish on public.tenders(publish_date desc);
-- Full-text search على العربي للبحث في الأسماء (بيعتمد على pg_trgm extension اللي اتعمل فوق)
create index if not exists idx_tenders_name_trgm on public.tenders using gin (tender_name gin_trgm_ops);

-- جدول للـ scrape runs (logging)
create table if not exists public.scrape_runs (
    id bigserial primary key,
    started_at timestamptz default now() not null,
    finished_at timestamptz,
    status text default 'running',         -- running / success / failed
    tenders_found int default 0,
    tenders_new int default 0,
    tenders_updated int default 0,
    tenders_deactivated int default 0,     -- منافسات اختفت من الـ listing
    error_message text,
    pages_scraped int default 0
);

-- Trigger يحدّث last_seen_at تلقائياً
create or replace function public.update_tender_last_seen()
returns trigger as $$
begin
    new.last_seen_at = now();
    return new;
end;
$$ language plpgsql;

-- View للمنافسات النشطة (بيستخدمها العملاء)
create or replace view public.active_tenders as
select
    etimad_tender_id,
    tender_name,
    tender_number,
    agency_name,
    tender_type,
    last_offer_date,
    place,
    condition_booklet_price,
    detail_url,
    publish_date,
    last_seen_at
from public.tenders
where is_active = true
  and (last_offer_date is null or last_offer_date > now())
order by last_offer_date asc nulls last;

-- Row Level Security — لو هتعرض الداتا في app للعملاء
-- alter table public.tenders enable row level security;
-- create policy "Anyone can read tenders" on public.tenders for select using (true);

-- =============================================================
-- Tender Awards — bidders + offers + winners for closed tenders
-- =============================================================
-- Sourced from /Tender/GetAwardingResultsForVisitorViewComponenet
-- for tenders with status "تم اعتماد الترسية".
create table if not exists public.tender_awards (
    id bigserial primary key,
    etimad_tender_id text not null references public.tenders(etimad_tender_id) on delete cascade,
    group_id integer not null,                       -- data-id من endpoint الـ Groups
    group_name text,                                 -- اسم الحزمة (مثلاً "حزمة افتراضية")
    bidder_name text not null,                       -- اسم المورد
    offer_value numeric(18,2),                       -- قيمة العرض المالي
    tech_evaluation text,                            -- "مطابق" / "غير مطابق" (للـ submitted فقط)
    award_value numeric(18,2),                       -- قيمة الترسية (للـ awarded فقط)
    role text not null check (role in ('submitted','awarded')),
    scraped_at timestamptz default now() not null,
    unique(etimad_tender_id, group_id, bidder_name, role)
);

create index if not exists idx_awards_tender on public.tender_awards(etimad_tender_id);
create index if not exists idx_awards_bidder on public.tender_awards(bidder_name);
create index if not exists idx_awards_role on public.tender_awards(role);

