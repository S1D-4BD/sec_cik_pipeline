#!/usr/bin/env python3
"""
Télécharge les documents utiles de chaque dépôt EDGAR, d'après la table produite par
sec_filings_index.py (tables/filings.csv).

Pour chaque dépôt retenu, le script lit la liste de ses fichiers sur EDGAR et télécharge
les TEXTES (HTML, XML) : le document principal ET ses pièces jointes. En effet, pour un 1-K,
le document principal est un petit formulaire XML ; le vrai rapport est une pièce jointe.
Les images, les pages d'index et le fichier de soumission complet sont ignorés.

Rangement dans S3 : raw/<CIK>/<numéro_de_dépôt>/<fichier>
Inventaire        : tables/documents.csv (une ligne par fichier téléchargé)

Le script peut être interrompu puis relancé : un dépôt déjà téléchargé en entier
(marqué par un fichier « _complete ») n'est pas retéléchargé.

Usage :
    python scrapers/sec_documents_downloader.py --limit 1          # test sur 1 société
    python scrapers/sec_documents_downloader.py                    # toutes les sociétés
    python scrapers/sec_documents_downloader.py --local            # garde aussi une copie sur le Mac
"""

import argparse
import csv
import io
import logging
import os
import time
from pathlib import Path

import boto3
import requests
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

#### AWS (mêmes réglages que vos autres scrapers) ######################
AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "sec-filings20260901174817304000000001")
S3_CLIENT = boto3.client("s3", region_name=AWS_REGION)
S3_FILINGS_KEY = "tables/filings.csv"
S3_MANIFEST_KEY = "tables/documents.csv"
RAW_PREFIX = "raw"
#########################################################################

#### SEC ##################################################################
USER_AGENT = os.getenv("SEC_USER_AGENT", "Celina Sid celina_sid@hotmail.com")
HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}"
DELAY = 0.15        # la SEC limite à 10 requêtes par seconde
MAX_RETRIES = 3
###########################################################################

#### Ce qu'on télécharge ##################################################
# Les types de dépôts utiles pour le tableau Masterworks :
#   253G2 / 253G3 : circulaires d'offre (œuvre, achat, true-up, actions)
#   1-U           : événements (consignation, vente, acheteur, prix)
#   1-K / 1-SA    : rapports annuels et semestriels (clôture, liquidation)
#   1-Z           : fin de l'offre
FORMS = {"253G2", "253G3", "1-U", "1-U/A", "1-K", "1-K/A", "1-SA", "1-SA/A", "1-Z", "1-Z/A"}
VAULT_FORMS = {"1-A", "1-A/A", "1-A POS"}
TEXT_EXTENSIONS = (".htm", ".html", ".xml", ".txt", ".pdf")
###########################################################################

LOCAL_DIR = Path(__file__).parent.parent / "data" / "documents"
LOCAL_MANIFEST = Path(__file__).parent.parent / "data" / "tables" / "documents.csv"
MANIFEST_COLUMNS = ["cik", "company_name", "form", "filing_date", "accession_number",
                    "file_name", "is_primary", "size", "s3_key"]


def get(session, url):
    """GET prudent : délai entre requêtes, nouvelles tentatives si la SEC est surchargée."""
    for attempt in range(1, MAX_RETRIES + 1):
        response = session.get(url, timeout=60)
        time.sleep(DELAY)
        if response.status_code == 200:
            return response
        if response.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
            wait = 2 ** attempt
            logging.warning(f"HTTP {response.status_code} sur {url}, nouvel essai dans {wait} s")
            time.sleep(wait)
            continue
        raise RuntimeError(f"HTTP {response.status_code} sur {url}")


def useful_files(index_items, accession):
    """Garde les textes du dépôt ; écarte images, pages d'index et fichier de soumission complet."""
    kept = []
    for item in index_items:
        name = item["name"]
        lower = name.lower()
        if not lower.endswith(TEXT_EXTENSIONS):
            continue                                            # images (.jpg, .gif...)
        if "-index" in lower:
            continue                                            # pages d'index d'EDGAR
        if lower == f"{accession}.txt":
            continue                                            # soumission complète : doublon de tout le reste
        kept.append(item)
    return kept


def s3_exists(key):
    try:
        S3_CLIENT.head_object(Bucket=BUCKET_NAME, Key=key)
        return True
    except ClientError:
        return False


def read_s3_csv(key):
    try:
        body = S3_CLIENT.get_object(Bucket=BUCKET_NAME, Key=key)["Body"].read().decode("utf-8")
        return list(csv.DictReader(io.StringIO(body)))
    except ClientError:
        return []


def download_filing(session, filing, local):
    """Télécharge les fichiers utiles d'UN dépôt. Renvoie les lignes d'inventaire."""
    cik = filing["cik"]
    accession = filing["accession_number"]
    accession_clean = accession.replace("-", "")
    prefix = f"{RAW_PREFIX}/{cik}/{accession}"
    base_url = FILING_URL.format(cik=int(cik), accession=accession_clean)
    primary_name = Path(filing["primary_document"]).name

    index = get(session, f"{base_url}/index.json").json()["directory"]["item"]
    rows = []
    for item in useful_files(index, accession):
        name = item["name"]
        content = get(session, f"{base_url}/{name}").content
        key = f"{prefix}/{name}"
        S3_CLIENT.put_object(Bucket=BUCKET_NAME, Key=key, Body=content)
        if local:
            path = LOCAL_DIR / cik / accession / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        rows.append({
            "cik": cik, "company_name": filing["company_name"], "form": filing["form"],
            "filing_date": filing["filing_date"], "accession_number": accession,
            "file_name": name, "is_primary": name == primary_name,
            "size": len(content), "s3_key": key,
        })

    # Le marqueur est écrit EN DERNIER : s'il existe, tout le dépôt est bien là.
    # Si le script est interrompu au milieu, le dépôt sera repris au prochain lancement.
    S3_CLIENT.put_object(Bucket=BUCKET_NAME, Key=f"{prefix}/_complete", Body=b"")
    return rows


def save_manifest(manifest):
    """Écrit l'inventaire en local et dans S3."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=MANIFEST_COLUMNS)
    writer.writeheader()
    writer.writerows(sorted(manifest.values(), key=lambda r: (r["cik"], r["filing_date"], r["s3_key"])))
    content = buffer.getvalue().encode("utf-8")
    LOCAL_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_MANIFEST.write_bytes(content)
    S3_CLIENT.put_object(Bucket=BUCKET_NAME, Key=S3_MANIFEST_KEY, Body=content, ContentType="text/csv")


SAVE_EVERY = 25     # l'inventaire est enregistré tous les 25 dépôts téléchargés


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--filings-file", help="filings.csv local, à la place de celui de S3.")
    parser.add_argument("--limit", type=int, help="Ne traiter que les N premières sociétés (pour tester).")
    parser.add_argument("--local", action="store_true", help="Garder aussi une copie dans scrapers/documents/.")
    args = parser.parse_args()

    if args.filings_file:
        filings = list(csv.DictReader(open(args.filings_file, encoding="utf-8")))
    else:
        filings = read_s3_csv(S3_FILINGS_KEY)
    if not filings:
        raise RuntimeError("filings.csv introuvable ou vide : lancez d'abord sec_filings_index.py.")

    ciks = list(dict.fromkeys(f["cik"] for f in filings))
    if args.limit:
        ciks = ciks[: args.limit]
    todo = []
    for f in filings:
        if f["cik"] not in set(ciks):
            continue                      # société pas sélectionnée
        if f["form"] in FORMS:
            todo.append(f)                # type habituel : pour tout le monde
        elif f["form"] in VAULT_FORMS and "vault" in f["company_name"].lower():
            todo.append(f)                # 1-A : seulement pour les vaults
    logging.info(f"{len(todo)} dépôts à traiter pour {len(ciks)} société(s), types : {sorted(FORMS | VAULT_FORMS)}")

    # L'inventaire existant est conservé : on y ajoute les nouveaux fichiers
    manifest = {r["s3_key"]: r for r in read_s3_csv(S3_MANIFEST_KEY)}

    session = requests.Session()
    session.headers.update(HEADERS)
    downloaded, skipped, failures = 0, 0, []

    # try / finally : même si vous arrêtez le script (Ctrl + C), l'inventaire est enregistré
    try:
        for n, filing in enumerate(todo, 1):
            label = f"[{n}/{len(todo)}] {filing['company_name']} {filing['form']} {filing['filing_date']}"
            if s3_exists(f"{RAW_PREFIX}/{filing['cik']}/{filing['accession_number']}/_complete"):
                skipped += 1
                continue
            try:
                rows = download_filing(session, filing, args.local)
                for r in rows:
                    manifest[r["s3_key"]] = r
                downloaded += 1
                logging.info(f"{label} : {len(rows)} fichier(s)")
                if downloaded % SAVE_EVERY == 0:
                    save_manifest(manifest)
            except Exception as e:
                failures.append(f"{label} : {e}")
                logging.error(f"{label} : {e}")
    finally:
        save_manifest(manifest)

    logging.info(f"Terminé : {downloaded} dépôt(s) téléchargé(s), {skipped} déjà présent(s), "
                 f"{len(failures)} en échec. Inventaire : {len(manifest)} fichiers -> {S3_MANIFEST_KEY}")
    for f in failures:
        logging.warning(f"   {f}")


if __name__ == "__main__":
    main()