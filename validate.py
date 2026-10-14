"""Validate a generated dataset from disk only (images + metadata.jsonl) and write qc_report.json.

Usage:  python validate.py --data data
Exit code 1 if any check fails.
"""
import argparse
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from PIL import Image

UNIQUE = ["EMAIL", "TELEPHONENUM", "IDCARDNUM", "SOCIALNUM", "IBAN", "CREDITCARDNUMBER"]


def iban_ok(s):
    s = s.replace(" ", "")
    n = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return bool(re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{8,30}", s)) and int(n) % 97 == 1


def luhn_ok(s):
    d = [int(c) for c in s.replace(" ", "")][::-1]
    return len(d) >= 13 and sum(x if i % 2 == 0 else (x * 2 - 9 if x > 4 else x * 2) for i, x in enumerate(d)) % 10 == 0


def dob_ok(e):
    try:
        return datetime.strptime(e["text"], e["source_format"]).date().isoformat() == e["normalized"]
    except (KeyError, ValueError):
        return False


VALIDATORS = {
    "EMAIL": lambda e: re.fullmatch(r"[a-z0-9._]+@[a-z0-9.-]+\.[a-z]{2,}", e["text"]) is not None,
    "TELEPHONENUM": lambda e: 7 <= sum(c.isdigit() for c in e["text"]) <= 15,
    "DATEOFBIRTH": dob_ok,
    "IBAN": lambda e: iban_ok(e["text"]),
    "CREDITCARDNUMBER": lambda e: luhn_ok(e["text"]),
    "SOCIALNUM": lambda e: re.fullmatch(r"\d{3}-\d{2}-\d{4}", e["text"]) is not None,
    "IDCARDNUM": lambda e: re.fullmatch(r"[A-Z0-9]{9}", e["text"]) is not None,
    "ZIPCODE": lambda e: any(c.isdigit() for c in e["text"]),
    "GIVENNAME": lambda e: e["text"] != "" and not any(c.isdigit() for c in e["text"]),
    "SURNAME": lambda e: e["text"] != "" and not any(c.isdigit() for c in e["text"]),
    "CITY": lambda e: e["text"] != "",
    "STREET": lambda e: e["text"] != "",
}


def derotated_boxes(ents):
    """Undo the page rotation (read from the quads) so overlap is tested on upright boxes."""
    (x0, y0), (x1, y1) = ents[0]["quad"][0], ents[0]["quad"][1]
    a = math.atan2(y1 - y0, x1 - x0)
    c, s = math.cos(-a), math.sin(-a)
    out = []
    for e in ents:
        pts = [(x * c - y * s, x * s + y * c) for x, y in e["quad"]]
        out.append((min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)))
    return out


def ink_ratio(gray, b):
    crop = gray.crop(b)
    hist = crop.histogram()
    return sum(hist[:140]) / max(1, crop.width * crop.height)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    root = Path(ap.parse_args().data)
    rows = [json.loads(line) for line in open(root / "metadata.jsonl", encoding="utf-8")]
    fails = defaultdict(list)
    img_hashes, idents, uniq = {}, {}, defaultdict(dict)
    min_ink, labels = 1.0, Counter()
    for r in rows:
        path = root / r["file_name"]
        if not path.exists():
            fails["missing_file"].append(r["doc_id"])
            continue
        raw = path.read_bytes()
        h = hashlib.sha256(raw).hexdigest()
        if h in img_hashes:
            fails["duplicate_image"].append((img_hashes[h], r["doc_id"]))
        img_hashes[h] = r["doc_id"]
        im = Image.open(path)
        if im.size != (r["width"], r["height"]):
            fails["size_mismatch"].append(r["doc_id"])
        gray = im.convert("L")
        ident = r["person_id"]
        if ident in idents:
            fails["duplicate_identity"].append((idents[ident][0], r["doc_id"]))
        idents[ident] = (r["doc_id"], r["split"])
        for e in r["entities"]:
            labels[e["label"]] += 1
            x0, y0, x1, y1 = e["bbox"]
            if not (0 <= x0 < x1 <= r["width"] and 0 <= y0 < y1 <= r["height"]):
                fails["out_of_bounds"].append((r["doc_id"], e["label"]))
            if x1 - x0 < 3 or y1 - y0 < 3:
                fails["degenerate_box"].append((r["doc_id"], e["label"]))
            if not VALIDATORS[e["label"]](e):
                fails["format:" + e["label"]].append((r["doc_id"], e["text"]))
            ink = ink_ratio(gray, e["bbox"])
            min_ink = min(min_ink, ink)
            if ink < 0.02:
                fails["no_visible_ink"].append((r["doc_id"], e["label"], round(ink, 3)))
            if e["label"] in UNIQUE:
                v = e["text"].replace(" ", "")
                if v in uniq[e["label"]]:
                    fails["duplicate_value:" + e["label"]].append((uniq[e["label"]][v][0], r["doc_id"]))
                uniq[e["label"]][v] = (r["doc_id"], r["split"])
        boxes = derotated_boxes(r["entities"])
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if min(a[2], b[2]) - max(a[0], b[0]) > 1 and min(a[3], b[3]) - max(a[1], b[1]) > 1:
                    fails["overlapping_boxes"].append((r["doc_id"], r["entities"][i]["label"], r["entities"][j]["label"]))
    # identities and unique values are generated per document, so a split is leak-free if no
    # value seen in test/validation also appears in train
    split_of = {r["doc_id"]: r["split"] for r in rows}
    leaks = 0
    for label, vals in uniq.items():
        by_val = defaultdict(set)
        for r in rows:
            for e in r["entities"]:
                if e["label"] == label:
                    by_val[e["text"].replace(" ", "")].add(split_of[r["doc_id"]])
        leaks += sum(len(s) > 1 for s in by_val.values())
    if leaks:
        fails["split_leakage"].append(leaks)
    gen = json.loads((root / "generation_log.json").read_text())
    report = {
        "documents": len(rows),
        "pii_fields": sum(labels.values()),
        "by_doc_type": Counter(r["doc_type"] for r in rows),
        "by_locale": Counter(r["locale"] for r in rows),
        "by_split": Counter(r["split"] for r in rows),
        "by_label": labels,
        "rotated_docs": sum(r["render"]["angle"] != 0 for r in rows),
        "render_check": f"{gen['render_ok']}/{gen['fields']}",
        "min_ink_ratio": round(min_ink, 3),
        "checks": ["files exist, sizes match", "boxes in bounds, non-degenerate",
                   "no overlapping PII boxes (rotation undone)", "format validators per label",
                   "visible ink inside every box", "no duplicate images (sha256)", "no duplicate identities",
                   "unique fields never repeat", "no value crosses splits"],
        "failures": {k: len(v) for k, v in fails.items()},
        "failure_examples": {k: v[:5] for k, v in fails.items()},
    }
    (root / "qc_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps({k: report[k] for k in ("documents", "pii_fields", "render_check", "min_ink_ratio", "failures")}))
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
