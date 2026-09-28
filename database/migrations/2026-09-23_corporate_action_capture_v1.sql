-- Persist immutable corporate-action acquisition attempts and their evidence.
-- Role grants are intentionally excluded pending a separate deployment decision.

BEGIN;

CREATE TABLE public.corporate_action_captures (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    capture_uid UUID NOT NULL UNIQUE,
    company_id BIGINT NOT NULL REFERENCES public.companies (id) ON DELETE RESTRICT,
    provider TEXT NOT NULL,
    source_variant TEXT NOT NULL,
    capture_contract_version TEXT NOT NULL,
    requested_window_start_at TIMESTAMPTZ NOT NULL,
    requested_window_end_at TIMESTAMPTZ,
    capture_started_at TIMESTAMPTZ NOT NULL,
    capture_completed_at TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    source_available_at TIMESTAMPTZ,
    acquisition_status TEXT NOT NULL,
    completeness_status TEXT NOT NULL,
    provider_capture_id TEXT,
    provider_revision_id TEXT,
    provider_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_code TEXT,
    error_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    response_fingerprint_version TEXT,
    response_fingerprint TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT corporate_action_captures_source_check CHECK (
        provider = 'YAHOO_FINANCE'
        AND source_variant = 'yfinance.splits'
    ),
    CONSTRAINT corporate_action_captures_contract_check CHECK (
        capture_contract_version = 'legacy-split-capture-v1'
    ),
    CONSTRAINT corporate_action_captures_status_check CHECK (
        acquisition_status IN ('SUCCESS', 'PARTIAL', 'FAILED', 'UNKNOWN')
        AND completeness_status IN ('COMPLETE', 'INCOMPLETE', 'UNKNOWN')
    ),
    CONSTRAINT corporate_action_captures_time_check CHECK (
        capture_started_at <= capture_completed_at
        AND observed_at = capture_completed_at
        AND (source_available_at IS NULL OR source_available_at <= observed_at)
    ),
    CONSTRAINT corporate_action_captures_window_check CHECK (
        requested_window_end_at IS NULL
        AND requested_window_start_at = capture_started_at - INTERVAL '6 years'
    ),
    CONSTRAINT corporate_action_captures_json_check CHECK (
        jsonb_typeof(provider_metadata) = 'object'
        AND jsonb_typeof(error_metadata) = 'object'
    ),
    CONSTRAINT corporate_action_captures_error_code_check CHECK (
        error_code IS NULL OR error_code ~ '^[A-Z][A-Z0-9_]{0,63}$'
    ),
    CONSTRAINT corporate_action_captures_outcome_check CHECK (
        (
            acquisition_status = 'SUCCESS'
            AND completeness_status = 'COMPLETE'
            AND error_code IS NULL
            AND response_fingerprint_version IS NOT NULL
            AND response_fingerprint IS NOT NULL
            AND response_fingerprint_version = 'corporate-action-response-sha256-v2'
            AND response_fingerprint ~ '^[0-9a-f]{64}$'
        )
        OR (
            acquisition_status = 'PARTIAL'
            AND completeness_status = 'INCOMPLETE'
            AND response_fingerprint_version IS NOT NULL
            AND response_fingerprint IS NOT NULL
            AND response_fingerprint_version = 'corporate-action-response-sha256-v2'
            AND response_fingerprint ~ '^[0-9a-f]{64}$'
        )
        OR (
            acquisition_status IN ('FAILED', 'UNKNOWN')
            AND completeness_status IN ('INCOMPLETE', 'UNKNOWN')
            AND error_code IS NOT NULL
            AND response_fingerprint_version IS NULL
            AND response_fingerprint IS NULL
        )
    )
);

CREATE TABLE public.corporate_action_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    capture_id BIGINT NOT NULL REFERENCES public.corporate_action_captures (id) ON DELETE RESTRICT,
    event_ordinal INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    event_date DATE NOT NULL,
    split_ratio NUMERIC NOT NULL,
    provider_event_id TEXT,
    provider_revision_id TEXT,
    provider_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT corporate_action_events_ordinal_check CHECK (event_ordinal >= 0),
    CONSTRAINT corporate_action_events_type_check CHECK (action_type = 'STOCK_SPLIT'),
    CONSTRAINT corporate_action_events_ratio_check CHECK (
        split_ratio > 0
        AND split_ratio < 'Infinity'::numeric
        AND split_ratio <> 'NaN'::numeric
    ),
    CONSTRAINT corporate_action_events_json_check CHECK (
        jsonb_typeof(provider_metadata) = 'object'
    ),
    UNIQUE (capture_id, event_ordinal)
);

CREATE INDEX corporate_action_captures_point_in_time_idx
    ON public.corporate_action_captures (
        company_id, source_variant, capture_contract_version, observed_at DESC, id DESC
    );

CREATE INDEX corporate_action_captures_provider_revision_idx
    ON public.corporate_action_captures (provider, provider_capture_id, provider_revision_id)
    WHERE provider_capture_id IS NOT NULL;

CREATE INDEX corporate_action_events_capture_date_idx
    ON public.corporate_action_events (capture_id, event_date, event_ordinal);

COMMIT;
