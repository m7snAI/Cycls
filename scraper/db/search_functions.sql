-- =============================================================
-- Etimad — Smart search (trigram, multi-field, relevance-ranked)
-- =============================================================
-- Run ONCE in the Supabase SQL Editor, AFTER db/schema.sql.
-- Powers the agent's tender_search / tender_lookup / award_comps tools through
-- PostgREST RPC:  POST /rest/v1/rpc/search_tenders
--
-- Why: the tools used `ilike '%kw%'` (exact substring), which misses Arabic
-- word-forms and word order (e.g. "هوية بصرية" never matched "الهويات البصرية").
-- Trigram similarity (pg_trgm) handles that; this function ranks by it.

create extension if not exists pg_trgm;

-- Trigram + plain indexes so every WHERE branch below is index-accelerated.
-- (tender_name already has idx_tenders_name_trgm from schema.sql.)
create index if not exists idx_tenders_agency_trgm
    on public.tenders using gin (agency_name gin_trgm_ops);
create index if not exists idx_tenders_purpose_trgm
    on public.tenders using gin (tender_purpose gin_trgm_ops);
create index if not exists idx_tenders_number  on public.tenders(tender_number);
create index if not exists idx_tenders_refnum  on public.tenders(reference_number);

-- One ranked search for both browse (only_active=true) and lookup (false).
create or replace function public.search_tenders(
    q           text,
    only_active boolean default true,
    city        text default null,
    agency      text default null,
    max_rows    int  default 10
)
returns table (
    etimad_tender_id        text,
    tender_name             text,
    tender_number           text,
    reference_number        text,
    agency_name             text,
    tender_type             text,
    tender_purpose          text,
    place                   text,
    branch_name             text,
    publish_date            timestamptz,
    last_offer_date         timestamptz,
    last_enquiry_date       timestamptz,
    offers_opening_date     timestamptz,
    condition_booklet_price numeric,
    tender_status           text,
    is_active               boolean,
    has_attachments         boolean,
    detail_url              text,
    score                   real
)
language plpgsql
stable
as $$
begin
    -- Generous fuzzy threshold for short Arabic queries; the LIMIT + ranking keep
    -- precision. Transaction-local, so it never leaks to other queries.
    perform set_config('pg_trgm.word_similarity_threshold', '0.25', true);
    q      := btrim(coalesce(q, ''));
    city   := btrim(coalesce(city, ''));
    agency := btrim(coalesce(agency, ''));

    return query
    select
        t.etimad_tender_id, t.tender_name, t.tender_number, t.reference_number,
        t.agency_name, t.tender_type, t.tender_purpose, t.place, t.branch_name,
        t.publish_date, t.last_offer_date, t.last_enquiry_date, t.offers_opening_date,
        t.condition_booklet_price, t.tender_status, t.is_active, t.has_attachments,
        t.detail_url,
        greatest(
            word_similarity(q, t.tender_name),
            0.6 * word_similarity(q, coalesce(t.agency_name, '')),
            0.5 * word_similarity(q, coalesce(t.tender_purpose, ''))
        )::real as score
    from public.tenders t
    where
        (not only_active
         or (t.is_active and (t.last_offer_date is null or t.last_offer_date > now())))
        and (agency = '' or t.agency_name ilike '%' || agency || '%')
        and (
            q = ''
            or q <% t.tender_name                       -- fuzzy trigram (indexed)
            or t.tender_name    ilike '%' || q || '%'   -- substring (trigram-indexed)
            or t.agency_name    ilike '%' || q || '%'
            or t.tender_purpose ilike '%' || q || '%'
            or t.tender_number    = q
            or t.reference_number = q
        )
    order by
        case
            when city <> '' and t.place ilike '%' || city || '%' then 0  -- same city
            when coalesce(t.place, '') = ''                       then 1  -- unknown location
            else 2                                                        -- other city
        end,
        score desc,
        t.last_offer_date asc nulls last
    limit greatest(1, least(max_rows, 100));
end;
$$;

grant execute on function public.search_tenders(text, boolean, text, text, int)
    to anon, authenticated, service_role;
