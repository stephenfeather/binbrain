CREATE TABLE IF NOT EXISTS photo_detections (
  id bigserial PRIMARY KEY,
  photo_id bigint REFERENCES photos(photo_id) ON DELETE CASCADE,
  model text NOT NULL,
  label text NOT NULL,
  category text,
  confidence float NOT NULL,
  x1 float NOT NULL,
  y1 float NOT NULL,
  x2 float NOT NULL,
  y2 float NOT NULL,
  created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS photo_detection_groups (
  id bigserial PRIMARY KEY,
  photo_id bigint REFERENCES photos(photo_id) ON DELETE CASCADE,
  model text NOT NULL,
  label text NOT NULL,
  category text,
  confidence_avg float NOT NULL,
  count_estimate int NOT NULL,
  created_at timestamptz DEFAULT now()
);
