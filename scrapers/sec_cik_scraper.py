#!/usr/bin/env python3


import json
import logging
import os
import time
import boto3
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "sec-filings20260901174817304000000001")
S3_CLIENT = boto3.client("s3", region_name=AWS_REGION)


# https://www.sec.gov/edgar/searchedgar/accessing-edgar-data.htm
USER_AGENT = os.getenv("SEC_USER_AGENT", "Celina Sid celina_sid@hotmail.com")
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
}

BASE_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
PAGE_SIZE = 40
DELAI_ENTRE_PAGES = 0.5


def _extraire_ciks(html):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.tableFile2")
    if table is None:
        return None  # stop cond

    trouves = {}
    for tr in table.find_all("tr"):
        lien = tr.find("a", href=lambda h: h and "CIK=" in h)
        if lien is None:
            continue

        cellules = tr.find_all("td")
        if len(cellules) < 2:
            continue

        cik = lien.get_text(strip=True)
        nom = cellules[1].get_text(separator="\n", strip=True).split("\n")[0].strip()

        if cik.isdigit() and "masterworks" in nom.lower():
            trouves[cik] = nom

    return trouves


def run():
    session = requests.Session()
    session.headers.update(HEADERS)

    all_ciks = {}
    start = 0

    while True:
        params = {
            "action": "getcompany",
            "company": "Masterworks",
            "owner": "include",
            "match": "starts-with",
            "start": start,
            "count": PAGE_SIZE,
            "hidefilings": 0,
        }

        reponse = session.get(BASE_URL, params=params, timeout=30)

        if reponse.status_code != 200:
            logging.warning(f"HTTP {reponse.status_code} sur start={start}, arret")
            break

        if "Undeclared Automated Tool" in reponse.text:
            raise RuntimeError(
                "Requete bloquee par la SEC. Verifier SEC_USER_AGENT."
            )

        trouves = _extraire_ciks(reponse.text)

        if trouves is None:
            logging.info("Pas de tableau de resultats, arret")
            break

        nouveaux = {c: n for c, n in trouves.items() if c not in all_ciks}
        all_ciks.update(nouveaux)

        logging.info(
            f"Page start={start} : {len(all_ciks)} CIK au total "
            f"({len(nouveaux)} nouveaux)"
        )

        if not nouveaux:
            logging.info("Fin de la pagination")
            break

        start += PAGE_SIZE
        time.sleep(DELAI_ENTRE_PAGES)

    logging.info(f"TOTAL FINAL : {len(all_ciks)} CIK")

    # Garde-fou : sans ceci, un scraping vide ecraserait le fichier S3 existant
    # et le pipeline renverrait un faux succes.
    if not all_ciks:
        raise RuntimeError("Aucun CIK recupere : ecriture S3 annulee.")

    S3_CLIENT.put_object(
        Bucket=BUCKET_NAME,
        Key="config/all_ciks.json",
        Body=json.dumps(all_ciks, indent=4, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )
    logging.info("all_ciks.json ecrit dans S3")

    return all_ciks


if __name__ == "__main__":
    run()