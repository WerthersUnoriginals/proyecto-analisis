-- N evidence (spec 2026-10-03-new-highs-n-v1 §4): literal Yahoo daily bars and
-- the literal 8-K items of each EDGAR filing.
-- Additive only. Both tables are append-only.

BEGIN;

-- Each bar is on the split basis of its observation date: Yahoo has no
-- unadjusted history. A bar is stored again only when its values change.
CREATE TABLE public.yahoo_price_bars_raw (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES public.companies (id) ON DELETE RESTRICT,
    ticker TEXT NOT NULL,
    bar_date DATE NOT NULL,
    open NUMERIC(38, 10) NOT NULL,
    high NUMERIC(38, 10) NOT NULL,
    low NUMERIC(38, 10) NOT NULL,
    close NUMERIC(38, 10) NOT NULL,
    adj_close NUMERIC(38, 10) NOT NULL,
    volume NUMERIC(38, 0) NOT NULL,
    currency TEXT NOT NULL,
    exchange_timezone TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    run_id BIGINT NOT NULL REFERENCES public.ingestion_runs (id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT yahoo_price_bars_raw_ohlc_check CHECK (
        low > 0 AND low <= open AND low <= close AND open <= high AND close <= high AND adj_close > 0
    ),
    CONSTRAINT yahoo_price_bars_raw_volume_check CHECK (volume >= 0),
    CONSTRAINT yahoo_price_bars_raw_final_check CHECK (bar_date <= (observed_at AT TIME ZONE exchange_timezone)::date)
);

CREATE UNIQUE INDEX yahoo_price_bars_raw_identity_unique
    ON public.yahoo_price_bars_raw (company_id, bar_date, observed_at);

CREATE INDEX yahoo_price_bars_raw_company_observed_idx
    ON public.yahoo_price_bars_raw (company_id, observed_at);

CREATE TABLE public.sec_filing_items (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES public.companies (id) ON DELETE RESTRICT,
    accession TEXT NOT NULL,
    item TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    run_id BIGINT NOT NULL REFERENCES public.ingestion_runs (id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- Literal: pre-2004 8-Ks use single-digit items ("5", "7").
    CONSTRAINT sec_filing_items_item_check CHECK (item <> '' AND item = btrim(item)),
    CONSTRAINT sec_filing_items_filing_fk FOREIGN KEY (company_id, accession)
        REFERENCES public.sec_filings (company_id, accession) ON DELETE RESTRICT
);

-- An accession's items are immutable: the first observation is kept.
CREATE UNIQUE INDEX sec_filing_items_identity_unique
    ON public.sec_filing_items (company_id, accession, item);

CREATE TRIGGER yahoo_price_bars_raw_append_only
    BEFORE UPDATE OR DELETE ON public.yahoo_price_bars_raw
    FOR EACH ROW EXECUTE FUNCTION public.reject_evidence_mutation();
CREATE TRIGGER sec_filing_items_append_only
    BEFORE UPDATE OR DELETE ON public.sec_filing_items
    FOR EACH ROW EXECUTE FUNCTION public.reject_evidence_mutation();

COMMIT;
