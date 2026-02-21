ALTER TABLE bins
  ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

ALTER TABLE items
  ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
