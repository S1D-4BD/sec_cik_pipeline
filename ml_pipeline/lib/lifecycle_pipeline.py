#!/usr/bin/env python3
"""
Pipeline « fin de vie » : clôture de l'offre, vente de l'œuvre, distribution et liquidation,
lues dans les rapports 1-U, 1-SA, 1-K (et leurs pièces jointes) de chaque société.

Aucun appel à un LLM : Masterworks décrit ces événements avec des tournures fixes
(« closed the Offering on … », « agreed to sell … (the “Buyer”) », « filed a Certificate of
Cancellation »), et publie depuis 2021 un tableau « Net Annualized Return Calculation ».
Le code les lit directement. Chaque valeur garde la phrase (ou la ligne du tableau) dont elle vient.

    filings.csv + documents.csv  ->  textes dans S3 (raw/)  ->  lecture des tournures et du tableau
                                 ->  tables/lifecycle_table.csv  (une ligne par société)

Une valeur absente des documents reste VIDE : rien n'est déduit ni complété.

Usage (depuis la racine du projet) :
    python ml_pipeline/lib/lifecycle_pipeline.py --limit 10
    python ml_pipeline/lib/lifecycle_pipeline.py --only 009,021          # seulement ces sociétés (le reste de la table est conservé)
    python ml_pipeline/lib/lifecycle_pipeline.py
"""

import argparse
import csv
import io
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "sec-filings20260901174817304000000001")
S3_CLIENT = boto3.client("s3", region_name=AWS_REGION)
S3_FILINGS_KEY = "tables/filings.csv"
S3_MANIFEST_KEY = "tables/documents.csv"
S3_TABLE_KEY = "tables/lifecycle_table.csv"
LOCAL_TABLE = Path(__file__).parent.parent / "tables" / "lifecycle_table.csv"

LIFECYCLE_FORMS = {"1-U", "1-U/A", "1-SA", "1-SA/A", "1-K", "1-K/A"}

FIELDS = ["offering_closing_date", "consignment_date", "sale_agreement_date", "sale_closing_date",
          "cash_received_date", "distribution_reported_date", "dissolution_date", "buyer", "sale_price",
          "net_liquidation_proceeds", "total_class_a_outstanding", "class_b_profit_share",
          "distributable_per_class_a_share", "irr", "llc_liquidated", "artwork_title", "artwork_artist"]
TABLE_COLUMNS = ["cik", "issuer_name_edgar"] + FIELDS + ["status", "n_documents_read", "sources", "warnings"]


# =====================================================================================
# 1. LIRE UN DOCUMENT
# =====================================================================================

QUOTES = {"“": '"', "”": '"', "’": "'", "‘": "'", "\xa0": " "}


def clean(text):
    for a, b in QUOTES.items():
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip()


# Texte INVISIBLE inséré dans certains documents pour brouiller la copie automatique :
# <FONT STYLE="font-size: 2pt; color: white">x</FONT> entre chaque lettre d'un titre.
# Un lecteur voit « Rat & Heart », un programme lirait « xRxaxtx x&x xHxexaxrxtx ».
def remove_hidden_text(soup):
    """Retire du document tout élément invisible : masqué, de taille nulle, ou blanc et minuscule."""
    for tag in soup.find_all(style=True):
        if tag.parent is None:
            continue                         # déjà retiré avec un élément parent
        style = re.sub(r"\s+", "", str(tag.get("style", "")).lower())
        size = re.search(r"font-size:(\d+(?:\.\d+)?)(pt|px)", style)
        tiny = size is not None and float(size.group(1)) <= (3 if size.group(2) == "pt" else 4)
        white = re.search(r"color:(white|#fff\b|#ffffff)", style) is not None
        filler = re.fullmatch(r"[xX\s]+", tag.get_text() or "") is not None     # que des « x » (et des espaces)
        if "display:none" in style or "visibility:hidden" in style or "font-size:0" in style \
                or (tiny and (white or filler)):
            # Le texte invisible porte parfois l'espace entre deux mots (« x x ») : on garde cet espace
            tag.replace_with(" " if re.search(r"\s", tag.get_text()) else "")
    # Les balises de mise en forme (police, gras…) sont fusionnées avec leur texte : « R » « a » « t »
    # redeviennent « Rat », au lieu d'être séparés par un espace à la lecture
    for tag in soup.find_all(["font", "span", "b", "i", "u", "strong", "em"]):
        tag.unwrap()
    soup.smooth()
    return soup


def to_text(html):
    return clean(remove_hidden_text(BeautifulSoup(html, "html.parser")).get_text(" "))


def sentences(text):
    # pas de coupure après « No. », « Inc. »… : « Untitled No. 10 » est un seul titre
    return re.split(r"(?<!\bNo\.)(?<!\bInc\.)(?<!\bCo\.)(?<!\bSt\.)(?<=[.!?])\s+(?=[A-Z])", text)


MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
DATE = rf"({MONTH}\s+\d{{1,2}}\s*,\s*\d{{4}})"
AMOUNT = r"\$\s?(\d[\d,]*(?:\.\d+)?)"


def parse_date(s):
    s = re.sub(r"\s+", " ", s.replace(" ,", ","))
    for fmt in ("%B %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse_number(s):
    try:
        return float(s.replace(",", "").replace("$", "").replace("(", "").replace(")", "").strip())
    except ValueError:
        return None


# =====================================================================================
# 2. LES TOURNURES FIXES DES RAPPORTS (lecture par le code)
#    Chaque règle : (champ, expression régulière, groupe de la valeur, type)
#    La valeur n'est retenue que si la PHRASE qui la contient correspond à la règle.
# =====================================================================================

PHRASE_RULES = [
    ("offering_closing_date", rf"closed the Offering on\s+{DATE}", "date"),
    ("offering_closing_date", rf"final closing (?:of (?:the|this) Offering )?(?:occurred |took place |was held )?on\s+{DATE}", "date"),
    ("consignment_date", rf"[Oo]n\s+{DATE},[^.]{{0,200}}?\bconsign", "date"),
    ("sale_agreement_date", rf"[Oo]n\s+{DATE},[^.]{{0,250}}?\bagreed to sell", "date"),
    ("sale_agreement_date", rf"[Oo]n\s+{DATE},[^.]{{0,250}}?\bentered into an? (?:agreement|purchase agreement) to sell", "date"),
    ("sale_closing_date", rf"[Oo]n\s+{DATE},\s+the\s+(?:parties|Company)[^.]{{0,200}}?\bconsummated", "date"),
    ("cash_received_date", rf"[Oo]n\s+{DATE},\s+title of the Painting passed to (?:the )?Buyer", "date"),
    ("dissolution_date", rf"[Oo]n\s+{DATE},\s+the Company filed a Certificate of Cancellation", "date"),
    ("dissolution_date", rf"was dissolved (?:on|effective(?: as of)?)\s+{DATE}", "date"),
    ("sale_price", rf"sale price for the Painting is\s+{AMOUNT}", "amount"),
    ("sale_price", rf"for\s+{AMOUNT}(?:\s+in cash)?\s*\(the\s+\"Sale Price\"\)", "amount"),
    ("buyer", r"\bto\s+((?:(?!\bto\s)[^()]){2,150}?)\s*\(the\s+\"Buyer\"\)", "buyer"),
    ("distributable_per_class_a_share", rf"distribution in the amount of\s+(?:approximately\s+)?{AMOUNT}[^.]{{0,40}}?per Class A", "amount"),
    ("irr", r"net annualized return of\s+(\d+(?:\.\d+)?)\s?%", "percent"),
    # L'œuvre, nommée dans les annonces de vente : « sold the painting created by Claude Monet, entitled
    # Coup de Vent (the "Painting") » (utile quand la circulaire ne donnait pas le titre)
    ("artwork_title", r"entitled,?\s+\"?(.+?)\"?,?\s*\(the\s+\"(?:Painting|Artwork|Work)\"(?:\s+or\s+the\s+\"(?:Painting|Artwork|Work)\")?\)", "title"),
    ("artwork_artist", r"\b(?:painting|work of art|artwork)\s+(?:created\s+)?by\s+([A-Z][^(),\"]{1,60}?)\s*(?:\([^)]*\))?,\s+entitled", "text"),
]
SCRAMBLED = re.compile(r"(?:\b\w\b\s+x\s+){3,}")   # texte brouillé dans certains 1-U : « x C x o x u … »

# Une phrase hypothétique ou au conditionnel ne décrit pas un événement qui a eu lieu
HYPOTHETICAL = ["if the", "would ", "assum", "for example", "hypothetical", "expects to", "anticipate"]


# Une date précédée de « filed with the SEC on » est la date de dépôt d'un AUTRE rapport, pas un événement
FILING_DATE_PREFIX = re.compile(r"(?:filed|submitted)(?: with the SEC)? on\s*$|SEC on\s*$", re.I)


def is_filing_date(sentence, date_start):
    return bool(FILING_DATE_PREFIX.search(sentence[max(0, date_start - 45):date_start]))


def phrase_values(text):
    """Toutes les valeurs trouvées par les tournures fixes : {champ: [(valeur, phrase)]}."""
    found = {}
    for sentence in sentences(text):
        low = sentence.lower()
        if any(h in low for h in HYPOTHETICAL):
            continue
        for field, pattern, kind in PHRASE_RULES:
            # Chaque « On <date>, » de la phrase est essayé séparément : si le premier est une date
            # de dépôt (« filed with the SEC on October 22, 2020, on October 19, 2020, the Company… »),
            # c'est le suivant qui est retenu.
            if pattern.startswith("[Oo]n"):
                starts = [m.start() for m in re.finditer(r"[Oo]n\s+" + DATE, sentence)]
                matches = [re.match(pattern, sentence[i:]) for i in starts]
                matches = [(i, m) for i, m in zip(starts, matches) if m]
            else:
                matches = [(0, m) for m in re.finditer(pattern, sentence)]
            for offset, m in matches:
                if kind == "date" and is_filing_date(sentence, offset + m.start(1)):
                    continue
                raw = m.group(1).strip()
                if kind == "date":
                    value = parse_date(raw)
                elif kind == "amount":
                    value = parse_number(raw)
                elif kind == "percent":
                    value = round(float(raw) / 100, 6)
                elif kind == "title":
                    value = None if SCRAMBLED.search(raw) else raw.strip(" ,")
                elif kind == "text":
                    value = raw.strip(" ,")
                else:   # buyer : on retire la description qui suit (« , a Delaware limited liability company »)
                    value = re.sub(r",\s+an?\s+[A-Za-z ]*$", "", raw).strip(" ,")
                if value not in (None, ""):
                    found.setdefault(field, []).append((value, sentence.strip()))
    return found


def is_buyer_description(value):
    return str(value).lower().startswith(("a ", "an ", "the ", "third", "unaffiliated", "undisclosed"))


# =====================================================================================
# 3. LE TABLEAU « Net Annualized Return Calculation » (lecture ligne par ligne)
# =====================================================================================

TABLE_ROWS = [   # (début du libellé, champ, type)
    ("gross artwork sale proceeds", "sale_price", "amount"),
    ("net liquidation proceeds", "net_liquidation_proceeds", "amount"),
    ("total class a shares outstanding", "total_class_a_outstanding", "count"),
    ("distributable proceeds per class a share", "distributable_per_class_a_share", "amount"),
    ("class b profit share", "class_b_profit_share", "amount"),
    ("cash receipt date", "cash_received_date", "date"),
    ("final offering close date", "offering_closing_date", "date"),
    ("irr", "irr", "percent"),
]


def table_values(html):
    """Les lignes du tableau de liquidation : {champ: [(valeur, ligne du tableau)]}."""
    soup = remove_hidden_text(BeautifulSoup(html, "html.parser"))
    if "distributable proceeds" not in soup.get_text(" ").lower():
        return {}
    found = {}
    for tr in soup.find_all("tr"):
        cells = [clean(c.get_text(" ")) for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c and c not in ("$", ")", "(+)", "(-)", "=", "(/)", "%", "%*")
                 and not re.fullmatch(r"[*†‡]+", c)]          # appels de note : « 301,138 | * »
        if len(cells) < 2:
            continue
        label = cells[0].lower().strip("()+-=/ ")
        row_text = " | ".join(cells)
        for start, field, kind in TABLE_ROWS:
            if not label.startswith(start) or (start == "irr" and label != "irr"):
                continue
            raw = cells[-1]
            if kind == "date":
                value = parse_date(raw)
            elif kind == "percent":
                n = parse_number(raw.replace("%", "").replace("*", ""))
                value = round(n / 100, 6) if n is not None else None
            else:
                value = parse_number(raw)
                if value is not None and kind == "count":
                    value = int(value)
            if value is not None:
                found.setdefault(field, []).append((value, row_text))
    return found


# =====================================================================================
# 4. S3 et inventaire des documents
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


# =====================================================================================
# 5. UNE SOCIÉTÉ
# =====================================================================================

def process_company(cik, company_filings, files_by_accession):
    issuer = company_filings[0]["company_name"]
    reports = sorted((f for f in company_filings if f["form"] in LIFECYCLE_FORMS),
                     key=lambda f: (f["filing_date"], f["accession_number"]))
    candidates, n_read = {}, 0     # {champ: [(valeur, source, preuve, rang)]}

    for f in reports:
        accession = f["accession_number"]
        label = f"{f['form']} du {f['filing_date']}"
        for name in files_by_accession.get(accession, []):
            if not name.lower().endswith((".htm", ".html")):
                continue
            html = s3_read(f"raw/{cik}/{accession}/{name}")
            if html is None:
                continue
            n_read += 1
            for field, values in table_values(html).items():          # rang 0 : le tableau
                for value, row in values:
                    candidates.setdefault(field, []).append((value, f"{label} · {name} (tableau)", row, 0))
            for field, values in phrase_values(to_text(html)).items():  # rang 1 : le texte
                for value, sentence in values:
                    # Un acheteur décrit sans être nommé (« an undisclosed buyer ») : rang 2, après un vrai nom
                    rank = 2 if field == "buyer" and is_buyer_description(value) else 1
                    candidates.setdefault(field, []).append((value, f"{label} · {name}", sentence, rank))
            # Le 1-U qui annonce que les actionnaires ONT REÇU leur distribution : sa date de dépôt
            if f["form"].startswith("1-U") and re.search(r"have received a distribution", to_text(html), re.I):
                candidates.setdefault("distribution_reported_date", []).append(
                    (f["filing_date"], f"{label} (date de dépôt EDGAR)", "« … have received a distribution … »", 1))

    final, sources, warnings = {}, {}, []
    for field, cands in candidates.items():
        top_rank = min(c[3] for c in cands)
        distinct = {c[0] for c in cands if c[3] == top_rank}
        best = sorted(cands, key=lambda c: c[3])[0]          # le tableau d'abord, puis le premier texte
        if len(distinct) > 1:
            table = {c[0] for c in cands if c[3] == 0}
            if field == "sale_price" and len(table) == 1:
                best = next(c for c in cands if c[3] == 0)
            warnings.append(f"{field} : plusieurs valeurs dans les documents {sorted(map(str, distinct))}, "
                            f"retenue : {best[0]} ({best[1]})")
        final[field] = best[0]
        sources[field] = {"value": best[0], "document": best[1], "evidence": best[2]}

    # « LLC Liquidated? » : oui si la dissolution est écrite dans un document, non sinon
    final["llc_liquidated"] = "yes" if final.get("dissolution_date") else "no"
    sources["llc_liquidated"] = {"value": final["llc_liquidated"],
                                 "document": sources.get("dissolution_date", {}).get("document", "aucune dissolution écrite"),
                                 "evidence": sources.get("dissolution_date", {}).get("evidence")}

    if final.get("sale_price") and not final.get("sale_agreement_date") and not final.get("sale_closing_date"):
        warnings.append("prix de vente trouvé, mais aucune date de vente")
    s3_write(f"extractions/{cik}/lifecycle_merged.json",
             json.dumps({"issuer_name": issuer, "fields": sources, "warnings": warnings},
                        ensure_ascii=False, indent=2, default=str), "application/json")
    return {"cik": cik, "issuer_name_edgar": issuer, **final, "status": "ok", "n_documents_read": n_read,
            "sources": " | ".join(f"{k} <- {v['document']}" for k, v in sources.items() if k != "llc_liquidated"),
            "warnings": " | ".join(warnings)}


# =====================================================================================
# 6. PROGRAMME PRINCIPAL
# =====================================================================================

def select_companies(companies, only=None, limit=None):
    """Les sociétés à traiter, toujours dans le même ordre (par CIK).
    only : « 009,021,Collection 001,0001817381 » : numéros de LLC, noms ou CIK, séparés par des virgules."""
    ciks = sorted(companies)
    if only:
        wanted = [w.strip() for w in only.split(",") if w.strip()]
        def matches(cik):
            name = companies[cik][0]["company_name"]
            for w in wanted:
                if w.isdigit() and len(w) > 4 and cik == w.zfill(10):
                    return True                                         # un CIK
                if re.search(rf"(?<![\d]){re.escape(w)}(?![\d])", name, re.I):
                    return True                                         # « 009 » ou « Collection 001 »
            return False
        ciks = [c for c in ciks if matches(c)]
        found = {w for w in wanted if any(re.search(rf"(?<![\d]){re.escape(w)}(?![\d])", companies[c][0]["company_name"], re.I)
                                          or c == w.zfill(10) for c in ciks)}
        for w in wanted:
            if w not in found:
                logging.warning(f"--only : aucune société ne correspond à « {w} »")
    return ciks[:limit] if limit else ciks


def merge_with_existing(rows, local_path, s3_key):
    """Mise à jour PARTIELLE : les lignes traitées remplacent les anciennes, toutes les autres sont gardées."""
    existing = []
    if local_path.exists():
        existing = list(csv.DictReader(open(local_path, encoding="utf-8")))
    else:
        raw = s3_read(s3_key)
        if raw:
            existing = list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
    done = {r["cik"] for r in rows}
    return [r for r in existing if r.get("cik") not in done] + rows


def save_table(rows):
    rows = merge_with_existing(rows, LOCAL_TABLE, S3_TABLE_KEY)
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
    parser.add_argument("--documents-file", help="documents.csv local, à la place de celui de S3.")
    parser.add_argument("--limit", type=int, help="Ne traiter que les N premières sociétés.")
    parser.add_argument("--only", help="Seulement ces sociétés : numéros de LLC, noms ou CIK séparés par des virgules "
                                       "(ex. --only 009,021,\"Collection 001\").")
    args = parser.parse_args()

    filings = read_csv_source(args.filings_file, S3_FILINGS_KEY, "sec_filings_index.py")
    manifest = read_csv_source(args.documents_file, S3_MANIFEST_KEY, "sec_documents_downloader.py")
    files_by_accession = {}
    for m in manifest:
        files_by_accession.setdefault(m["accession_number"], []).append(m["file_name"])

    companies = {}
    for f in filings:
        companies.setdefault(f["cik"], []).append(f)
    ciks = select_companies(companies, args.only, args.limit)
    logging.info(f"{len(ciks)} société(s) à traiter")

    rows = []
    try:
        for n, cik in enumerate(ciks, 1):
            name = companies[cik][0]["company_name"]
            try:
                row = process_company(cik, companies[cik], files_by_accession)
            except Exception as e:
                row = {"cik": cik, "issuer_name_edgar": name, "status": f"erreur : {e}"}
            rows.append(row)
            filled = sum(1 for f in FIELDS if row.get(f) not in (None, ""))
            logging.info(f"[{n}/{len(ciks)}] {name} : {row['status']}, {row.get('n_documents_read', 0)} document(s) lu(s), "
                         f"{filled} champ(s) rempli(s), liquidée : {row.get('llc_liquidated', '?')}")
    finally:
        if rows:
            save_table(rows)
            logging.info(f"{len(rows)} société(s) -> {LOCAL_TABLE} et s3://{BUCKET_NAME}/{S3_TABLE_KEY}")


if __name__ == "__main__":
    main()