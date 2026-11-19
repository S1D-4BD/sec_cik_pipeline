#!/usr/bin/env python3
"""
Usage :
    python scrapers/sec_filings_index.py                                   # tous les CIK de S3
    python scrapers/sec_filings_index.py --limit 3                         # test sur 3 sociétés (les autres sont conservées)
    python scrapers/sec_filings_index.py --ciks-file data/all_ciks.json        # CIK depuis un fichier local
"""

import argparse
import csv
import io
import json
import logging
import os
import time
from pathlib import Path

import boto3
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

#### AWS (mêmes réglages que vos autres scrapers) ######################
AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "sec-filings20260901174817304000000001")
S3_CLIENT = boto3.client("s3", region_name=AWS_REGION)
S3_CIKS_KEY = "config/all_ciks.json"
S3_OUTPUT_KEY = "tables/filings.csv"
#########################################################################

#### SEC ##################################################################
# La SEC exige un User-Agent avec un vrai nom et un vrai e-mail
USER_AGENT = os.getenv("SEC_USER_AGENT", "Celina Sid celina_sid@hotmail.com")
HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
SUBMISSIONS_URL = "https://data.sec.gov/submissions/{name}"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}"
DELAY = 0.15        # secondes entre deux requêtes : la SEC limite à 10 requêtes par seconde
MAX_RETRIES = 3
###########################################################################

LOCAL_OUTPUT = Path(__file__).parent.parent / "data" / "tables" / "filings.csv"

COLUMNS = ["cik", "list_name", "company_name", "form", "filing_date", "report_date",
           "accession_number", "primary_document", "primary_doc_description",
           "document_url", "filing_index_url"]


def get_json(session, url):
    """GET avec quelques nouvelles tentatives si la SEC répond « trop de requêtes » ou est indisponible."""
    for attempt in range(1, MAX_RETRIES + 1):
        response = session.get(url, timeout=30)
        time.sleep(DELAY)
        if response.status_code == 200:
            return response.json()
        if response.status_code == 404:
            return None                                   # CIK inconnu : inutile d'insister
        if response.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
            wait = 2 ** attempt
            logging.warning(f"HTTP {response.status_code} sur {url}, nouvel essai dans {wait} s")
            time.sleep(wait)
            continue
        raise RuntimeError(f"HTTP {response.status_code} sur {url}")
    return None


def rows_from_block(block, cik, list_name, company_name):
    """Transforme un bloc de l'API (des colonnes parallèles) en une ligne par document."""
    cik_int = str(int(cik))
    rows = []
    for i in range(len(block.get("accessionNumber", []))):
        accession = block["accessionNumber"][i]
        accession_clean = accession.replace("-", "")
        primary = block.get("primaryDocument", [""] * (i + 1))[i]
        base = ARCHIVES_URL.format(cik=cik_int, accession=accession_clean)
        rows.append({
            "cik": cik,
            "list_name": list_name,
            "company_name": company_name,
            "form": block["form"][i],
            "filing_date": block["filingDate"][i],
            "report_date": (block.get("reportDate") or [""] * (i + 1))[i],
            "accession_number": accession,
            "primary_document": primary,
            "primary_doc_description": (block.get("primaryDocDescription") or [""] * (i + 1))[i],
            "document_url": f"{base}/{primary}" if primary else "",
            "filing_index_url": f"{base}/{accession}-index.htm",
        })
    return rows


def filings_for_cik(session, cik, list_name):
    """Tous les dépôts d'une société. L'API donne les plus récents dans « recent »,
    et range les plus anciens dans des pages supplémentaires (« files »), qu'on lit aussi."""
    data = get_json(session, SUBMISSIONS_URL.format(name=f"CIK{cik}.json"))
    if data is None:
        logging.warning(f"CIK {cik} ({list_name}) introuvable dans l'API")
        return []
    company_name = data.get("name", "")
    rows = rows_from_block(data["filings"]["recent"], cik, list_name, company_name)
    for extra in data["filings"].get("files", []):
        older = get_json(session, SUBMISSIONS_URL.format(name=extra["name"]))
        if older:
            rows += rows_from_block(older, cik, list_name, company_name)
    return rows


def load_ciks(ciks_file):
    if ciks_file:
        logging.info(f"CIK lus depuis le fichier local {ciks_file}")
        return json.loads(Path(ciks_file).read_text(encoding="utf-8"))
    response = S3_CLIENT.get_object(Bucket=BUCKET_NAME, Key=S3_CIKS_KEY)
    logging.info(f"CIK lus depuis s3://{BUCKET_NAME}/{S3_CIKS_KEY}")
    return json.loads(response["Body"].read().decode("utf-8"))


def merge_with_existing(rows):
    """Mise à jour PARTIELLE : les dépôts des sociétés traitées remplacent les anciens, toutes les autres sont gardées.
    La table de référence est celle de S3 (c'est elle que lisent les étapes suivantes), sinon la copie locale."""
    try:
        content = S3_CLIENT.get_object(Bucket=BUCKET_NAME, Key=S3_OUTPUT_KEY)["Body"].read().decode("utf-8")
    except S3_CLIENT.exceptions.NoSuchKey:
        content = LOCAL_OUTPUT.read_text(encoding="utf-8") if LOCAL_OUTPUT.exists() else ""
    existing = list(csv.DictReader(io.StringIO(content)))
    done = {r["cik"] for r in rows}
    return [r for r in existing if r.get("cik") not in done] + rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ciks-file", help="Fichier JSON local {CIK: nom}, à la place de celui de S3.")
    parser.add_argument("--limit", type=int, help="Ne traiter que les N premières sociétés (pour tester).")
    args = parser.parse_args()

    ciks = load_ciks(args.ciks_file)
    items = list(ciks.items())[: args.limit] if args.limit else list(ciks.items())

    session = requests.Session()
    session.headers.update(HEADERS)

    all_rows, failures = [], []
    for n, (cik, list_name) in enumerate(items, 1):
        cik10 = str(cik).zfill(10)
        try:
            rows = filings_for_cik(session, cik10, list_name)
            all_rows += rows
            forms = sorted({r["form"] for r in rows})
            logging.info(f"[{n}/{len(items)}] {list_name} (CIK {cik10}) : {len(rows)} dépôts {forms}")
        except Exception as e:
            failures.append(f"{cik10} {list_name} : {e}")
            logging.error(f"[{n}/{len(items)}] {list_name} (CIK {cik10}) : {e}")

    # Garde-fou, comme dans sec_cik_scraper.py : un résultat vide n'écrase jamais le fichier existant
    if not all_rows:
        raise RuntimeError("Aucun dépôt récupéré : écriture annulée.")

    # Avec --limit, les autres sociétés gardent leurs dépôts : un test n'écrase plus la table complète
    if args.limit:
        all_rows = merge_with_existing(all_rows)

    all_rows.sort(key=lambda r: (r["cik"], r["filing_date"], r["accession_number"]))
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(all_rows)
    content = buffer.getvalue().encode("utf-8")

    LOCAL_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_OUTPUT.write_bytes(content)
    S3_CLIENT.put_object(Bucket=BUCKET_NAME, Key=S3_OUTPUT_KEY, Body=content, ContentType="text/csv")

    logging.info(f"{len(all_rows)} dépôts ({len(items) - len(failures)} société(s) mise(s) à jour) "
                 f"-> {LOCAL_OUTPUT} et s3://{BUCKET_NAME}/{S3_OUTPUT_KEY}")
    if failures:
        logging.warning(f"{len(failures)} société(s) en échec :")
        for f in failures:
            logging.warning(f"   {f}")


if __name__ == "__main__":
    main()