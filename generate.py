"""Generate a synthetic OCR dataset of PII-bearing documents with field-level boxes.

Three templates (form, invoice, ID-style card), six locales. Every PII value is drawn
twice: once on the visible page and once on a hidden index mask with the same transform.
After augmentation the mask gives the true ink extent of each field, which is used to
verify the geometric box labels before anything is written.

Usage:  python generate.py --out data --per-template 100 --seed 1234
"""
import argparse
import hashlib
import json
import math
import random
import unicodedata
from functools import lru_cache
from pathlib import Path

import faker
import PIL
from faker import Faker
from faker.providers.address.it_IT import Provider as ItAddress
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

# Faker builds this list from a set, so its order follows PYTHONHASHSEED and the same seed
# gives different cities on each run. Sorting pins it.
ItAddress.cities = sorted(ItAddress.cities)

VERSION = "0.1.0"
FONT_DIR = Path("C:/Windows/Fonts")
FONTS = ["arial.ttf", "calibri.ttf", "times.ttf", "georgia.ttf", "verdana.ttf", "segoeui.ttf", "tahoma.ttf"]
LOCALES = ["en_US", "en_GB", "de_DE", "fr_FR", "it_IT", "nl_NL"]
DATE_FMT = {"en_US": "%m/%d/%Y", "en_GB": "%d/%m/%Y", "de_DE": "%d.%m.%Y",
            "fr_FR": "%d/%m/%Y", "it_IT": "%d/%m/%Y", "nl_NL": "%d-%m-%Y"}
ZIP_FIRST = {"de_DE", "fr_FR", "it_IT", "nl_NL"}
UNIQUE = ["EMAIL", "TELEPHONENUM", "IDCARDNUM", "SOCIALNUM", "IBAN", "CREDITCARDNUMBER"]

# Our label -> Presidio entity. None = no equivalent; kept visible instead of silently dropped.
PRESIDIO = {"GIVENNAME": "PERSON", "SURNAME": "PERSON", "DATEOFBIRTH": "DATE_TIME",
            "EMAIL": "EMAIL_ADDRESS", "TELEPHONENUM": "PHONE_NUMBER", "STREET": "LOCATION",
            "CITY": "LOCATION", "ZIPCODE": "LOCATION", "IBAN": "IBAN_CODE",
            "CREDITCARDNUMBER": "CREDIT_CARD", "SOCIALNUM": "US_SSN", "IDCARDNUM": None}

KEYS = {
    "GIVENNAME": ["First name", "Given name(s)", "Forename"],
    "SURNAME": ["Last name", "Surname", "Family name"],
    "DATEOFBIRTH": ["Date of birth", "DOB", "Birth date"],
    "EMAIL": ["Email", "E-mail address", "Email address"],
    "TELEPHONENUM": ["Phone", "Telephone", "Mobile number"],
    "STREET": ["Street address", "Address line 1", "Home address"],
    "CITY": ["City", "Town / City"],
    "ZIPCODE": ["Postcode", "ZIP / Postal code", "Postal code"],
    "IDCARDNUM": ["ID card no.", "National ID number", "Identity document no."],
    "SOCIALNUM": ["SSN", "Social Security no."],
    "IBAN": ["IBAN", "Bank account (IBAN)", "Direct debit IBAN"],
    "CREDITCARDNUMBER": ["Card number", "Credit card no."],
}


@lru_cache(maxsize=None)
def font(name, size):
    return ImageFont.truetype(str(FONT_DIR / name), size)


def fit(draw, text, name, size, max_w):
    while size > 11 and draw.textlength(text, font=font(name, size)) > max_w:
        size -= 1
    return font(name, size)


def ascii_slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return "".join(ch for ch in s.lower() if ch.isalnum())


def digits(s):
    return sum(ch.isdigit() for ch in s)


def make_person(fk, loc, rnd, seen):
    """One synthetic identity; values in UNIQUE never repeat across the dataset."""
    while True:
        given, surname = fk.first_name(), fk.last_name()
        dob = fk.date_of_birth(minimum_age=18, maximum_age=85)
        fmt = "%Y-%m-%d" if rnd.random() < 0.15 else DATE_FMT[loc]
        phone = fk.phone_number()
        while not 7 <= digits(phone) <= 15:
            phone = fk.phone_number()
        p = {
            "GIVENNAME": given, "SURNAME": surname, "DATEOFBIRTH": dob.strftime(fmt),
            "EMAIL": f"{ascii_slug(given)}{rnd.choice(['.', '_', ''])}{ascii_slug(surname)}"
                     f"{rnd.choice(['', '', str(rnd.randint(1, 99))])}@{fk.free_email_domain()}",
            "TELEPHONENUM": phone, "STREET": fk.street_address(), "CITY": fk.city(),
            "ZIPCODE": fk.postcode(),
            "IDCARDNUM": fk.bothify(rnd.choice(["??#######", "#########", "?##?#####"])).upper(),
        }
        if loc == "en_US":
            p["SOCIALNUM"] = fk.ssn()
            cc = fk.credit_card_number(card_type=rnd.choice(["visa16", "mastercard"]))
            p["CREDITCARDNUMBER"] = " ".join(cc[i:i + 4] for i in range(0, len(cc), 4))
        else:
            iban = fk.iban()
            p["IBAN"] = " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))
        p = {k: " ".join(v.split()) for k, v in p.items()}  # single line, single spaces
        keys = [(k, p[k].replace(" ", "")) for k in UNIQUE if k in p]
        ident = (given, surname, dob.isoformat())
        if ident not in seen and not any(kv in seen for kv in keys):
            seen.add(ident)
            seen.update(keys)
            p["_dob_iso"], p["_dob_fmt"] = dob.isoformat(), fmt
            return p


class Doc:
    """A page plus a hidden index mask: PII value i is drawn on the mask with pixel value i."""

    def __init__(self, w, h, paper):
        self.w, self.h, self.paper = w, h, paper
        self.img = Image.new("RGB", (w, h), paper)
        self.d = ImageDraw.Draw(self.img)
        self.mask = Image.new("L", (w, h), 0)
        self.md = ImageDraw.Draw(self.mask)
        self.md.fontmode = "1"  # no antialiasing, so mask values stay exact
        self.ents = []

    def text(self, xy, s, f, fill=(70, 70, 70)):
        self.d.text(xy, s, font=f, fill=fill)
        return list(self.d.textbbox(xy, s, font=f))

    def pii(self, xy, s, f, label, key_text=None, key_bbox=None, fill=(25, 25, 30)):
        box = self.text(xy, s, f, fill)
        self.ents.append({"label": label, "text": s, "bbox": box,
                          "key_text": key_text, "key_bbox": key_bbox})
        self.md.text(xy, s, font=f, fill=len(self.ents))
        return box


def paper(rnd):
    return tuple(rnd.randint(244, 255) - k for k in (0, rnd.randint(0, 3), rnd.randint(0, 8)))


def accent(rnd):
    return rnd.choice([(24, 64, 120), (20, 90, 80), (110, 30, 40), (60, 60, 70), (35, 80, 150)])


def fake_date(fk, loc):
    return fk.date_between("-2y", "today").strftime(DATE_FMT[loc])


# ---------------------------------------------------------------- templates

FORM_TITLES = ["Patient Registration Form", "Customer Onboarding Form", "Membership Application",
               "Tenant Application", "New Account Request"]


def form(p, loc, rnd, fk):
    W, H = 1000, 1414
    doc, fn, acc = Doc(W, H, paper(rnd)), rnd.choice(FONTS), accent(rnd)
    m = rnd.randint(60, 85)
    doc.d.rectangle([0, 0, W, rnd.randint(8, 16)], fill=acc)
    doc.text((m, 48), rnd.choice(FORM_TITLES), font(fn, rnd.randint(32, 40)), fill=acc)
    doc.text((m, 104), f"Form ref {rnd.randint(10000, 99999)}-{rnd.choice('ABCDEFG')}"
                       f"     Received {fake_date(fk, loc)}", font(fn, 17))
    id_key = "SOCIALNUM" if loc == "en_US" else "IDCARDNUM"
    pay_key = "CREDITCARDNUMBER" if loc == "en_US" else "IBAN"
    rows = [[("GIVENNAME", 1), ("SURNAME", 1)], [("DATEOFBIRTH", 1), ("TELEPHONENUM", 1)],
            [("EMAIL", 2)], [("STREET", 2)],
            [("ZIPCODE", 1), ("CITY", 1)] if loc in ZIP_FIRST else [("CITY", 1), ("ZIPCODE", 1)],
            [(id_key, 1)]]
    body = rows[2:]
    rnd.shuffle(body)
    rows = rows[:2] + body
    if rnd.random() < 0.7:
        rows.append([(pay_key, 2)])
    boxed = rnd.random() < 0.6
    gap, col_w = 30, (W - 2 * m - 30) // 2
    y, key_size, val_size = 160, rnd.randint(15, 18), rnd.randint(21, 26)
    for row in rows:
        x = m
        for label, span in row:
            w = col_w * span + gap * (span - 1)
            key = rnd.choice(KEYS[label])
            kb = doc.text((x, y), key.upper() if boxed else key + ":", font(fn, key_size))
            vy = y + key_size + 12
            if boxed:
                doc.d.rectangle([x, vy - 6, x + w, vy + val_size + 14], outline=(150, 150, 155), width=1)
            else:
                doc.d.line([x, vy + val_size + 10, x + w, vy + val_size + 10], fill=(150, 150, 155), width=1)
            vf = fit(doc.d, p[label], fn, val_size, w - 24)
            doc.pii((x + 10, vy), p[label], vf, label, key, kb)
            x += w + gap
        y += key_size + val_size + rnd.randint(52, 70)
    y += 20
    small = font(fn, 16)
    doc.text((m, y), "Preferred contact:   [x] Email   [ ] Phone   [ ] Post", small)
    doc.text((m, y + 34), "I confirm the information above is correct and consent to it being processed",
             small, fill=(110, 110, 110))
    doc.text((m, y + 56), "for the purpose stated in the privacy notice.", small, fill=(110, 110, 110))
    doc.d.line([m, y + 150, m + 330, y + 150], fill=(120, 120, 120))
    doc.text((m, y + 158), "Signature", small)
    doc.text((m + 420, y + 124), fake_date(fk, loc), font(fn, 20))
    doc.text((m + 420, y + 158), "Date", small)
    doc.text((m, H - 50), f"Synthetic sample - not a real record - template form/v1 - page 1 of 1",
             font(fn, 13), fill=(150, 150, 150))
    return doc, "form/v1"


SERVICES = ["Consulting hours", "Annual software licence", "Onboarding workshop", "Support plan (monthly)",
            "Data migration", "Hardware rental", "Training session", "Cloud storage 1 TB", "Maintenance visit"]


def invoice(p, loc, rnd, fk):
    W, H = 1000, 1414
    doc, fn, acc = Doc(W, H, paper(rnd)), rnd.choice(FONTS), accent(rnd)
    m = rnd.randint(60, 85)
    f16, f18 = font(fn, 16), font(fn, 18)
    doc.text((m, 60), fk.company(), font(fn, 26), fill=acc)
    doc.d.multiline_text((m, 100), fk.address(), font=f16, fill=(90, 90, 90), spacing=6)
    doc.text((W - m - 260, 56), "INVOICE", font(fn, 44), fill=acc)
    doc.text((W - m - 260, 120), f"No. INV-{rnd.randint(100000, 999999)}", f16)
    doc.text((W - m - 260, 146), f"Issued {fake_date(fk, loc)}", f16)
    doc.text((W - m - 260, 172), f"Due {fake_date(fk, loc)}", f16)
    y = 250
    kb = doc.text((m, y), rnd.choice(["Bill to", "Billed to", "Customer"]), font(fn, 15), fill=acc)
    vf = font(fn, rnd.randint(19, 22))
    line_h = vf.size + 12
    y += 30
    gx = m
    doc.pii((gx, y), p["GIVENNAME"], vf, "GIVENNAME", "Bill to", kb)
    gx += doc.d.textlength(p["GIVENNAME"] + " ", font=vf)
    doc.pii((gx, y), p["SURNAME"], vf, "SURNAME", "Bill to", kb)
    y += line_h
    doc.pii((m, y), p["STREET"], fit(doc.d, p["STREET"], fn, vf.size, 420), "STREET", "Bill to", kb)
    y += line_h
    first, second = ("ZIPCODE", "CITY") if loc in ZIP_FIRST else ("CITY", "ZIPCODE")
    sep = " " if loc in ZIP_FIRST else ", "
    doc.pii((m, y), p[first], vf, first, "Bill to", kb)
    doc.pii((m + doc.d.textlength(p[first] + sep, font=vf), y), p[second], vf, second, "Bill to", kb)
    y += line_h
    doc.pii((m, y), p["EMAIL"], vf, "EMAIL", "Bill to", kb)
    y += line_h
    doc.pii((m, y), p["TELEPHONENUM"], vf, "TELEPHONENUM", "Bill to", kb)
    y += line_h + 50
    cols = [m, m + 470, m + 580, W - m - 140]
    doc.d.rectangle([m - 10, y - 8, W - m + 10, y + 30], fill=acc)
    for x, h in zip(cols, ["Description", "Qty", "Unit", "Amount"]):
        doc.d.text((x, y), h, font=f16, fill=(255, 255, 255))
    y += 48
    total = 0.0
    for svc in rnd.sample(SERVICES, rnd.randint(3, 6)):
        q, u = rnd.randint(1, 12), round(rnd.uniform(20, 900), 2)
        total += q * u
        for x, s in zip(cols, [svc, str(q), f"{u:,.2f}", f"{q * u:,.2f}"]):
            doc.text((x, y), s, f16)
        y += 34
        doc.d.line([m, y - 8, W - m, y - 8], fill=(220, 220, 220))
    vat = total * 0.2
    for i, (k, v) in enumerate([("Subtotal", total), ("VAT 20%", vat), ("Total due", total + vat)]):
        doc.text((cols[2], y + 16 + i * 32), k, f18 if i < 2 else font(fn, 20))
        doc.text((cols[3], y + 16 + i * 32), f"{v:,.2f}", f18 if i < 2 else font(fn, 20))
    y += 160
    pay = "CREDITCARDNUMBER" if loc == "en_US" else "IBAN"
    key = "Paid by card" if pay == "CREDITCARDNUMBER" else "Direct debit from IBAN"
    kb = doc.text((m, y), key, f16, fill=acc)
    doc.pii((m, y + 26), p[pay], font(fn, 20), pay, key, kb)
    doc.text((m, H - 60), "Thank you for your business. Payment terms: 30 days.", f16, fill=(120, 120, 120))
    doc.text((m, H - 36), "Synthetic sample - not a real record - template invoice/v1", font(fn, 13),
             fill=(150, 150, 150))
    return doc, "invoice/v1"


def idcard(p, loc, rnd, fk):
    W, H = 1012, 638
    tint = rnd.choice([(232, 240, 248), (238, 246, 236), (248, 240, 232), (240, 236, 248)])
    doc, fn = Doc(W, H, (205, 205, 205)), rnd.choice(["arial.ttf", "verdana.ttf", "segoeui.ttf", "tahoma.ttf"])
    doc.bed, acc = (205, 205, 205), accent(rnd)
    doc.d.rounded_rectangle([6, 6, W - 6, H - 6], radius=36, fill=tint, outline=(150, 150, 150), width=2)
    # watermark goes on first so field text sits on top of it
    wm = Image.new("L", (W, H), 0)
    ImageDraw.Draw(wm).text((W // 2, H // 2), "SPECIMEN", font=font("arial.ttf", 150), fill=255, anchor="mm")
    wm = wm.rotate(18, resample=Image.BICUBIC)
    doc.img.paste(Image.new("RGB", (W, H), tuple(c - 14 for c in tint)), (0, 0), wm.point(lambda v: v // 3))
    doc.d.rounded_rectangle([6, 6, W - 6, 96], radius=36, fill=acc)
    doc.d.rectangle([6, 60, W - 6, 96], fill=acc)
    doc.d.text((40, 26), "IDENTITY CARD", font=font(fn, 34), fill=(255, 255, 255))
    doc.d.text((W - 40, 34), "SYNTHETIC SPECIMEN - NOT VALID", font=font(fn, 18), fill=(255, 255, 255), anchor="ra")
    doc.d.rounded_rectangle([40, 130, 290, 450], radius=12, fill=(200, 205, 212))
    doc.d.ellipse([115, 175, 215, 285], fill=(150, 156, 166))
    doc.d.pieslice([75, 300, 255, 520], 180, 360, fill=(150, 156, 166))
    x, y = 330, 124
    k, v = font(fn, 15), font(fn, rnd.randint(26, 30))
    for label, key in [("SURNAME", "Surname"), ("GIVENNAME", "Given names"), ("DATEOFBIRTH", "Date of birth"),
                       ("IDCARDNUM", "Document no.")]:
        kb = doc.text((x, y), key.upper(), k, fill=(90, 90, 100))
        doc.pii((x, y + 22), p[label], fit(doc.d, p[label], fn, v.size, W - x - 50), label, key, kb)
        y += 76
    doc.text((x + 330, 124 + 2 * 76), "EXPIRES", k, fill=(90, 90, 100))
    doc.text((x + 330, 124 + 2 * 76 + 22), fk.date_between("+1y", "+10y").strftime(DATE_FMT[loc]), font(fn, 24))
    kb = doc.text((40, 476), "ADDRESS", k, fill=(90, 90, 100))
    af = font(fn, 21)
    doc.pii((40, 500), p["STREET"], fit(doc.d, p["STREET"], fn, 21, W - 100), "STREET", "Address", kb)
    first, second = ("ZIPCODE", "CITY") if loc in ZIP_FIRST else ("CITY", "ZIPCODE")
    sep = " " if loc in ZIP_FIRST else ", "
    doc.pii((40, 534), p[first], af, first, "Address", kb)
    doc.pii((40 + doc.d.textlength(p[first] + sep, font=af), 534), p[second], af, second, "Address", kb)
    doc.text((W - 40 - 160, 560), "template id/v1", font(fn, 13), fill=(130, 130, 130))
    return doc, "idcard/v1"


TEMPLATES = {"form": form, "invoice": invoice, "idcard": idcard}

# ---------------------------------------------------------------- augmentation & geometry


def rotate_pts(pts, angle, w, h, nw, nh):
    """Forward map for PIL's rotate(angle, expand=True): counter-clockwise about the centre."""
    a = math.radians(angle)
    c, s = math.cos(a), math.sin(a)
    return [((x - w / 2) * c + (y - h / 2) * s + nw / 2, -(x - w / 2) * s + (y - h / 2) * c + nh / 2)
            for x, y in pts]


def corners(b):
    return [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]


def aabb(q):
    xs, ys = [x for x, _ in q], [y for _, y in q]
    return [math.floor(min(xs)), math.floor(min(ys)), math.ceil(max(xs)), math.ceil(max(ys))]


def augment(doc, rnd):
    img, mask, w, h = doc.img, doc.mask, doc.w, doc.h
    angle = round(rnd.uniform(-1.5, 1.5), 2) if rnd.random() < 0.7 else 0.0
    if angle:
        bed = getattr(doc, "bed", None) or (rnd.randint(190, 215),) * 3
        img = img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=bed)
        mask = mask.rotate(angle, resample=Image.NEAREST, expand=True, fillcolor=0)
    nw, nh = img.size
    for e in doc.ents:
        for k in ("bbox", "key_bbox"):
            if e[k] is None:
                continue
            q = rotate_pts(corners(e[k]), angle, w, h, nw, nh)
            if k == "bbox":
                e["quad"] = [[round(x, 1), round(y, 1)] for x, y in q]
            e[k] = aabb(q)
    blur = rnd.choice([0, 0, 0.4, 0.7])
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    img = ImageEnhance.Contrast(img).enhance(round(rnd.uniform(0.85, 1.1), 2))
    img = ImageEnhance.Brightness(img).enhance(round(rnd.uniform(0.96, 1.04), 2))
    noise = round(rnd.uniform(0, 0.05), 3)
    if noise:
        # seeded noise; Image.effect_noise has its own unseeded RNG and breaks reproducibility
        grain = Image.frombytes("L", (nw, nh), rnd.randbytes(nw * nh)).convert("RGB")
        img = Image.blend(img, grain, noise)
    return img, mask, {"angle": angle, "blur": blur, "noise": noise, "jpeg_quality": rnd.randint(62, 92)}


def render_check(ents, mask, tol=2):
    """True ink box of each field (from the mask) must sit inside its label box (+tol px)."""
    out = []
    for i, e in enumerate(ents, start=1):
        ink = mask.point(lambda v, i=i: 255 if v == i else 0).getbbox()
        b = e["bbox"]
        ok = ink is not None and ink[0] >= b[0] - tol and ink[1] >= b[1] - tol \
            and ink[2] <= b[2] + tol and ink[3] <= b[3] + tol
        out.append(ok)
    return out


def split_for(ident):
    v = int(hashlib.sha1("|".join(ident).encode()).hexdigest(), 16) % 10
    return "test" if v == 0 else "validation" if v == 1 else "train"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data")
    ap.add_argument("--per-template", type=int, default=100)
    ap.add_argument("--seed", type=int, default=1234)
    a = ap.parse_args()
    out = Path(a.out)
    (out / "images").mkdir(parents=True, exist_ok=True)
    rnd = random.Random(a.seed)
    fakers = {}
    for i, loc in enumerate(LOCALES):
        fakers[loc] = Faker(loc)
        fakers[loc].seed_instance(a.seed + i)
    provenance = {"generator": f"pii-ocr-pilot {VERSION}", "seed": a.seed, "faker": faker.VERSION,
                  "pillow": PIL.__version__, "values": "Faker (MIT), synthetic",
                  "fonts": "Windows system fonts - pilot only, swap to OFL fonts before public release"}
    seen, rows, checks = set(), [], {"fields": 0, "render_ok": 0}
    for t, build in TEMPLATES.items():
        for n in range(a.per_template):
            loc = rnd.choice(LOCALES)
            fk = fakers[loc]
            p = make_person(fk, loc, rnd, seen)
            doc, template = build(p, loc, rnd, fk)
            img, mask, render = augment(doc, rnd)
            oks = render_check(doc.ents, mask)
            checks["fields"] += len(oks)
            checks["render_ok"] += sum(oks)
            if not all(oks):
                raise SystemExit(f"{t}_{n}: label box does not cover rendered text {oks}")
            doc_id = f"{t}_{n:04d}"
            img.save(out / "images" / f"{doc_id}.jpg", quality=render["jpeg_quality"])
            for e in doc.ents:
                e["presidio"] = PRESIDIO[e["label"]]
                if e["label"] == "DATEOFBIRTH":
                    e["normalized"], e["source_format"] = p["_dob_iso"], p["_dob_fmt"]
            ident = (p["GIVENNAME"], p["SURNAME"], p["_dob_iso"])
            person_id = hashlib.sha1("|".join(ident).encode()).hexdigest()[:12]
            rows.append({"file_name": f"images/{doc_id}.jpg", "doc_id": doc_id, "person_id": person_id, "doc_type": t,
                         "template": template, "locale": loc, "width": img.width, "height": img.height,
                         "split": split_for(ident), "entities": doc.ents, "render": render,
                         "provenance": provenance})
    with open(out / "metadata.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
        flat = [{"doc_id": r["doc_id"], "doc_type": r["doc_type"], "locale": r["locale"], "split": r["split"],
                 "label": e["label"], "text": e["text"], "presidio": e["presidio"],
                 "x0": e["bbox"][0], "y0": e["bbox"][1], "x1": e["bbox"][2], "y1": e["bbox"][3],
                 "key_text": e["key_text"]} for r in rows for e in r["entities"]]
        pq.write_table(pa.Table.from_pylist(flat), out / "entities.parquet")
    except ImportError:
        pass
    (out / "generation_log.json").write_text(json.dumps({**checks, "docs": len(rows), **provenance}, indent=2))
    print(f"{len(rows)} docs, {checks['fields']} PII fields, render check {checks['render_ok']}/{checks['fields']}")


if __name__ == "__main__":
    main()
