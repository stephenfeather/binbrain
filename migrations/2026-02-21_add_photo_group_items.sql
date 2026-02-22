CREATE TABLE IF NOT EXISTS photo_group_items (
  id bigserial PRIMARY KEY,
  photo_id bigint REFERENCES photos(photo_id) ON DELETE CASCADE,
  model text NOT NULL,
  label text NOT NULL,
  category text,
  item_id bigint REFERENCES items(item_id) ON DELETE CASCADE,
  created_at timestamptz DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS photo_group_items_uq
ON photo_group_items (photo_id, model, label, category, item_id);
