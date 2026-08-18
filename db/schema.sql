-- Review/Approval workflow persistence
-- Temporal owns the live state machine; this table is the durable,
-- queryable record of every request (for listing, reporting, audit).

CREATE TABLE IF NOT EXISTS review_requests (
    id                TEXT PRIMARY KEY,           -- request_id (uuid4 string)
    -- Temporal workflow id. Nullable: set back to NULL if a Temporal admin
    -- deletes the workflow execution entirely (`temporal workflow delete`)
    -- while this row is still PENDING_REVIEW -- see service.py's
    -- _reconcile_missing_workflow. There is nothing left in Temporal for
    -- this id to point at once that happens.
    workflow_id       TEXT UNIQUE,
    review_type       TEXT NOT NULL,               -- discriminator: "purchase_order", "leave_request", ...
    payload           JSONB NOT NULL,              -- type-specific payload, already validated by BFF
    requester         TEXT NOT NULL,               -- Keycloak username of the Operator
    -- PENDING_REVIEW | APPROVED | REJECTED | CANCELLED
    status            TEXT NOT NULL DEFAULT 'PENDING_REVIEW',
    -- All three terminal outcomes are "closed" in the same sense: someone
    -- (a Manager approving/rejecting, or the requester cancelling their
    -- own request) made it final. These three columns are shared across
    -- all of them rather than being decision-specific:
    closed_status     TEXT,                        -- APPROVED | REJECTED | CANCELLED
    closed_by         TEXT,                        -- Manager username (approve/reject), or the
                                                     -- requester's own username (cancel)
    closed_comment    TEXT,                        -- optional note from whoever closed it
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Set on ANY terminal transition, so (closed_at - created_at) always
    -- gives a duration for a resolved request, matching the start->close
    -- time the Temporal Web UI shows for that workflow.
    closed_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_review_requests_status ON review_requests (status);
CREATE INDEX IF NOT EXISTS idx_review_requests_type ON review_requests (review_type);
-- Both support list_reviews_page()'s ORDER BY created_at DESC, the
-- first indexed (requester-filtered, i.e. the operator screens) and
-- the second unindexed (unfiltered, i.e. the manager screens).
CREATE INDEX IF NOT EXISTS idx_review_requests_requester_created_at
    ON review_requests (requester, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_review_requests_created_at
    ON review_requests (created_at DESC);
