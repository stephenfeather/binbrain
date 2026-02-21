# QR Label Generator

This script generates a PDF of QR labels sized for 2" x 1" labels on letter paper.

## Install

```
pip install -r scripts/requirements.txt
```

## Examples

From a file of bin_ids:

```
python scripts/qr_labels.py \
  --input bins.txt \
  --out labels.pdf
```

From the database (uses `DATABASE_URL`):

```
DATABASE_URL=postgresql+psycopg://binbrain:***@127.0.0.1:5432/binbrain \
python scripts/qr_labels.py \
  --from-db \
  --out labels.pdf
```

## Layout Defaults (2" x 1")

- Label size: 2.0" x 1.0"
- Columns: 4
- Rows: 10
- Margins: 0.25" left/right, 0.5" top/bottom
- Gaps: 0.0"

Use `--label-width`, `--label-height`, `--columns`, `--rows`, `--margin-x`, `--margin-y`, `--gap-x`, `--gap-y` to override.
