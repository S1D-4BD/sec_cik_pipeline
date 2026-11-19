#!/usr/bin/env python3
"""
python3 pipeline/lib/push_to_database.py
python3 pipeline/lib/push_to_database.py --no-s3-logs   # without the S3 extraction logs
python3 pipeline/lib/push_to_database.py --reset        # drop and recreate the tables (after a schema change)
"""

import argparse
import csv
import io
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
import psycopg
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

#### Settings ##############################################################
AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "sec-filings20260901174817304000000001")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost:5432/postgres")
S3_CLIENT = boto3.client("s3", region_name=AWS_REGION)

ROOT = Path(__file__).parent.parent.parent
TABLES_DIR = ROOT / "data" / "tables"
SCHEMA_FILE = ROOT / "pipeline" / "sql" / "schema.sql"

# All the tables of the database (children first, so they can be dropped in this order)
TABLES = ["extraction_value", "lifecycle", "offering", "artwork", "artist", "issuer", "document", "filing", "company"]

OFFERING_COLUMNS = ["offering_accession", "terms_accession", "incorporation_date", "date_initial_1a",
                    "date_sec_qualification", "date_offering_launch", "date_1z", "offering_status",
                    "purchase_date", "purchase_type", "purchase_price", "purchase_currency", "purchase_price_usd",
                    "acquisition_cost", "true_up", "class_a_shares_offered", "price_per_class_a_share",
                    "aggregate_offering", "class_b_shares_masterworks", "class_b_aggregate_cash_cost"]
LIFECYCLE_COLUMNS = ["offering_closing_date", "consignment_date", "sale_agreement_date", "sale_announcement_date",
                     "sale_closing_date", "cash_received_date", "distribution_reported_date", "dissolution_date",
                     "buyer", "sale_price", "net_liquidation_proceeds", "total_class_a_outstanding",
                     "class_b_profit_share", "distributable_per_class_a_share", "irr", "llc_liquidated"]
INTEGER_COLUMNS = ["execution_year", "class_a_shares_offered", "class_b_shares_masterworks",
                   "total_class_a_outstanding", "size"]
YES_NO_COLUMNS = ["llc_liquidated", "is_primary"]
###########################################################################


def s3_read(key):
    """The content of an S3 file, or None if it does not exist."""
    try:
        return S3_CLIENT.get_object(Bucket=BUCKET_NAME, Key=key)["Body"].read()
    except ClientError:
        return None


def read_csv(name):
    """A table of the pipeline: the copy on this computer (data/tables/), otherwise the one in S3."""
    local_file = TABLES_DIR / name
    if local_file.exists():
        return list(csv.DictReader(open(local_file, encoding="utf-8")))

    content = s3_read(f"tables/{name}")
    if content is None:
        logging.warning(f"{name} not found, on this computer or in S3: skipped")
        return []
    return list(csv.DictReader(io.StringIO(content.decode("utf-8"))))


def read_s3_logs():
    """The extraction logs written by offering_pipeline.py and lifecycle_pipeline.py in S3:
        extractions/{cik}/{accession}/offering_result.json   one per circular
        extractions/{cik}/offering_merged.json               all the circulars of a company
        extractions/{cik}/lifecycle_merged.json              sales and liquidation
    Returns {cik: {"circulars": {accession: content}, "offering": content, "lifecycle": content}}."""
    # 1. The list of the files
    keys = []
    for page in S3_CLIENT.get_paginator("list_objects_v2").paginate(Bucket=BUCKET_NAME, Prefix="extractions/"):
        for item in page.get("Contents", []):
            if item["Key"].endswith(("offering_result.json", "offering_merged.json", "lifecycle_merged.json")):
                keys.append(item["Key"])
    logging.info(f"{len(keys)} extraction log(s) to read in S3")

    # 2 Their content (10 files at a time, otherwise it takes minutes)
    with ThreadPoolExecutor(max_workers=10) as pool:
        contents = list(pool.map(s3_read, keys))

    # 3 Sorted by company
    logs = {}
    for key, content in zip(keys, contents):
        if content is None:
            continue
        parts = key.split("/")                   # ["extractions", cik, accession, "offering_result.json"]
        cik = parts[1]
        company = logs.setdefault(cik, {"circulars": {}, "offering": None, "lifecycle": None})
        if key.endswith("offering_result.json"):
            company["circulars"][parts[2]] = json.loads(content)
        elif key.endswith("offering_merged.json"):
            company["offering"] = json.loads(content)
        else:
            company["lifecycle"] = json.loads(content)
    return logs



def clean(row, column):
    """A CSV cell, ready for PostgreSQL: empty -> None (NULL), "99825" -> 99825, "yes" -> True.
    Dates and decimal numbers stay text: PostgreSQL converts them itself."""
    text = (row.get(column) or "").strip()
    if text == "":
        return None
    if column in INTEGER_COLUMNS:
        return int(float(text))
    if column in YES_NO_COLUMNS:
        return text.lower() in ("yes", "true")
    return text


def to_text(value):
    """Any value -> text, for the « value » column of the log."""
    if value is None or value == "":
        return None
    return str(value)


def name_key(name):
    """« Masterworks 003, LLC » -> « masterworks003llc »: to compare names written differently."""
    return re.sub(r"[^a-z0-9()]", "", name.lower())


def insert_rows(cur, table, rows):
    """Inserts rows into a table. Each row is a dict {column name: value}."""
    if not rows:
        logging.info(f"{table}: 0 row")
        return
    columns = list(rows[0].keys())
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    cur.executemany(sql, [list(row.values()) for row in rows])
    logging.info(f"{table}: {len(rows)} row(s)")


# =====================================================================================
# 3. FINDING THE DOCUMENT OF A LOG LINE
# The pipelines write where a value comes from as a label: « 1-U du 2022-05-19 · ex6-1.htm »
# (form, filing date and, sometimes, file name). These tools turn it into a document_id.
# =====================================================================================

def document_lookup(filings, documents, document_ids):
    """The tables needed to find a document, built once."""
    filings_by_label = {}                        # (cik, form, date) -> the filings
    for f in filings:
        filings_by_label.setdefault((f["cik"], f["form"], f["filing_date"]), []).append(f)

    largest_htm = {}                             # accession -> its largest .htm file
    for d in documents:
        if d["file_name"].lower().endswith((".htm", ".html")):
            current = largest_htm.get(d["accession_number"])
            if current is None or int(d["size"] or 0) > int(current["size"] or 0):
                largest_htm[d["accession_number"]] = d

    return {"filings_by_label": filings_by_label, "largest_htm": largest_htm, "ids": document_ids}


def find_document(lookup, cik, form, date, file_name=None, largest_htm=False):
    """The document_id of a file. Without a file name: the main document of the filing."""
    for f in lookup["filings_by_label"].get((cik, form, date), []):
        accession = f["accession_number"]
        if file_name:
            name = file_name
        elif largest_htm and accession in lookup["largest_htm"]:
            name = lookup["largest_htm"][accession]["file_name"]     # what vault_pipeline.py reads
        else:
            name = Path(f["primary_document"]).name
        if (accession, name) in lookup["ids"]:
            return lookup["ids"][(accession, name)]
    return None


def find_document_from_label(lookup, cik, label, largest_htm=False):
    """« 1-U du 2022-05-19 · ex6-1.htm (tableau) » -> the document_id of ex6-1.htm."""
    match = re.match(r"(.+?) du (\d{4}-\d\d-\d\d)(?: · (\S+))?", label or "")
    if match is None:                            # e.g. « aucune dissolution écrite »
        return None
    form, date, file_name = match.groups()
    return find_document(lookup, cik, form, date, file_name, largest_htm)


def field_in_note(note, fields):
    """« purchase_price : two values found… » -> "purchase_price" (None if the note is about no field)."""
    match = re.match(r"(\w+) : ", note)
    if match and match.group(1) in fields:
        return match.group(1)
    return None


# =====================================================================================
# 4. THE EXTRACTION LOG: one row per value read, with what was decided
# =====================================================================================

def log_row(issuer_id, step, field, value, decision, note=None, document_id=None, source_text=None):
    return {"issuer_id": issuer_id, "step": step, "field": field, "value": value, "decision": decision,
            "note": note, "document_id": document_id, "source_text": source_text}


def offering_log(issuer_id, cik, company_logs, lookup, filings_by_accession):
    rows = []

    # Each circular: the values kept, rejected, to check (warning) and left empty (missing)
    for accession, result in company_logs["circulars"].items():
        filing = filings_by_accession.get(accession)
        document_id = None
        if filing:
            document_id = lookup["ids"].get((accession, Path(filing["primary_document"]).name))
        fields = result.get("record", {}).keys()

        for kept in result.get("audit", []):
            rows.append(log_row(issuer_id, "offering", kept["field"], to_text(kept.get("value")), "kept",
                                kept.get("source"), document_id, kept.get("source_text")))
        for rejected in result.get("rejected", []):
            # « purchase_price = 181500 supprimé : identique au true-up »
            match = re.match(r"(\w+) = (.*) supprimé : (.*)", rejected, re.S)
            if match:
                field, value, reason = match.groups()
                rows.append(log_row(issuer_id, "offering", field, value, "rejected", reason, document_id))
            else:
                rows.append(log_row(issuer_id, "offering", None, None, "rejected", rejected, document_id))
        for warning in result.get("warnings", []):
            rows.append(log_row(issuer_id, "offering", field_in_note(warning, fields), None, "warning",
                                warning, document_id))
        for field in result.get("missing", []):
            rows.append(log_row(issuer_id, "offering", field, None, "missing", None, document_id))

    # Between circulars: a value replaced by a later circular, or ignored
    merged = company_logs["offering"] or {}
    for change in merged.get("changes", []):
        # « true_up : 623944.0 (253G2 du 2020-03-10) -> 622234.0 (253G3 du 2020-04-03) [réel] »
        # « price_per_class_a_share : 26.67 lu dans 253G2 du 2022-12-30 IGNORÉ, 20.0 conservé […] »
        match = re.match(r"(\w+) : (.*?) (?:lu dans |\()(.+?) du (\d{4}-\d\d-\d\d)", change, re.S)
        if match is None:
            continue
        field, value, form, date = match.groups()
        decision = "ignored" if " IGNORÉ" in change else "replaced"
        rows.append(log_row(issuer_id, "offering", field, value, decision, change,
                            find_document(lookup, cik, form, date)))
    return rows


def lifecycle_log(issuer_id, cik, lifecycle, lookup):
    rows = []
    fields = lifecycle.get("fields", {})
    for field, source in fields.items():
        rows.append(log_row(issuer_id, "lifecycle", field, to_text(source.get("value")), "kept",
                            source.get("document"), find_document_from_label(lookup, cik, source.get("document")),
                            source.get("evidence")))
    for warning in lifecycle.get("warnings", []):
        rows.append(log_row(issuer_id, "lifecycle", field_in_note(warning, fields), None, "warning", warning))
    return rows


def vault_log(issuer_id, vault_row, lookup):
    """The vaults have no log in S3: it is in the « sources » and « warnings » columns of vault_table.csv,
    « artist_name <- 1-A/A du 2023-03-01 | title <- 1-A/A du 2023-03-01 | … »."""
    rows = []
    for source in vault_row.get("sources", "").split(" | "):
        if not source:
            continue
        field, _, label = source.partition(" <- ")
        document_id = find_document_from_label(lookup, vault_row["vault_cik"], label, largest_htm=True)
        rows.append(log_row(issuer_id, "vault", field, to_text(vault_row.get(field)), "kept", label, document_id))
    for warning in vault_row.get("warnings", "").split(" | "):
        if warning:
            rows.append(log_row(issuer_id, "vault", field_in_note(warning, vault_row), None, "warning", warning))
    return rows


# =====================================================================================
# 5. MAIN PROGRAM
# =====================================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-s3-logs", action="store_true", help="Skip the extraction logs of S3 (faster).")
    parser.add_argument("--reset", action="store_true", help="Drop and recreate the tables (after a schema change).")
    args = parser.parse_args()

    # ---- Read everything first ----
    filings = read_csv("filings.csv")
    documents = read_csv("documents.csv")
    offerings = read_csv("offering_table.csv")
    lifecycles = read_csv("lifecycle_table.csv")
    vaults = read_csv("vault_table.csv")
    exits = read_csv("exits_table.csv")
    logs = {} if args.no_s3_logs else read_s3_logs()
    if not filings:
        raise RuntimeError("filings.csv not found: run the pipeline first.")

    # ---- The issuers: what an investor buys ----
    # A classic LLC (series "") or one series of a vault. Each one keeps its rows of the CSV files.
    lifecycle_by_cik = {row["cik"]: row for row in lifecycles}
    issuers = {}                                 # (cik, series) -> issuer
    for row in offerings:
        issuers[(row["cik"], "")] = {"name": row["issuer_name_edgar"], "offering": row,
                                     "lifecycle": lifecycle_by_cik.get(row["cik"], {}), "vault": None}
    for row in lifecycles:
        if (row["cik"], "") not in issuers:
            issuers[(row["cik"], "")] = {"name": row["issuer_name_edgar"], "offering": None,
                                         "lifecycle": row, "vault": None}
    for row in vaults:                           # a vault row has the offering AND the sale
        issuers[(row["vault_cik"], row["series"])] = {"name": row["issuer_name_edgar"], "offering": row,
                                                      "lifecycle": row, "vault": row}

    # ---- Write to the database (all or nothing: if anything fails, nothing is changed) ----
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:

        # The tables: created if they do not exist, then emptied
        if args.reset:
            cur.execute(f"DROP TABLE IF EXISTS {', '.join(TABLES)} CASCADE")
        cur.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
        cur.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")

        # company and filing: straight from filings.csv
        companies = {}
        for f in filings:
            companies[f["cik"]] = f["company_name"]
        company_rows = []
        for cik, name in companies.items():
            kind = "vault" if "Vault" in name else "llc"
            company_rows.append({"cik": cik, "name": name, "kind": kind})
        insert_rows(cur, "company", company_rows)

        filing_rows = []
        for f in filings:
            filing_rows.append({column: clean(f, column) for column in [
                "accession_number", "cik", "form", "filing_date", "report_date", "primary_document",
                "document_url", "filing_index_url"]})
        insert_rows(cur, "filing", filing_rows)

        # document: documents.csv
        known_accessions = {f["accession_number"] for f in filings}
        document_rows = []
        for d in documents:
            if d["accession_number"] in known_accessions:
                document_rows.append({"accession_number": d["accession_number"], "file_name": d["file_name"],
                                      "is_primary": clean(d, "is_primary"), "size_bytes": clean(d, "size"),
                                      "s3_key": d["s3_key"]})
        insert_rows(cur, "document", document_rows)

        # PostgreSQL has numbered the documents: we read their numbers back
        cur.execute("SELECT accession_number, file_name, document_id FROM document")
        document_ids = {}
        for accession, file_name, document_id in cur.fetchall():
            document_ids[(accession, file_name)] = document_id

        # issuer, and the numbers PostgreSQL gave them
        issuer_rows = []
        for (cik, series), issuer in issuers.items():
            issuer_rows.append({"cik": cik, "series": series, "name": issuer["name"]})
        insert_rows(cur, "issuer", issuer_rows)
        cur.execute("SELECT cik, series, issuer_id FROM issuer")
        issuer_ids = {}
        for cik, series, issuer_id in cur.fetchall():
            issuer_ids[(cik, series)] = issuer_id

        # artist and artwork: the circulars first, otherwise the sale reports (as in the Excel export)
        artworks = {}
        for key, issuer in issuers.items():
            offering = issuer["offering"] or {}
            lifecycle = issuer["lifecycle"]
            artwork = {column: offering.get(column, "") for column in [
                "artist_name", "title", "execution_year", "dimensions_text", "height_cm", "width_cm"]}
            if not artwork["artist_name"]:
                artwork["artist_name"] = lifecycle.get("artwork_artist", "")
            # The sale reports write « Coup du Vent (1882) »: title « Coup du Vent », year 1882
            report_title = lifecycle.get("artwork_title", "")
            match = re.fullmatch(r"\s*(.+?)\s*\((1[5-9]\d\d|20[0-2]\d)\)\s*", report_title)
            if not artwork["title"] and report_title:
                artwork["title"] = match.group(1) if match else report_title
            if not artwork["execution_year"] and match:
                artwork["execution_year"] = match.group(2)
            artwork["artist_name"] = artwork["artist_name"].strip()
            if artwork["artist_name"] or artwork["title"]:
                artworks[key] = artwork

        artist_names = sorted({a["artist_name"] for a in artworks.values() if a["artist_name"]})
        insert_rows(cur, "artist", [{"name": name} for name in artist_names])
        cur.execute("SELECT name, artist_id FROM artist")
        artist_ids = dict(cur.fetchall())

        artwork_rows = []
        for key, artwork in artworks.items():
            row = {"issuer_id": issuer_ids[key], "artist_id": artist_ids.get(artwork["artist_name"])}
            for column in ["title", "execution_year", "dimensions_text", "height_cm", "width_cm"]:
                row[column] = clean(artwork, column)
            artwork_rows.append(row)
        insert_rows(cur, "artwork", artwork_rows)

        # offering: only the issuers whose offering was read
        offering_rows = []
        for key, issuer in issuers.items():
            offering = issuer["offering"]
            if offering is None or offering.get("status", "ok") != "ok":
                continue
            row = {"issuer_id": issuer_ids[key]}
            for column in OFFERING_COLUMNS:
                row[column] = clean(offering, column)
            offering_rows.append(row)
        insert_rows(cur, "offering", offering_rows)

        # lifecycle: lifecycle_table.csv / vault_table.csv, completed by Masterworks' sales history
        exits_by_name = {name_key(e["issuer_name"]): e for e in exits}
        lifecycle_rows = []
        for key, issuer in issuers.items():
            sale = exits_by_name.pop(name_key(issuer["name"]), {})
            if not issuer["lifecycle"] and not sale:
                continue
            lifecycle = dict(issuer["lifecycle"])
            lifecycle["sale_announcement_date"] = sale.get("sale_announcement_date", "")
            lifecycle["sale_agreement_date"] = sale.get("sale_agreement_date") or lifecycle.get("sale_agreement_date", "")
            lifecycle["irr"] = lifecycle.get("irr") or sale.get("irr", "")
            row = {"issuer_id": issuer_ids[key]}
            for column in LIFECYCLE_COLUMNS:
                row[column] = clean(lifecycle, column)
            lifecycle_rows.append(row)
        insert_rows(cur, "lifecycle", lifecycle_rows)
        if exits_by_name:                        # sales left: their issuer is not in the tables
            names = sorted(e["issuer_name"] for e in exits_by_name.values())
            logging.warning(f"exits_table.csv: no issuer for {names}")

        # extraction_value: the extraction log
        lookup = document_lookup(filings, documents, document_ids)
        filings_by_accession = {f["accession_number"]: f for f in filings}
        log_rows = []
        for (cik, series), issuer in issuers.items():
            issuer_id = issuer_ids[(cik, series)]
            if issuer["vault"]:
                log_rows += vault_log(issuer_id, issuer["vault"], lookup)
            elif cik in logs:
                log_rows += offering_log(issuer_id, cik, logs[cik], lookup, filings_by_accession)
                if logs[cik]["lifecycle"]:
                    log_rows += lifecycle_log(issuer_id, cik, logs[cik]["lifecycle"], lookup)
        insert_rows(cur, "extraction_value", log_rows)

    logging.info("Done")


if __name__ == "__main__":
    main()
