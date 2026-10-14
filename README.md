# Synthetic PII OCR Pilot

**Live page:** https://ibrahim77gh.github.io/synthetic-pii-ocr-pilot/

A small, reproducible pipeline that generates a synthetic document OCR dataset (forms, invoices and ID-style cards) with a bounding box on every personal-data field, plus a validator that re-checks the output from disk.

The live page shows six of the generated pages with their stored labels drawn on top, the QC results, the label mapping to Presidio, and the design for a matching speech dataset.

## Results

Seed `1234`, 100 pages per template:

| | |
|---|---|
| Documents | 300 (form, invoice, ID-style card) |
| Locales | 6 (en_US, en_GB, de_DE, fr_FR, it_IT, nl_NL) |
| Labelled PII fields | 2,474 across 12 labels |
| Box vs. rendered ink check | 2,474 / 2,474 |
| Validator | 9 checks, 0 failures |
| Reruns with the same seed | byte-identical (303 of 303 files) |
| Runtime | about 1 min 30 s on a laptop CPU |

## Quick start

```bash
pip install -r requirements.txt
python generate.py --out data --per-template 100 --seed 1234
python validate.py --data data     # writes data/qc_report.json, exits 1 on any failure
python build_page.py               # optional: rebuilds docs/index.html and samples/
```

Fonts are read from `C:\Windows\Fonts`. On Linux, point `FONT_DIR` and `FONTS` in `generate.py` at OFL fonts such as Noto or Liberation. Byte-identical reruns hold for the same fonts and the pinned library versions.

## Output

| File | Contents |
|---|---|
| `data/images/*.jpg` | Rendered pages |
| `data/metadata.jsonl` | One record per page: `file_name`, `doc_type`, `template`, `locale`, size, `split`, `person_id`, render settings, provenance, `entities` |
| `data/entities.parquet` | One row per field: `doc_id, doc_type, locale, split, label, text, presidio, x0, y0, x1, y1, key_text` |
| `data/generation_log.json` | Generation counts and versions |
| `data/qc_report.json` | Validator report: counts by type, locale, split and label, plus every failure with examples |

Each entity stores `label`, `text`, `bbox` (axis-aligned, pixels), `quad` (four corners after page rotation), `key_text` and `key_bbox` (the printed field label, for key–value linking) and its `presidio` entity. Dates also store `normalized` (ISO) and `source_format`.

`samples/` holds six real pages from this run with their records, the QC report and the generation log.

## Checks

During generation:

1. **Text sits inside its box.** Every value is also drawn on a hidden index mask that goes through the same rotation as the page. Each field's real ink extent must sit inside its stored box, within 2 px.

In `validate.py`, from disk only:

2. Files exist and image sizes match the records.
3. Boxes are in bounds and at least 3 px wide and tall.
4. No overlapping PII boxes. Page rotation is read from the quads and undone first.
5. Values match their label: IBAN mod-97, card Luhn, dates parse back to their ISO value, SSN and ID patterns, phone digit count, no digits in names.
6. Text is still visible inside each box after blur, grain and JPEG.
7. No duplicate images (SHA-256).
8. No duplicate identities (`person_id`).
9. Unique fields (email, phone, ID, SSN, IBAN, card) never repeat, and no value appears in two splits. Splits are assigned by hashing the identity.

## Reproducibility notes

Two fixes were needed for byte-identical reruns:

- Pillow's `Image.effect_noise` uses its own random generator, which the seed doesn't control. Grain is drawn from the seeded generator instead.
- Faker builds its Italian city list from a Python `set`, so its order follows `PYTHONHASHSEED`. `generate.py` sorts that list at import.

## Licences and provenance

- Code: MIT (see `LICENSE`).
- Values come from Faker (MIT). Faker's Italian place names are converted from GeoNames postal data (CC BY 4.0), which needs attribution in a public dataset release.
- The pilot renders with Windows system fonts. A public dataset release should render with OFL-licensed fonts.
- Every record stores the generator version, seed, and Faker and Pillow versions.
- All names, numbers and addresses are synthetic.
