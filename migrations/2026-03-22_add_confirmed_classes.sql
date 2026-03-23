-- Confirmed classes table for YOLO-World dynamic class vocabulary
CREATE TABLE IF NOT EXISTS confirmed_classes (
    id          bigserial PRIMARY KEY,
    class_name  text NOT NULL,
    category    text,
    source      text NOT NULL,
    confirmed_by text,
    confirmed_at timestamptz NOT NULL DEFAULT now(),
    removed_at   timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS confirmed_classes_name_uq
ON confirmed_classes (lower(trim(class_name)))
WHERE removed_at IS NULL;

-- Seed useful classes from COCO vocabulary
INSERT INTO confirmed_classes (class_name, category, source) VALUES
    ('scissors', 'tools', 'seed'),
    ('knife', 'tools', 'seed'),
    ('cell phone', 'electronics', 'seed'),
    ('laptop', 'electronics', 'seed'),
    ('remote', 'electronics', 'seed'),
    ('keyboard', 'electronics', 'seed'),
    ('mouse', 'electronics', 'seed'),
    ('bottle', 'household', 'seed'),
    ('cup', 'household', 'seed'),
    ('book', 'office', 'seed'),
    ('backpack', 'other', 'seed')
ON CONFLICT DO NOTHING;
