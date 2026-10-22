#!/usr/bin/env python3
"""
Pipeline « vaults » : une ligne par SÉRIE des sociétés Masterworks Vault (une vault = un CIK,
mais chaque série détient sa propre œuvre et fait sa propre offre).

Tout est lu par le code, sans LLM, dans les demandes d'offre de chaque vault (1-A, 1-A/A, 1-A POS) :
  - le tableau des séries        : « Series 354 | Hans Hofmann | Gloria in Excelsis | $ | 2,442,000 | 122,100 | 4/24/2024 | Open »
  - le « Use of Proceeds » de chaque série : prix d'achat de l'œuvre et « Expense Allocation Payment »
  - la description de chaque série : « The Artwork held by Series 354 entitled Gloria in Excelsis (1963), is a single
    work of art by Hans Hofmann. […] measures at 50 inches by 40 inches […] for $2,200,000 on April 4, 2024. »
  - l'historique des ventes de Masterworks (toutes sociétés) : date de l'accord de vente et rendement annualisé

Sorties :
    data/tables/vault_table.csv  (une ligne par série, nommée comme dans l'ancien Excel :
                                  « Masterworks Vault 5, LLC (Series 354) »)
    data/tables/exits_table.csv  (l'historique des ventes, utilisé aussi pour les LLC classiques)
    + les mêmes fichiers dans S3, sous tables/

Une valeur absente des documents reste VIDE.

Usage (depuis la racine du projet) :
    python3 pipeline/lib/vault_pipeline.py
    python3 pipeline/lib/vault_pipeline.py --only "Vault 5"
"""

import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).parent))
import extract_sections as sections_lib                    # noqa: E402  (lecture HTML, texte invisible retiré)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "sec-filings20260901174817304000000001")
S3_CLIENT = boto3.client("s3", region_name=AWS_REGION)
S3_FILINGS_KEY = "tables/filings.csv"
S3_MANIFEST_KEY = "tables/documents.csv"
S3_VAULT_KEY = "tables/vault_table.csv"
S3_EXITS_KEY = "tables/exits_table.csv"

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "tables"
LOCAL_VAULT = DATA_DIR / "vault_table.csv"
LOCAL_EXITS = DATA_DIR / "exits_table.csv"

OFFERING_FORMS = {"1-A", "1-A/A", "1-A POS"}

VAULT_COLUMNS = ["cik", "vault_cik", "issuer_name_edgar", "vault_name", "series",
                 "artist_name", "title", "execution_year", "dimensions_text",
                 "purchase_date", "purchase_price_usd", "acquisition_cost", "purchase_type", "true_up",
                 "class_a_shares_offered", "price_per_class_a_share", "aggregate_offering",
                 "class_b_shares_masterworks", "class_b_aggregate_cash_cost",
                 "date_initial_1a", "date_sec_qualification", "date_offering_launch", "offering_status",
                 "sale_agreement_date", "irr", "llc_liquidated", "sources", "warnings"]
EXITS_COLUMNS = ["issuer_name", "artist", "sale_agreement_date", "sale_announcement_date", "irr", "source"]

MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
DATE_WORDS = rf"({MONTH}\s+\d{{1,2}}\s*,\s*\d{{4}})"


# =====================================================================================
# 1. OUTILS DE LECTURE
# =====================================================================================

def parse_date(s):
    s = re.sub(r"\s+", " ", (s or "").replace(" ,", ",")).strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse_number(s):
    try:
        return float(re.sub(r"[^\d.]", "", s))
    except ValueError:
        return None


def series_key(vault_name, series):
    """« Masterworks Vault 5, LLC (Series 354) » : le nom utilisé dans l'ancien Excel."""
    return f"{vault_name} (Series {series})"


def exit_issuer_key(issuer):
    """« Series 373, a Series of Masterworks Vault 3, LLC » -> « Masterworks Vault 3, LLC (Series 373) »."""
    m = re.match(r"\s*Series (\d{3,4}), a Series of (Masterworks Vault \d+, LLC)", issuer)
    return series_key(m.group(2), m.group(1)) if m else issuer.strip()


def load_blocks(html):
    with tempfile.NamedTemporaryFile(suffix=".htm", delete=False) as tmp:
        tmp.write(html)
    try:
        return sections_lib.load_file(tmp.name)
    finally:
        os.unlink(tmp.name)


def unscramble(text):
    """« xExrxox » -> « Ero » : une lettre sur deux est un « x » de brouillage (texte anti-copie mal retiré)."""
    if text and len(text) >= 5 and re.fullmatch(r"(?:[xX].)+[xX]?", text):
        return text[1::2]
    return text


def block_text(block):
    return re.sub(r"\s+", " ", block.get_text(" ")).replace("“", '"').replace("”", '"').replace("’", "'").strip()


# =====================================================================================
# 2. CE QU'UN DOCUMENT DE VAULT CONTIENT
# =====================================================================================

def series_table(blocks):
    """Le tableau « Series Name | Artist | Artwork | Offering Size | Class A shares | Opening Date | Status »."""
    found = {}
    for b in blocks:
        if b.name != "table":
            continue
        rows = sections_lib.render_table(b)
        if not rows or not re.match(r"Series( Name)?\b", rows[0]) or "Artist" not in rows[0]:
            continue
        for row in rows[1:]:
            cells = [c.strip() for c in row.split(" | ")]
            m = re.fullmatch(r"Series (\d{3,4})", cells[0]) if cells else None
            if not m or len(cells) < 5:
                continue
            numbers = [parse_number(c) for c in cells[3:] if re.fullmatch(r"[\d,]+(\.\d+)?", c)]
            dates = [parse_date(c) for c in cells if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", c)]
            found[m.group(1)] = {
                "artist_name": unscramble(cells[1]), "title": unscramble(cells[2]),
                "aggregate_offering": numbers[0] if len(numbers) > 0 else None,
                "class_a_shares_offered": int(numbers[1]) if len(numbers) > 1 else None,
                "date_offering_launch": dates[0] if dates else None,          # « [*/*/24] » : pas encore de date
                "offering_status": cells[-1] if not re.fullmatch(r"\(\d\)", cells[-1]) else None,
                "_evidence": row,
            }
    return found


def use_of_proceeds(blocks):
    """Pour chaque série : « Acquisition of Artwork » (prix d'achat) et « Expense Allocation Payment »."""
    found, current = {}, None
    for b in blocks:
        text = block_text(b)
        if b.name != "table":
            m = re.fullmatch(r"Series (\d{3,4})", text)
            if m:
                current = m.group(1)
            continue
        rows = sections_lib.render_table(b)
        acq = next((r for r in rows if r.startswith("Acquisition")), None)
        if current and acq:
            fee = next((r for r in rows if r.lower().startswith("expense allocation")), None)
            amount = lambda r: parse_number(re.search(r"\$ \| ([\d,]+)", r).group(1)) if r and re.search(r"\$ \| ([\d,]+)", r) else None
            found[current] = {"acquisition_cost": amount(acq), "true_up": amount(fee),
                              "_evidence": " ; ".join(r for r in (acq, fee) if r)}
            current = None
    return found


DESCRIPTION = re.compile(
    r"The Artwork held by Series (?P<series>\d{3,4}),? entitled,?\s+\"?(?P<title>.+?)\"?"
    r"(?:\s*\((?P<year>\d{4})(?:\s*[-–]\s*(?P<year_end>\d{2,4}))?\))?\s*,?\s+is (?:a single )?(?:work of art )?(?:created )?by\s+"
    r"(?P<artist>[^.]+?)\.\s")
# « for €739,750 or $800,719 » : le montant en dollars après une autre devise
DUAL_PRICE = re.compile(r"(?:€|£|EUR|GBP|HK\$|CHF)\s?[\d,.]+\s*(?:,\s*)?or\s+\$\s?([\d,]+(?:\.\d+)?)")
SINGLE_PRICE = re.compile(r"\bfor\s+\$\s?([\d,]+(?:\.\d+)?)")


def last_year(year, year_end):
    """« (2018-2019) » -> 2019, « (2007-08) » -> 2008 : la dernière année, comme l'ancien Excel."""
    if not year:
        return None
    if not year_end:
        return int(year)
    return int(year[:4 - len(year_end)] + year_end) if len(year_end) < 4 else int(year_end)


def descriptions(blocks):
    """La phrase qui décrit l'œuvre d'une série, et ce qui la suit dans le même paragraphe."""
    found = {}
    for b in blocks:
        text = block_text(b)
        m = DESCRIPTION.search(text + " ")
        if not m:
            continue
        rest = text[m.end() - 1:]
        # La phrase d'achat : « Masterworks has agreed to acquire the Artwork, which measures … on <date> … for $… »
        buy = re.search(r"(?:agreed to acquire|acquired|purchased) the Artwork.+?(?<!\bNo)(?<!\bInc)(?<!\bCo)\.(?:\s|$)", rest)
        buy = buy.group(0) if buy else rest[:500]
        dims = re.search(r"measures(?: at)?\s+(.+?(?:inches|cm|in\.)(?:\s*\([^)]*\))?)(?:,|\s+in\s+a\b|\s+and\b|\s+at\b)", buy)
        date = re.search(r"\bon\s+" + DATE_WORDS, buy)
        price = DUAL_PRICE.search(buy) or SINGLE_PRICE.search(buy)
        kind = ("private" if re.search(r"privately negotiated", buy) else
                "auction" if re.search(r"\bauction\b|Christie|Sotheby|Phillips|Bonhams", buy) else None)
        found[m.group("series")] = {
            "title": unscramble(m.group("title").strip(" ,")),
            "execution_year": last_year(m.group("year"), m.group("year_end")),
            "artist_name": unscramble(m.group("artist").strip()), "dimensions_text": dims.group(1).strip() if dims else None,
            "purchase_price_usd": parse_number(price.group(1)) if price else None,
            "purchase_date": parse_date(date.group(1)) if date else None, "purchase_type": kind,
            "_evidence": text[m.start():m.start() + 420],
        }
    return found


def class_b_terms(blocks):
    """Les actions B, décrites une fois pour toutes les séries : « 1,000 Class B shares of each series », « $100.00 »."""
    text = " ".join(block_text(b) for b in blocks)
    shares = re.search(r"([\d,]+) Class B shares of each series", text)
    cost = re.search(r"aggregate cash cost to Masterworks for the Class B shares of a series will be \$([\d,.]+)", text)
    price = re.search(r"\$(\d+(?:\.\d+)?) per Class A share", text)
    return {"class_b_shares_masterworks": int(parse_number(shares.group(1))) if shares else None,
            "class_b_aggregate_cash_cost": parse_number(cost.group(1)) if cost else None,
            "price_per_class_a_share": parse_number(price.group(1)) if price else None}


def exit_history(blocks):
    """L'historique des ventes de Masterworks : « Issuer | Artist | Date of Sale Agreement | … | Net Annualized Return »."""
    exits = {}
    for b in blocks:
        if b.name != "table":
            continue
        rows = sections_lib.render_table(b)
        if not rows or "Date of Sale Agreement" not in rows[0] or "Annualized" not in rows[0]:
            continue
        for row in rows[1:]:
            cells = [c.strip() for c in row.split(" | ")]
            if len(cells) < 4 or "Masterworks" not in cells[0]:
                continue
            irr = re.search(r"(-?\d+(?:\.\d+)?)\s?%", " ".join(cells[3:]))
            dates = [parse_date(c) for c in cells if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", c)]
            announce = re.search(r"(\d{1,2}/\d{1,2}/\d{2,4})\s*\(", " ".join(cells[3:]))
            exits[exit_issuer_key(cells[0])] = {
                "issuer_name": exit_issuer_key(cells[0]), "artist": cells[1],
                "sale_agreement_date": dates[0] if dates else None,
                "sale_announcement_date": parse_date(announce.group(1)) if announce else None,
                "irr": round(float(irr.group(1)) / 100, 6) if irr else None, "_evidence": row}
    return exits


# =====================================================================================
# 3. S3
# =====================================================================================

def s3_read(key):
    try:
        return S3_CLIENT.get_object(Bucket=BUCKET_NAME, Key=key)["Body"].read()
    except ClientError:
        return None


def s3_write(key, content, content_type="text/plain"):
    if isinstance(content, str):
        content = content.encode("utf-8")
    S3_CLIENT.put_object(Bucket=BUCKET_NAME, Key=key, Body=content, ContentType=content_type)


def read_csv_source(local, s3_key, label):
    if local:
        return list(csv.DictReader(open(local, encoding="utf-8")))
    raw = s3_read(s3_key)
    if raw is None:
        raise RuntimeError(f"{s3_key} introuvable dans S3 : lancez d'abord {label}.")
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))


def write_csv(rows, columns, local_path, s3_key):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(buffer.getvalue(), encoding="utf-8")
    s3_write(s3_key, buffer.getvalue(), "text/csv")


# =====================================================================================
# 4. UNE VAULT
# =====================================================================================

def main_document(accession, files_by_accession):
    """Le document principal d'un dépôt 1-A : le plus gros fichier .htm (la circulaire, « partiiandiii.htm »)."""
    files = [f for f in files_by_accession.get(accession, []) if f["file_name"].lower().endswith((".htm", ".html"))]
    return max(files, key=lambda f: int(f.get("size") or 0))["file_name"] if files else None


def process_vault(cik, vault_filings, files_by_accession, exits):
    vault_name = vault_filings[0]["company_name"]
    ordered = sorted(vault_filings, key=lambda f: (f["filing_date"], f["accession_number"]))
    qualifs = sorted(f["filing_date"] for f in ordered if f["form"] == "QUALIF")
    series, warnings, n_docs = {}, [], 0
    terms = {}

    for f in ordered:
        if f["form"] not in OFFERING_FORMS:
            continue
        name = main_document(f["accession_number"], files_by_accession)
        html = s3_read(f"raw/{cik}/{f['accession_number']}/{name}") if name else None
        if html is None:
            warnings.append(f"{f['form']} du {f['filing_date']} absent de S3")
            continue
        n_docs += 1
        blocks = load_blocks(html)
        label = f"{f['form']} du {f['filing_date']}"
        terms = {**terms, **{k: v for k, v in class_b_terms(blocks).items() if v is not None}}
        exits.update(exit_history(blocks))
        # La description passe en dernier : pour un même document, elle l'emporte sur le tableau
        # (titre complet, année). D'un document à l'autre, le plus récent l'emporte (une description
        # peut être mise à jour : date d'achat de Series 475).
        for source, priority in ((series_table(blocks), "latest"), (use_of_proceeds(blocks), "latest"),
                                 (descriptions(blocks), "latest")):
            for s, values in source.items():
                entry = series.setdefault(s, {"_first_seen": f["filing_date"], "_sources": {}})
                for field, value in values.items():
                    if field.startswith("_") or value in (None, ""):
                        continue
                    # Tableau et Use of Proceeds : le document le plus récent l'emporte (statut, date d'ouverture…).
                    # Description : la première lue (l'œuvre et son achat ne changent pas).
                    if priority == "first" and entry.get(field) not in (None, ""):
                        continue
                    if priority == "latest" or field not in entry:
                        entry[field] = value
                        entry["_sources"][field] = label

    rows = []
    for s, entry in sorted(series.items()):
        key = series_key(vault_name, s)
        first = entry["_first_seen"]
        exit_row = exits.get(key, {})
        if entry.get("purchase_price_usd") in (None, "") and entry.get("acquisition_cost"):
            entry["purchase_price_usd"] = entry["acquisition_cost"]           # secours : coût d'acquisition
            entry["_sources"]["purchase_price_usd"] = "Use of Proceeds (coût d'acquisition, frais compris)"
        rows.append({
            "cik": f"{cik}-S{s}", "vault_cik": cik, "issuer_name_edgar": key, "vault_name": vault_name, "series": s,
            **{k: v for k, v in entry.items() if not k.startswith("_")},
            **terms,
            "date_initial_1a": first,
            "date_sec_qualification": next((q for q in qualifs if q >= first), ""),
            "sale_agreement_date": exit_row.get("sale_agreement_date"), "irr": exit_row.get("irr"),
            "llc_liquidated": "yes" if exit_row else "no",
            "sources": " | ".join(f"{k} <- {v}" for k, v in entry["_sources"].items()),
            "warnings": " | ".join(warnings),
        })
    return rows, n_docs


# =====================================================================================
# 5. PROGRAMME PRINCIPAL
# =====================================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--filings-file", help="filings.csv local, à la place de celui de S3.")
    parser.add_argument("--documents-file", help="documents.csv local, à la place de celui de S3.")
    parser.add_argument("--only", help="Seulement ces vaults, ex. --only \"Vault 5,Vault 12\".")
    args = parser.parse_args()

    filings = read_csv_source(args.filings_file, S3_FILINGS_KEY, "sec_filings_index.py")
    manifest = read_csv_source(args.documents_file, S3_MANIFEST_KEY, "sec_documents_downloader.py")
    files_by_accession = {}
    for m in manifest:
        files_by_accession.setdefault(m["accession_number"], []).append(m)

    vaults = {}
    for f in filings:
        if re.match(r"Masterworks Vault \d+, LLC", f["company_name"]):
            vaults.setdefault(f["cik"], []).append(f)
    if args.only:
        squash = lambda text: re.sub(r"\s+", "", text.lower())
        wanted = [squash(w) for w in args.only.split(",") if w.strip()]
        vaults = {c: v for c, v in vaults.items()
                  if any(re.search(rf"{re.escape(w)}(?!\d)", squash(v[0]["company_name"])) for w in wanted)}
    logging.info(f"{len(vaults)} vault(s) à traiter")

    # On repart des tables existantes : une vault non retraitée garde ses lignes
    existing = []
    if LOCAL_VAULT.exists():
        existing = list(csv.DictReader(open(LOCAL_VAULT, encoding="utf-8")))
    exits = {}
    if LOCAL_EXITS.exists():
        exits = {r["issuer_name"]: r for r in csv.DictReader(open(LOCAL_EXITS, encoding="utf-8"))}

    all_rows = [r for r in existing if r.get("vault_cik") not in vaults]
    for n, (cik, vault_filings) in enumerate(sorted(vaults.items(), key=lambda x: x[1][0]["company_name"]), 1):
        name = vault_filings[0]["company_name"]
        try:
            rows, n_docs = process_vault(cik, vault_filings, files_by_accession, exits)
            all_rows += rows
            logging.info(f"[{n}/{len(vaults)}] {name} : {n_docs} document(s) lu(s), {len(rows)} série(s)")
        except Exception as e:
            logging.error(f"[{n}/{len(vaults)}] {name} : erreur : {e}")

    all_rows.sort(key=lambda r: (r["vault_name"], r["series"]))
    write_csv(all_rows, VAULT_COLUMNS, LOCAL_VAULT, S3_VAULT_KEY)
    exit_rows = sorted(exits.values(), key=lambda r: (r.get("sale_agreement_date") or "", r["issuer_name"]))
    for r in exit_rows:
        r.setdefault("source", "historique des ventes (1-A POS de vault)")
    write_csv(exit_rows, EXITS_COLUMNS, LOCAL_EXITS, S3_EXITS_KEY)
    logging.info(f"{len(all_rows)} série(s) -> {LOCAL_VAULT} | {len(exit_rows)} vente(s) -> {LOCAL_EXITS}")


if __name__ == "__main__":
    main()