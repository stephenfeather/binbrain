-- Add API key authentication table
CREATE TABLE IF NOT EXISTS api_keys (
    id          bigserial PRIMARY KEY,
    key_hash    text NOT NULL UNIQUE,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz,
    last_used   timestamptz
);

CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
