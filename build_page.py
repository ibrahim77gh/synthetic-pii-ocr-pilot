"""Build the showcase page and sample folder from a generated dataset in data/.

Writes docs/index.html (GitHub Pages), build/artifact.html (Claude artifact) and samples/.
Usage:  python build_page.py ["<runtime text>"]
"""
import base64
import html
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
REPO_URL = "https://github.com/ibrahim77gh/synthetic-pii-ocr-pilot"
DESCRIPTION = ("Synthetic PII document OCR dataset pilot: 300 forms, invoices and ID-style cards "
               "with field-level bounding boxes, QC checks and reproducible generation.")
PICKS = ["form_0007", "form_0012", "invoice_0000", "invoice_0003", "idcard_0005", "idcard_0004"]
ORDER = [("GIVENNAME", "name", "Name"), ("SURNAME", "name", "Name"), ("EMAIL", "contact", "Contact"),
         ("TELEPHONENUM", "contact", "Contact"), ("STREET", "address", "Address"), ("CITY", "address", "Address"),
         ("ZIPCODE", "address", "Address"), ("DATEOFBIRTH", "date", "Date of birth"),
         ("IDCARDNUM", "id", "Government ID"), ("SOCIALNUM", "id", "Government ID"),
         ("IBAN", "fin", "Financial"), ("CREDITCARDNUMBER", "fin", "Financial")]
PRESIDIO = {"GIVENNAME": "PERSON", "SURNAME": "PERSON", "DATEOFBIRTH": "DATE_TIME", "EMAIL": "EMAIL_ADDRESS",
            "TELEPHONENUM": "PHONE_NUMBER", "STREET": "LOCATION", "CITY": "LOCATION", "ZIPCODE": "LOCATION",
            "IBAN": "IBAN_CODE", "CREDITCARDNUMBER": "CREDIT_CARD", "SOCIALNUM": "US_SSN", "IDCARDNUM": None}


def compact_record(r):
    """Top-level keys one per line, each entity on its own line."""
    lines = ["{"]
    keys = list(r)
    for i, k in enumerate(keys):
        end = "," if i < len(keys) - 1 else ""
        if k == "entities":
            lines.append('  "entities": [')
            for j, e in enumerate(r[k]):
                lines.append("    " + json.dumps(e, ensure_ascii=False) + ("," if j < len(r[k]) - 1 else ""))
            lines.append("  ]" + end)
        else:
            lines.append(f"  {json.dumps(k)}: {json.dumps(r[k], ensure_ascii=False)}{end}")
    lines.append("}")
    return "\n".join(lines)


def main():
    runtime = sys.argv[1] if len(sys.argv) > 1 else "About 1&nbsp;min 30&nbsp;s"
    rows = {r["doc_id"]: r for r in (json.loads(l) for l in open(DATA / "metadata.jsonl", encoding="utf-8"))}
    qc = json.loads((DATA / "qc_report.json").read_text(encoding="utf-8"))
    assert not qc["failures"], qc["failures"]
    samples = []
    for d in PICKS:
        r = rows[d]
        b64 = base64.b64encode((DATA / r["file_name"]).read_bytes()).decode()
        samples.append({"doc_id": d, "doc_type": r["doc_type"], "template": r["template"], "locale": r["locale"],
                        "width": r["width"], "height": r["height"], "split": r["split"], "render": r["render"],
                        "img": f"data:image/jpeg;base64,{b64}",
                        "entities": [{k: e[k] for k in ("label", "text", "bbox", "quad", "key_text")} for e in r["entities"]],
                        "record": compact_record(r)})
    by_label = qc["by_label"]
    top = max(by_label.values())
    label_rows = []
    for label, grp, grp_name in ORDER:
        n = by_label.get(label, 0)
        pres = PRESIDIO[label]
        pres_cell = f'<span class="lbl">{pres}</span>' if pres else '<span class="none">no equivalent, kept as unmapped</span>'
        label_rows.append(
            f'<tr><td class="lbl">{label}</td>'
            f'<td><span class="grp"><span class="sw" style="background:var(--g-{grp})"></span>{grp_name}</span></td>'
            f'<td class="barcell"><div class="bar" title="{label}: {n:,} fields"><span style="width:{n / top * 78:.1f}%"></span><b>{n:,}</b></div></td>'
            f"<td>{pres_cell}</td></tr>")
    first = rows[PICKS[0]]["entities"]
    example = next(e for e in first if e["label"] == "DATEOFBIRTH")
    size_mb = sum(f.stat().st_size for f in (DATA / "images").iterdir()) / 1e6
    nfiles = sum(1 for f in DATA.rglob("*") if f.is_file() and f.name != "qc_report.json")
    sp = qc["by_split"]
    fills = {
        "DOCS": f"{qc['documents']:,}", "FIELDS": f"{qc['pii_fields']:,}", "NLABELS": str(len(by_label)),
        "NLOCALES": str(len(qc["by_locale"])), "ROTATED": str(qc["rotated_docs"]),
        "RENDER": qc["render_check"].replace("/", " / "), "MIN_INK": f"{qc['min_ink_ratio'] * 100:.1f}%",
        "SPLITS": f"{sp['train']} train, {sp['validation']} validation, {sp['test']} test",
        "NFILES": str(nfiles), "SIZE_MB": f"{size_mb:.0f}", "RUNTIME": runtime,
        "LABEL_ROWS": "".join(label_rows),
        "EXAMPLE_ENTITY": html.escape(json.dumps(example, indent=2, ensure_ascii=False)),
        "GEN_SRC": html.escape((ROOT / "generate.py").read_text(encoding="utf-8")),
        "VAL_SRC": html.escape((ROOT / "validate.py").read_text(encoding="utf-8")),
        "DATA": json.dumps({"samples": samples}, ensure_ascii=False).replace("</", "<\\/"),
    }
    fills["REPO_URL"], fills["REPO_SHORT"] = REPO_URL, REPO_URL.removeprefix("https://")
    page = (ROOT / "page.tmpl.html").read_text(encoding="utf-8")
    for k, v in fills.items():
        page = page.replace("{{" + k + "}}", v)
    assert "{{" not in page.replace("${", ""), "unfilled placeholder"

    # Claude artifact version: the host adds the document skeleton itself
    (ROOT / "build").mkdir(exist_ok=True)
    (ROOT / "build" / "artifact.html").write_text(page, encoding="utf-8")

    # GitHub Pages version: same page in a full document
    head, body = page.split("</style>", 1)
    standalone = (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        f'<meta name="description" content="{DESCRIPTION}">\n'
        '<meta property="og:title" content="Synthetic PII OCR Pilot">\n'
        f'<meta property="og:description" content="{DESCRIPTION}">\n'
        "<style>:root{color-scheme:light}body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>\n"
        f"{head}</style>\n</head>\n<body>\n{body.strip()}\n</body>\n</html>\n")
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "index.html").write_text(standalone, encoding="utf-8")

    # A few real outputs for people browsing the repo
    out = ROOT / "samples"
    (out / "images").mkdir(parents=True, exist_ok=True)
    with open(out / "metadata.jsonl", "w", encoding="utf-8") as f:
        for d in PICKS:
            r = rows[d]
            shutil.copyfile(DATA / r["file_name"], out / r["file_name"])
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    for name in ("qc_report.json", "generation_log.json"):
        shutil.copyfile(DATA / name, out / name)
    print(f"wrote docs/index.html ({len(standalone.encode()) / 1e6:.2f} MB), build/artifact.html, "
          f"samples/ ({len(PICKS)} pages); dataset has {nfiles} files, {size_mb:.1f} MB images")


if __name__ == "__main__":
    main()
