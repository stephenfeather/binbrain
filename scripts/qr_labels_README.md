# QR Label Generator

This script generates a PDF of QR labels sized for 2" x 1" labels on letter paper.

## Install (uv)

```
cd api
uv sync --extra scripts
```

Font:
- `scripts/fonts/OpenSans-ExtraBold.ttf` is used if present. If missing, the script falls back to Helvetica.

## Examples

From a file of bin_ids:

```
uv run python scripts/qr_labels.py \
  --input bins.txt \
  --out labels.pdf
```

From the database (uses `DATABASE_URL`):

```
DATABASE_URL=postgresql+psycopg://binbrain:***@127.0.0.1:5432/binbrain \
uv run python scripts/qr_labels.py \
  --from-db \
  --out labels.pdf
```

Sequential bin_ids:

```
uv run python scripts/qr_labels.py \
  --sequential \
  --start 1 \
  --count 120 \
  --out labels.pdf
```

This generates `BIN-0001` through `BIN-0120` by default. Use `--prefix` and `--pad` to customize.

Label printers (one label per page):

```
uv run python scripts/qr_labels.py \
  --sequential \
  --start 1 \
  --count 120 \
  --out labels.pdf \
  --single-per-page
```

Label printer preset (2" x 1" page, 0.15" margins all sides, one label per
page — ready to feed directly to a thermal label printer):

```
uv run python scripts/qr_labels.py \
  --sequential \
  --start 1 \
  --count 120 \
  --out labels.pdf \
  --label-printer
```

`--label-printer` implies `--single-per-page` and sets label size to 2"x1"
with 0.15" margins on all sides. Explicit `--label-width`, `--label-height`,
`--margin-x`, `--margin-y` override the preset when passed.

Note: `--single-per-page` now honors `--margin-x` / `--margin-y` (previously
hard-coded to 0). Pass `--margin-x 0 --margin-y 0` if you need full-bleed
labels.

## Layout Defaults (2" x 1")

- Label size: 2.0" x 1.0"
- Columns: 4
- Rows: 10
- Margins: 0.25" left/right, 0.5" top/bottom
- Gaps: 0.0"

Use `--label-width`, `--label-height`, `--columns`, `--rows`, `--margin-x`, `--margin-y`, `--gap-x`, `--gap-y` to override.
