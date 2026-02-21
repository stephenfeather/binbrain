CREATE TABLE IF NOT EXISTS photo_labels (
  id bigserial PRIMARY KEY,
  photo_id bigint REFERENCES photos(photo_id) ON DELETE CASCADE,
  model text NOT NULL,
  label text NOT NULL,
  confidence float NOT NULL,
  created_at timestamptz DEFAULT now()
);
