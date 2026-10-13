#!/usr/bin/env python3
"""
Enchaîne, pour chaque société, les étapes qui remplissent la partie « offre » du tableau Masterworks :

    filings.csv  ->  TOUTES les circulaires 253G2 et 253G3, de la plus ancienne à la plus récente
                 ->  extract_sections.py --profile offering   (les 4 sections utiles ; le texte entier
                                                               pour un supplément court)
                 ->  offering_extraction.py                   (Nova + contrôles stricts, nom EDGAR)
                 ->  fusion : la valeur du document LE PLUS RÉCENT l'emporte, chaque changement est noté
                 ->  une ligne par société dans tables/offering_table.csv

La table contient aussi les dates lues directement dans EDGAR (1-A, QUALIF, lancement, 1-Z) :
aucune n'est demandée au modèle.

Le script peut être interrompu puis relancé : une société déjà traitée (son résultat existe
dans S3, sous extractions/) n'est pas renvoyée à Nova. --force les retraite toutes.

Usage (depuis la racine du projet) :
    python ml_pipeline/lib/offering_pipeline.py --limit 3        # test sur 3 sociétés
    python ml_pipeline/lib/offering_pipeline.py                  # toutes les sociétés
    python ml_pipeline/lib/offering_pipeline.py --force          # retraite tout (après un changement de règles)

Nécessite, dans le même dossier : extract_sections.py et offering_extraction.py.
"""

import argparse
import csv
import io
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).parent))
import extract_sections as sections_lib                    # noqa: E402
try:
    import offering_extraction as offering_lib             # noqa: E402  (le nom de votre fichier)
except ImportError:
    import offering_extract as offering_lib                # noqa: E402  (le nom d'origine)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

#### AWS (mêmes réglages que vos scrapers) ##############################
AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "sec-filings20260901174817304000000001")
S3_CLIENT = boto3.client("s3", region_name=AWS_REGION)
S3_FILINGS_KEY = "tables/filings.csv"
S3_TABLE_KEY = "tables/offering_table.csv"
RAW_PREFIX = "raw"
EXTRACTIONS_PREFIX = "extractions"
#########################################################################

LOCAL_TABLE = Path(__file__).parent.parent / "tables" / "offering_table.csv"

CIRCULAR_FORMS = ("253G2", "253G3")
SHORT_DOCUMENT = 30_000      # en dessous (en caractères) : un supplément court, envoyé en entier

EDGAR_DATE_COLUMNS = ["date_initial_1a", "date_sec_qualification", "date_offering_launch", "date_1z"]
STATUS_COLUMNS = ["status", "verified", "rejected", "missing", "warnings", "changes"]
TABLE_COLUMNS = (["cik", "issuer_name_edgar", "offering_accession", "offering_filing_date", "n_253g2",
                  "n_circulars", "terms_accession", "terms_filing_date"]
                 + EDGAR_DATE_COLUMNS
                 + [f for f in offering_lib.OfferingRecord.model_fields if f != "issuer_name"]
                 + STATUS_COLUMNS)


# -------------------------------------------------------------------------
# S3
# -------------------------------------------------------------------------
def s3_read(key):
    try:
        return S3_CLIENT.get_object(Bucket=BUCKET_NAME, Key=key)["Body"].read()
    except ClientError:
        return None


def s3_write(key, content, content_type="text/plain"):
    if isinstance(content, str):
        content = content.encode("utf-8")
    S3_CLIENT.put_object(Bucket=BUCKET_NAME, Key=key, Body=content, ContentType=content_type)


# -------------------------------------------------------------------------
# Les dates EDGAR et le choix de la circulaire, d'après filings.csv
# -------------------------------------------------------------------------
def edgar_summary(company_filings):
    """Les dates lues directement dans EDGAR, et la liste des circulaires (253G2 et 253G3),
    de la plus ancienne à la plus récente."""
    ordered = sorted(company_filings, key=lambda f: (f["filing_date"], f["accession_number"]))
    by_form = {}
    for f in ordered:
        by_form.setdefault(f["form"], []).append(f)
    first = lambda form: by_form[form][0]["filing_date"] if form in by_form else ""
    circulars = [f for f in ordered if f["form"] in CIRCULAR_FORMS]
    return {
        "date_initial_1a": first("1-A"),
        "date_sec_qualification": first("QUALIF"),
        "date_offering_launch": first("253G2"),
        "date_1z": first("1-Z"),
        "n_253g2": len(by_form.get("253G2", [])),
        "n_circulars": len(circulars),
    }, circulars


def document_text(html):
    """Le texte à envoyer au modèle : les 4 sections utiles d'une circulaire complète,
    ou le texte entier d'un supplément court (qui n'a pas ces sections)."""
    with tempfile.NamedTemporaryFile(suffix=".htm", delete=False) as tmp:
        tmp.write(html)
    try:
        blocks = sections_lib.load_file(tmp.name)
    finally:
        os.unlink(tmp.name)
    full = "\n".join(sections_lib.render_slice(blocks, 0, len(blocks)))
    if len(full) < SHORT_DOCUMENT:
        return full, "texte entier (document court)", []
    profile = sections_lib.PROFILES["offering"]
    lines, warnings = sections_lib.extract(blocks, config=profile["sections"], cover_page=profile["cover_page"])
    text = "\n".join(lines)
    if not text.strip():
        return full, "texte entier (sections introuvables)", warnings
    return text, "sections offering", warnings


def extract_circular(cik, circular, issuer, client, force):
    """L'extraction d'UNE circulaire, relue depuis S3 si elle a déjà été faite."""
    accession = circular["accession_number"]
    folder = f"{EXTRACTIONS_PREFIX}/{cik}/{accession}"
    cached = None if force else s3_read(f"{folder}/offering_result.json")
    if cached:
        text = (s3_read(f"{folder}/offering_sections.txt") or b"").decode("utf-8")
        return offering_lib.OfferingResult.model_validate_json(cached), "déjà traitée", text

    html = s3_read(f"{RAW_PREFIX}/{cik}/{accession}/{Path(circular['primary_document']).name}")
    if html is None:
        return None, "absente de S3 : lancez sec_documents_downloader.py", ""
    text, mode, section_warnings = document_text(html)
    s3_write(f"{folder}/offering_sections.txt", text)

    result = offering_lib.process_offering(text, client=client, issuer_name=issuer)
    result.warnings[:0] = [f"découpage ({mode}) : {w}" for w in section_warnings]
    s3_write(f"{folder}/offering_result.json", result.model_dump_json(indent=2), "application/json")
    raw_path = Path("last_raw_response.json")
    if raw_path.exists():
        s3_write(f"{folder}/offering_raw_response.json", raw_path.read_text(encoding="utf-8"), "application/json")
    return result, "ok", text


def change_kind(field, old, new, later_text):
    """Un changement entre deux circulaires est-il réel, ou une différence de lecture du modèle ?
      - réel : le document récent écrit « from <ancien> to <nouveau> », ou n'écrit plus l'ancienne valeur ;
      - variation de lecture : l'ancienne valeur est TOUJOURS écrite dans le document récent, sans
        « from … to » : les deux documents disent la même chose, c'est la lecture qui a différé."""
    written = offering_lib.explicit_changes(later_text).get(field)
    if written and written[0] is not None and abs(written[0] - float(new)) < 0.01:
        return "réel (écrit « from … to »)"
    if isinstance(old, (int, float)) and not isinstance(old, bool):
        still_there = offering_lib.number_matches(float(old), later_text)
    else:
        still_there = offering_lib.normalize(str(old)) in offering_lib.normalize(later_text)
    return "VARIATION DE LECTURE, à vérifier" if still_there else "réel (ancienne valeur absente)"


def process_company(cik, company_filings, client, force):
    """Toutes les circulaires d'une société, fusionnées : le document le plus récent l'emporte."""
    issuer = company_filings[0]["company_name"]
    dates, circulars = edgar_summary(company_filings)
    row = {"cik": cik, "issuer_name_edgar": issuer, **dates}
    if not circulars:
        return {**row, "status": "aucune circulaire 253G2 / 253G3"}
    row.update({"offering_accession": circulars[0]["accession_number"],
                "offering_filing_date": circulars[0]["filing_date"]})

    final, provenance, changes, problems = {}, {}, [], []
    verified = rejected = 0
    terms = circulars[0]
    for circular in circulars:
        label = f"{circular['form']} du {circular['filing_date']}"
        result, status, text = extract_circular(cik, circular, issuer, client, force)
        if result is None:
            problems.append(f"{label} : {status}")
            continue
        verified, rejected = verified + len(result.audit), rejected + len(result.rejected)
        problems += [f"{label} : {w}" for w in result.warnings if not w.startswith("essais du modèle")]
        evidence = {a["field"]: a.get("source_text") or a.get("source") for a in result.audit}

        for field, value in result.record.model_dump().items():
            if value is None or field == "issuer_name":
                continue                         # un document muet sur un champ ne l'efface pas
            if field in final and final[field] != value:
                kind = change_kind(field, final[field], value, text)
                changes.append(f"{field} : {final[field]} ({provenance[field]['document']}) -> {value} "
                               f"({label}) [{kind}]")
                if kind.startswith("réel"):
                    terms = circular
            final[field] = value
            provenance[field] = {"value": value, "document": label, "accession": circular["accession_number"],
                                 "source_text": evidence.get(field)}

    if not final:
        return {**row, "status": "aucune circulaire exploitable", "warnings": " | ".join(problems)}

    # La trace complète : pour chaque valeur, le document et la phrase dont elle vient
    s3_write(f"{EXTRACTIONS_PREFIX}/{cik}/offering_merged.json",
             json.dumps({"issuer_name": issuer, "fields": provenance, "changes": changes, "warnings": problems},
                        ensure_ascii=False, indent=2, default=str), "application/json")

    missing = [f for f in offering_lib.OfferingRecord.model_fields if f != "issuer_name" and f not in final]
    return {**row, **final, "status": "ok", "verified": verified, "rejected": rejected,
            "missing": len(missing), "terms_accession": terms["accession_number"],
            "terms_filing_date": terms["filing_date"], "changes": " | ".join(changes),
            "warnings": " | ".join(problems)}


# -------------------------------------------------------------------------
# Programme principal
# -------------------------------------------------------------------------
def save_table(rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=TABLE_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(sorted(rows, key=lambda r: r["cik"]))
    LOCAL_TABLE.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_TABLE.write_text(buffer.getvalue(), encoding="utf-8")
    s3_write(S3_TABLE_KEY, buffer.getvalue(), "text/csv")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--filings-file", help="filings.csv local, à la place de celui de S3.")
    parser.add_argument("--limit", type=int, help="Ne traiter que les N premières sociétés (pour tester).")
    parser.add_argument("--force", action="store_true", help="Retraiter aussi les sociétés déjà traitées.")
    args = parser.parse_args()

    if args.filings_file:
        content = Path(args.filings_file).read_text(encoding="utf-8")
    else:
        raw = s3_read(S3_FILINGS_KEY)
        if raw is None:
            raise RuntimeError("tables/filings.csv introuvable dans S3 : lancez sec_filings_index.py.")
        content = raw.decode("utf-8")
    filings = list(csv.DictReader(io.StringIO(content)))

    companies = {}
    for f in filings:
        companies.setdefault(f["cik"], []).append(f)
    ciks = sorted(companies)[: args.limit] if args.limit else sorted(companies)

    client = boto3.client("bedrock-runtime", region_name=offering_lib.REGION)
    rows = []
    # try / finally : même si vous arrêtez le script (Ctrl + C), la table est enregistrée
    try:
        for n, cik in enumerate(ciks, 1):
            name = companies[cik][0]["company_name"]
            try:
                row = process_company(cik, companies[cik], client, args.force)
            except Exception as e:
                row = {"cik": cik, "issuer_name_edgar": name, "status": f"erreur : {e}"}
            rows.append(row)
            detail = (f"{row.get('n_circulars', 0)} circulaire(s), {row.get('verified', 0)} vérifiée(s), "
                      f"{row.get('rejected', 0)} supprimée(s), {len(row['changes'].split(' | ')) if row.get('changes') else 0} changement(s)"
                      if "verified" in row else "")
            logging.info(f"[{n}/{len(ciks)}] {name} : {row['status']} {detail}")
    finally:
        if rows:
            save_table(rows)
            ok = sum(1 for r in rows if r["status"] == "ok")
            logging.info(f"{ok}/{len(rows)} société(s) extraite(s) -> {LOCAL_TABLE} et s3://{BUCKET_NAME}/{S3_TABLE_KEY}")


if __name__ == "__main__":
    main()