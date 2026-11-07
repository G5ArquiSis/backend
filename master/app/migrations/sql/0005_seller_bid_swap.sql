ALTER TABLE seller_bids
    ADD COLUMN IF NOT EXISTS swap_id VARCHAR(128);

ALTER TABLE seller_bids
    ADD COLUMN IF NOT EXISTS swap_expires_at TIMESTAMPTZ;

ALTER TABLE seller_bids
    ADD COLUMN IF NOT EXISTS proposal_attempted_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_seller_bids_swap_id
    ON seller_bids (swap_id);
