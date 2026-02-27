-- Add UPC barcode column to items table
-- Partial unique index ensures NULL UPCs never conflict with each other

ALTER TABLE items
  ADD COLUMN IF NOT EXISTS upc text;

CREATE UNIQUE INDEX IF NOT EXISTS items_upc_uq
  ON items (upc)
  WHERE upc IS NOT NULL;

CREATE INDEX IF NOT EXISTS items_upc_idx
  ON items (upc)
  WHERE upc IS NOT NULL AND deleted_at IS NULL;
