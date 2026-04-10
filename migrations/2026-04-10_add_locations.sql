-- Add locations lookup table and FK on bins

CREATE TABLE IF NOT EXISTS locations (
    location_id serial PRIMARY KEY,
    name        text NOT NULL,
    description text,
    parent_id   integer REFERENCES locations(location_id),
    deleted_at  timestamptz,
    created_at  timestamptz DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS locations_name_uq
ON locations (lower(trim(name)))
WHERE deleted_at IS NULL;

ALTER TABLE bins
ADD COLUMN IF NOT EXISTS location_id integer REFERENCES locations(location_id);
