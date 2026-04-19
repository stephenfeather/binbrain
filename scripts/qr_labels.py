#!/usr/bin/env python3
import argparse
import csv
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

import qrcode
from qrcode.image.pil import PilImage
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


@dataclass
class Layout:
    label_width_in: float
    label_height_in: float
    columns: int
    rows: int
    margin_x_in: float
    margin_y_in: float
    gap_x_in: float
    gap_y_in: float


DEFAULT_LAYOUT = Layout(
    label_width_in=2.0,
    label_height_in=1.0,
    columns=4,
    rows=10,
    margin_x_in=0.25,
    margin_y_in=0.5,
    gap_x_in=0.0,
    gap_y_in=0.0,
)

FONT_NAME = "OpenSans-ExtraBold"
FONT_PATH = Path(__file__).parent / "fonts" / "OpenSans-ExtraBold.ttf"


def load_bin_ids_from_file(path: Path) -> List[str]:
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            return [row[0].strip() for row in reader if row and row[0].strip()]
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_bin_ids_from_db(database_url: str) -> List[str]:
    import psycopg

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT bin_id FROM bins WHERE deleted_at IS NULL ORDER BY bin_id"
            )
            return [row[0] for row in cur.fetchall()]


def qr_image(data: str, size_px: int) -> PilImage:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    return img.resize((size_px, size_px))


def iter_positions(layout: Layout):
    start_x = layout.margin_x_in * inch
    start_y = layout.margin_y_in * inch
    label_w = layout.label_width_in * inch
    label_h = layout.label_height_in * inch
    gap_x = layout.gap_x_in * inch
    gap_y = layout.gap_y_in * inch

    for row in range(layout.rows):
        for col in range(layout.columns):
            x = start_x + col * (label_w + gap_x)
            y = letter[1] - start_y - (row + 1) * label_h - row * gap_y
            yield x, y, label_w, label_h


def render_pdf(
    bin_ids: Iterable[str],
    out_path: Path,
    layout: Layout,
    qr_padding_in: float,
    single_per_page: bool,
) -> None:
    if FONT_PATH.exists():
        pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT_PATH)))

    positions = list(iter_positions(layout))
    if not positions:
        raise ValueError("layout has no positions")

    if single_per_page:
        page_w = layout.label_width_in * inch
        page_h = layout.label_height_in * inch
        mx = layout.margin_x_in * inch
        my = layout.margin_y_in * inch
        usable_w = page_w - 2 * mx
        usable_h = page_h - 2 * my
        if usable_w <= 0 or usable_h <= 0:
            raise ValueError(
                f"single-per-page margins ({layout.margin_x_in}in x, {layout.margin_y_in}in y) "
                f"exceed label size ({layout.label_width_in}in x {layout.label_height_in}in)"
            )
        qr_pad = qr_padding_in * inch
        if usable_w <= 2 * qr_pad or usable_h <= 2 * qr_pad:
            raise ValueError(
                f"single-per-page usable area after margins "
                f"({usable_w / inch:.3f}in x {usable_h / inch:.3f}in) "
                f"is too small for qr-padding ({qr_padding_in}in each side); "
                f"need at least {qr_padding_in * 2}in in each dimension"
            )
        positions = [(mx, my, usable_w, usable_h)]
        c = canvas.Canvas(str(out_path), pagesize=(page_w, page_h))
    else:
        c = canvas.Canvas(str(out_path), pagesize=letter)

    qr_padding = qr_padding_in * inch
    labels_per_page = len(positions)
    for idx, bin_id in enumerate(bin_ids):
        if idx and idx % labels_per_page == 0:
            c.showPage()
        pos_idx = idx % labels_per_page
        x, y, w, h = positions[pos_idx]

        qr_size = min(w, h) - 2 * qr_padding
        qr_px = max(64, int(qr_size))
        img = qr_image(bin_id, qr_px)
        img_reader = ImageReader(img)

        qr_center_x = x + w * 0.25
        text_center_x = x + w * 0.73
        center_y = y + h * 0.5

        qr_x = qr_center_x - (qr_size / 2)
        qr_y = center_y - (qr_size / 2)
        c.drawImage(
            img_reader,
            qr_x,
            qr_y,
            width=qr_size,
            height=qr_size,
            preserveAspectRatio=True,
            mask="auto",
        )
        font_name = FONT_NAME if FONT_PATH.exists() else "Helvetica"
        font_size = max(8, int(h * 0.25))
        max_text_width = w * 0.5
        text_width = pdfmetrics.stringWidth(bin_id, font_name, font_size)
        if text_width > max_text_width and text_width > 0:
            font_size = max(6, int(font_size * (max_text_width / text_width)))
        c.setFont(font_name, font_size)
        c.drawCentredString(text_center_x, center_y - (font_size * 0.35), bin_id)

    c.save()


def write_csv(bin_ids: Iterable[str], out_path: Path) -> None:
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for bin_id in bin_ids:
            writer.writerow([bin_id])


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate QR label PDFs for bins.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", help="Text or CSV file with bin_ids, one per line/row")
    src.add_argument(
        "--from-db", action="store_true", help="Load bin_ids from DATABASE_URL"
    )
    src.add_argument(
        "--sequential", action="store_true", help="Generate sequential bin_ids"
    )

    p.add_argument("--out", required=True, help="Output PDF path")
    p.add_argument("--csv", help="Optional CSV output path")

    p.add_argument("--label-width", type=float, default=DEFAULT_LAYOUT.label_width_in)
    p.add_argument("--label-height", type=float, default=DEFAULT_LAYOUT.label_height_in)
    p.add_argument("--columns", type=int, default=DEFAULT_LAYOUT.columns)
    p.add_argument("--rows", type=int, default=DEFAULT_LAYOUT.rows)
    p.add_argument("--margin-x", type=float, default=DEFAULT_LAYOUT.margin_x_in)
    p.add_argument("--margin-y", type=float, default=DEFAULT_LAYOUT.margin_y_in)
    p.add_argument("--gap-x", type=float, default=DEFAULT_LAYOUT.gap_x_in)
    p.add_argument("--gap-y", type=float, default=DEFAULT_LAYOUT.gap_y_in)
    p.add_argument(
        "--qr-padding", type=float, default=0.08, help="Padding inside label in inches"
    )
    p.add_argument(
        "--start", type=int, default=1, help="Start number for sequential bin_ids"
    )
    p.add_argument("--count", type=int, help="How many sequential bin_ids to generate")
    p.add_argument(
        "--end", type=int, help="End number (inclusive) for sequential bin_ids"
    )
    p.add_argument("--prefix", default="BIN-", help="Prefix for sequential bin_ids")
    p.add_argument(
        "--pad", type=int, default=4, help="Zero padding width for sequential bin_ids"
    )
    p.add_argument(
        "--single-per-page", action="store_true", help="Render one label per page"
    )
    p.add_argument(
        "--label-printer",
        action="store_true",
        help=(
            "Preset for label printers: 2x1in page, 0.15in margins all sides, "
            "one label per page. Implies --single-per-page. Uses preset values "
            "for --label-width, --label-height, --margin-x, and --margin-y "
            "unless you explicitly provide those options."
        ),
    )

    args = p.parse_args()
    if args.label_printer:
        # Preset — detect what's still at its default so explicit overrides win.
        if args.label_width == DEFAULT_LAYOUT.label_width_in:
            args.label_width = 2.0
        if args.label_height == DEFAULT_LAYOUT.label_height_in:
            args.label_height = 1.0
        if args.margin_x == DEFAULT_LAYOUT.margin_x_in:
            args.margin_x = 0.15
        if args.margin_y == DEFAULT_LAYOUT.margin_y_in:
            args.margin_y = 0.15
        args.single_per_page = True
    return args


def main() -> None:
    args = parse_args()

    if args.sequential:
        if args.count is None and args.end is None:
            raise SystemExit("--sequential requires --count or --end")
        if args.count is not None and args.end is not None:
            raise SystemExit("Use only one of --count or --end")
        if args.end is not None and args.end < args.start:
            raise SystemExit("--end must be >= --start")

        if args.count is not None:
            end = args.start + args.count - 1
        else:
            end = args.end
        bin_ids = [
            f"{args.prefix}{str(i).zfill(args.pad)}" for i in range(args.start, end + 1)
        ]
    elif args.from_db:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise SystemExit("DATABASE_URL is required with --from-db")
        bin_ids = load_bin_ids_from_db(database_url)
    else:
        bin_ids = load_bin_ids_from_file(Path(args.input))

    if not bin_ids:
        raise SystemExit("No bin_ids found")

    layout = Layout(
        label_width_in=args.label_width,
        label_height_in=args.label_height,
        columns=args.columns,
        rows=args.rows,
        margin_x_in=args.margin_x,
        margin_y_in=args.margin_y,
        gap_x_in=args.gap_x,
        gap_y_in=args.gap_y,
    )

    render_pdf(
        bin_ids,
        Path(args.out),
        layout,
        qr_padding_in=args.qr_padding,
        single_per_page=args.single_per_page,
    )

    if args.csv:
        write_csv(bin_ids, Path(args.csv))


if __name__ == "__main__":
    main()
