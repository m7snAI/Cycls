-- =============================================================
-- Etimad — Smart search (trigram, multi-field, relevance-ranked)
-- =============================================================
-- Run in the Supabase SQL Editor, AFTER db/schema.sql. Idempotent — safe to
-- re-run after edits.
-- Powers the agent's tender_search / tender_lookup / award_comps tools through
-- PostgREST RPC:  POST /rest/v1/rpc/search_tenders
--
-- Why: the tools used `ilike '%kw%'` (exact substring), which misses Arabic
-- word-forms and word order (e.g. "هوية بصرية" never matched "الهويات البصرية").
-- Trigram similarity (pg_trgm) handles that; this function ranks by it.

create extension if not exists pg_trgm;

-- Trigram + plain indexes so every WHERE branch is index-accelerated.
-- (tender_name already has idx_tenders_name_trgm from schema.sql.)
create index if not exists idx_tenders_agency_trgm
    on public.tenders using gin (agency_name gin_trgm_ops);
create index if not exists idx_tenders_number on public.tenders(tender_number);
create index if not exists idx_tenders_refnum on public.tenders(reference_number);
-- tender_purpose is long free text — matching/scoring on it was the main cause
-- of statement timeouts on broad queries, so it's no longer used. Drop its index.
drop index if exists idx_tenders_purpose_trgm;

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
    -- Fuzzy but selective enough to stay fast on broad queries; ranking + LIMIT
    -- keep precision. Transaction-local, so it never leaks to other queries.
    perform set_config('pg_trgm.word_similarity_threshold', '0.35', true);
    q      := btrim(coalesce(q, ''));
    city   := btrim(coalesce(city, ''));
    agency := btrim(coalesce(agency, ''));

    return query
    with cand as (
        -- Index-accelerated candidate set, hard-capped so a very broad query
        -- can never run away (statement-timeout guard). Selective queries match
        -- far fewer than the cap, so quality is unaffected for real lookups.
        select t.*
        from public.tenders t
        where
            (not only_active
             or (t.is_active and (t.last_offer_date is null or t.last_offer_date > now())))
            and (agency = '' or t.agency_name ilike '%' || agency || '%')
            and (
                q = ''
                or q <% t.tender_name                       -- fuzzy trigram (indexed)
                or t.tender_name ilike '%' || q || '%'      -- substring (trigram-indexed)
                or t.agency_name ilike '%' || q || '%'
                or t.tender_number    = q
                or t.reference_number = q
            )
        limit 4000
    )
    select
        c.etimad_tender_id, c.tender_name, c.tender_number, c.reference_number,
        c.agency_name, c.tender_type, c.tender_purpose, c.place, c.branch_name,
        c.publish_date, c.last_offer_date, c.last_enquiry_date, c.offers_opening_date,
        c.condition_booklet_price, c.tender_status, c.is_active, c.has_attachments,
        c.detail_url,
        greatest(
            word_similarity(q, c.tender_name),
            0.6 * word_similarity(q, coalesce(c.agency_name, ''))
        )::real as score
    from cand c
    order by
        case
            when city <> '' and c.place ilike '%' || city || '%' then 0  -- same city
            when coalesce(c.place, '') = ''                       then 1  -- unknown location
            else 2                                                        -- other city
        end,
        score desc,
        c.last_offer_date asc nulls last
    limit greatest(1, least(max_rows, 100));
end;
$$;

grant execute on function public.search_tenders(text, boolean, text, text, int)
    to anon, authenticated, service_role;
