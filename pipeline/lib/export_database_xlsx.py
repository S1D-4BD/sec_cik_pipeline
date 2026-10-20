#!/usr/bin/env python3
"""
Exporte la table produite par offering_pipeline.py au format EXACT de la feuille « Database »
du classeur Dashboard_Masterworks_SEC_model : mêmes 56 en-têtes, mêmes colonnes (A à BD),
mêmes formules Excel pour les colonnes calculées.

    data/tables/offering_table.csv  (offering_pipeline.py : l'offre)
  + data/tables/lifecycle_table.csv (lifecycle_pipeline.py : clôture, vente, liquidation)
    ->  data/tables/masterworks_database.xlsx  (feuille « Database » + « Notes »)

Couleurs (convention des modèles financiers) :
    bleu  = valeur lue dans les documents SEC par le pipeline (ou dans EDGAR)
    noir  = formule Excel, identique à celle du classeur d'origine

Usage (depuis la racine du projet) :
    python pipeline/lib/export_database_xlsx.py
    python pipeline/lib/export_database_xlsx.py --input data/tables/offering_table.csv

Bibliothèques : pip install openpyxl boto3
"""

import argparse
import csv
import io
import os
import re
from datetime import date, datetime
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "sec-filings20260901174817304000000001")
S3_INPUT_KEY = "tables/offering_table.csv"
S3_LIFECYCLE_KEY = "tables/lifecycle_table.csv"
S3_OUTPUT_KEY = "tables/masterworks_database.xlsx"

TABLES_DIR = Path(__file__).parent.parent.parent / "data" / "tables"
LOCAL_INPUT = TABLES_DIR / "offering_table.csv"
LOCAL_LIFECYCLE = TABLES_DIR / "lifecycle_table.csv"


SALE_DATE_FIELD = "sale_closing_date"
LOCAL_OUTPUT = TABLES_DIR / "masterworks_database.xlsx"

D, USD, USD2, INT, PCT, TXT = "yyyy-mm-dd", "$#,##0", "$#,##0.00", "#,##0", "0.0%", "@"

COLUMNS = [
    ('Issuer LLC / "The Company"', {"source": "issuer_name_edgar"}),
    ("Artist", {"source": "artist_name"}),
    ("Title", {"source": "title"}),
    ("Dimensions", {"source": "dimensions_text"}),
    ("Year of creation of artwork ", {"source": "execution_year", "format": "0"}),
    ("Art Period", {"formula": '=IF(E{r}=0,"",ROUNDDOWN(E{r},-1)&"s")', "needs": ['execution_year']}),
    ("Date of Incorporation", {"source": "incorporation_date", "format": D}),
    ("Date of artwork purchase", {"source": "purchase_date", "format": D}),
    ("Date of Initial 1-A Form publication", {"source": "date_initial_1a", "format": D}),
    ("Date of SEC Qualification", {"source": "date_sec_qualification", "format": D}),
    ("Initial Offering Launch Date", {"source": "date_offering_launch", "format": D}),
    ("Initial Offering Closing Date", {"source": "offering_closing_date", "format": D}),
    ("Date of Consignment", {"source": "consignment_date", "format": D}),
    ("Date of sale agreement", {"source": SALE_DATE_FIELD, "format": D}),
    ("Date of cash received from artwork sale", {"source": "cash_received_date", "format": D}),
    ("Estimated Date of Proceeds Distribution to Investors", {"source": "distribution_reported_date", "format": D}),
    ("Date of 1-Z Form publication", {"source": "date_1z", "format": D}),
    ("Date of LLC Dissolution", {"source": "dissolution_date", "format": D}),
    ("Year of incorporation", {"formula": '=IF(G{r}=0,"",YEAR(G{r}))', "format": "0", "needs": ['incorporation_date']}),
    ("# of days between LLC Incorporation and Initial Offering launch", {"formula": '=IF(K{r}=0,"",K{r}-G{r})', "format": INT, "needs": ['incorporation_date', 'date_offering_launch']}),
    ("Duration (in days) of Initial Offering", {"formula": '=IF(L{r}-K{r}>0,L{r}-K{r},"")', "format": INT, "needs": ['offering_closing_date', 'date_offering_launch']}),
    ("# of months between LLC Offering closing and Artwork Sale", {"formula": '=IF(N{r}=0,"",(N{r}-L{r})/30)', "format": "0.0", "needs": ['offering_closing_date', SALE_DATE_FIELD]}),
    ("Year of sale", {"formula": '=IF(N{r}=0,"",YEAR(N{r}))', "format": "0", "needs": [SALE_DATE_FIELD]}),
    ("Purchase price", {"source": "purchase_price_usd", "format": USD}),
    ("unique Purchase price", {"formula": "=X{r}+ROW()/100000", "format": USD2, "needs": ['purchase_price_usd']}),
    ("Asset Class", {"formula": '=IF(AND(AB{r}<=2000000),"Class 1",IF(AND(AB{r}>2000000,AB{r}<=4000000),"Class 2",IF(AB{r}>4000000,"Class 3")))', "needs": ['purchase_price_usd', 'true_up']}),
    ("True-up", {"source": "true_up", "format": USD}),
    ("Offering Issuing Price", {"formula": "=X{r}+AA{r}", "format": USD, "needs": ['purchase_price_usd', 'true_up']}),
    ("# of Class A ordinary Shares offered", {"source": "class_a_shares_offered", "format": INT}),
    ("Price per Class A share offered", {"source": "price_per_class_a_share", "format": USD2}),
    ("Aggregate offering (class A shares)", {"source": "aggregate_offering", "format": USD}),
    ("# of class B ordinary shares held by Masterworks ", {"source": "class_b_shares_masterworks", "format": INT}),
    ("Aggregate cash cost to Masterworks for the Class B", {"source": "class_b_aggregate_cash_cost", "format": USD2}),
    ("Class B shares nominal value", {"formula": '=IF(AF{r}=0,"",AG{r}/AF{r})', "format": "0.0000", "needs": ['class_b_shares_masterworks', 'class_b_aggregate_cash_cost']}),
    ("LLC Liquidated?", {"source": "llc_liquidated", "format": TXT}),
    ("Liquidated ?", {"formula": '=IF(AI{r}="yes",1,0)', "format": "0"}),
    ("Buyer", {"source": "buyer", "format": TXT}),
    ("Artwork Sale Price", {"source": "sale_price", "format": USD}),
    ("Gross profit based on Asset Issuing price\n$", {"formula": '=IF(AI{r}="no","",IFERROR(AL{r}-AB{r},"n.a"))', "sale": True, "needs": ['sale_price', 'purchase_price_usd', 'true_up'], "format": USD}),
    ("Gross profit based on Asset Issuing price\n%", {"formula": '=IF(AI{r}="no","",AM{r}/AL{r})', "sale": True, "needs": ['sale_price', 'purchase_price_usd', 'true_up'], "format": PCT}),
    ("Net Liquidation Proceeds", {"source": "net_liquidation_proceeds", "format": USD}),
    ("Total Class A Shares Outstanding", {"source": "total_class_a_outstanding", "format": INT}),
    ("Class A Preferred shares issued to MW", {"formula": '=IF(AI{r}="no","",AP{r}-AC{r})', "sale": True, "needs": ['total_class_a_outstanding'], "format": INT}),
    ("Cash from Class B Preferred Shares held by MW", {"source": "class_b_profit_share", "format": USD}),
    ("Cash from Class A Preferred Shares held by MW", {"formula": '=IF(AI{r}="no","",AQ{r}*AT{r})', "sale": True, "needs": ['total_class_a_outstanding', 'distributable_per_class_a_share'], "format": USD}),
    ("Distributable Proceeds per Class A Share", {"source": "distributable_per_class_a_share", "format": USD2}),
    ("Profit per class A share from issuing price for LLC investors\n(%)", {"formula": '=IF(AI{r}="no","",(AT{r}-AD{r})/AD{r})', "sale": True, "needs": ['distributable_per_class_a_share'], "format": PCT}),
    ("Profit per class A share from issuing price for LLC investors\n($)", {"formula": '=IF(AI{r}="no","",AT{r}-AD{r})', "sale": True, "needs": ['distributable_per_class_a_share'], "format": USD2}),
    ("IRR", {"source": "irr", "format": PCT}),
    ("Projected profit per Class A share that LLC investors would realize without MW dilution", {"formula": '=IF(AI{r}="no","",(AO{r}-AB{r})/AC{r})', "sale": True, "needs": ['net_liquidation_proceeds'], "format": USD2}),
    ("Difference in profit per Class A share between diluted and non-diluted scenarios\n($)", {"formula": '=IF(AI{r}="no","",AX{r}-AV{r})', "sale": True, "needs": ['net_liquidation_proceeds', 'distributable_per_class_a_share'], "format": USD2}),
    ("Difference in profit per Class A share between diluted and non-diluted scenarios\n(%)", {"formula": '=IF(AI{r}="no","",(AX{r}-AV{r})/AV{r})', "sale": True, "needs": ['net_liquidation_proceeds', 'distributable_per_class_a_share'], "format": PCT}),
    ("Total cash back for MW following the liquidation\n(true-up + class A shares + Class B shares)", {"formula": '=IF(AI{r}="no","",AS{r}+AR{r}+AA{r})', "sale": True, "needs": ['total_class_a_outstanding', 'distributable_per_class_a_share', 'class_b_profit_share'], "format": USD}),
    ("Total cash back for MW on Offering Issuing Price ", {"formula": '=IF(AI{r}="no","",BA{r}/AB{r})', "sale": True, "needs": ['total_class_a_outstanding', 'distributable_per_class_a_share', 'class_b_profit_share'], "format": PCT}),
    (None, {}),
    ("Comments on Sale Transaction", {}),
]
assert len(COLUMNS) == 56, "la feuille Database a 56 colonnes"

# Remarques sur les formules d'origine, affichées en commentaire sur l'en-tête concerné
HEADER_NOTES = {
    "Date of sale agreement": "Date de CONCLUSION de la vente (« consummated »), comme dans l'ancien Excel. "
        "La date de l'accord (« agreed to sell ») est dans lifecycle_table.csv, colonne sale_agreement_date.",
    "Estimated Date of Proceeds Distribution to Investors": "Date de dépôt du 1-U qui annonce que les actionnaires "
        "« have received a distribution ».",
    "Cash from Class B Preferred Shares held by MW": "Ligne « Class B Profit Share » du tableau de liquidation.",
    "LLC Liquidated?": "yes si un document écrit la dissolution (Certificate of Cancellation), no sinon.",
    "IRR": "Lu dans les documents (« net annualized return » ou tableau de liquidation). Vide s'il n'y est pas écrit.",
    "Gross profit based on Asset Issuing price\n%": "Formule d'origine conservée : elle divise par le PRIX DE VENTE (AL), "
        "alors que le titre parle du prix d'émission (AB). À trancher : AM/AB ?",
    "unique Purchase price": "Astuce d'origine (+ROW()/100000) pour rendre chaque prix unique dans les tableaux croisés. "
        "Ce n'est pas une donnée ; inutile dans Power BI.",
    "Dimensions": "Lues dans la circulaire, en centimètres (plus précis que les pouces arrondis de l'ancien tableau).",
    "Purchase price": "Prix payé, en dollars américains, tel qu'écrit dans la dernière circulaire (253G2/253G3).",
    "Date of Initial 1-A Form publication": "Date du PREMIER dépôt 1-A dans EDGAR.",
    "Total Class A Shares Outstanding": "Saisi (pas de formule) : la formule d'origine =AQ+AC créait une référence "
        "circulaire avec la colonne AQ.",
}

INPUT_FONT = Font(name="Arial", size=10, color="0000FF")
FORMULA_FONT = Font(name="Arial", size=10, color="000000")
HEADER_FONT = Font(name="Arial", size=10, bold=True)
HEADER_FILL = PatternFill("solid", start_color="D9E1F2")


def parse(value, fmt):
    """Convertit le texte du CSV en vraie valeur Excel : date, nombre ou texte."""
    if value in (None, ""):
        return None
    if fmt == D:
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return value
    if fmt and fmt != TXT:
        try:
            number = float(value)
            return int(number) if number.is_integer() else number
        except ValueError:
            return value
    return value


def read_lifecycle():
    """Les valeurs de fin de vie, par CIK (vide si lifecycle_pipeline.py n'a pas encore tourné)."""
    if LOCAL_LIFECYCLE.exists():
        rows = list(csv.DictReader(open(LOCAL_LIFECYCLE, encoding="utf-8")))
    else:
        try:
            body = boto3.client("s3", region_name=AWS_REGION).get_object(Bucket=BUCKET_NAME, Key=S3_LIFECYCLE_KEY)["Body"]
            rows = list(csv.DictReader(io.StringIO(body.read().decode("utf-8"))))
        except Exception:
            rows = []
    keep = lambda r: {k: v for k, v in r.items() if k not in ("issuer_name_edgar", "status", "warnings", "sources",
                                                                "n_documents_read")}
    return {r["cik"]: keep(r) for r in rows}


def read_input(path):
    if path:
        return list(csv.DictReader(open(path, encoding="utf-8")))
    if LOCAL_INPUT.exists():
        return list(csv.DictReader(open(LOCAL_INPUT, encoding="utf-8")))
    body = boto3.client("s3", region_name=AWS_REGION).get_object(Bucket=BUCKET_NAME, Key=S3_INPUT_KEY)["Body"]
    return list(csv.DictReader(io.StringIO(body.read().decode("utf-8"))))


def build_workbook(rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Database"

    for c, (header, spec) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=c, value=header)
        cell.font, cell.fill = HEADER_FONT, HEADER_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        if header in HEADER_NOTES:
            cell.comment = Comment(HEADER_NOTES[header], "pipeline")
        ws.column_dimensions[get_column_letter(c)].width = 14 if header and len(header) < 30 else 20
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["C"].width = 34
    ws.row_dimensions[1].height = 75

    for r, data in enumerate(rows, start=2):
        liquidation_known = bool(data.get("llc_liquidated"))      # futur champ de l'extraction des ventes
        for c, (header, spec) in enumerate(COLUMNS, start=1):
            cell = ws.cell(row=r, column=c)
            if "source" in spec:
                cell.value = parse(data.get(spec["source"]), spec.get("format"))
                cell.font = INPUT_FONT
            elif "formula" in spec and (not spec.get("sale") or liquidation_known) and all(
                    data.get(f) not in (None, "") for f in spec.get("needs", [])):
                # Une formule n'est écrite que si TOUTES ses données existent : sinon Excel lirait
                # une case vide comme un zéro, et afficherait un résultat faux
                # (ex. -51 950 actions, ou une offre de 44 043 jours).
                cell.value = spec["formula"].format(r=r)
                cell.font = FORMULA_FONT
            if spec.get("format"):
                cell.number_format = spec["format"]
            cell.alignment = Alignment(vertical="top")

    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(2, len(rows) + 1)}"

    notes = wb.create_sheet("Notes")
    lines = [
        ("Généré le", date.today().isoformat()),
        ("Source", "Documents SEC (EDGAR) : circulaires 253G2 / 253G3 et index des dépôts, via offering_pipeline.py"),
        ("Valeurs en bleu", "Lues dans les documents par le pipeline, chacune vérifiée mot pour mot (preuves dans S3 : "
                            "extractions/<CIK>/offering_merged.json)"),
        ("Valeurs en noir", "Formules Excel identiques à celles du classeur Dashboard_Masterworks_SEC_model"),
        ("Fin de vie", "Clôture, consignation, vente, distribution, dissolution : lues dans les 1-U, 1-SA, 1-K et le "
                       "tableau « Net Annualized Return Calculation » (lifecycle_pipeline.py, sans LLM). "
                       "Preuves : extractions/<CIK>/lifecycle_merged.json"),
        ("Cases vides", "Valeur absente des documents SEC : jamais déduite. Ex. : les sociétés vendues avant 2021 "
                        "n'ont pas de tableau de liquidation (IRR, actions en circulation...)."),
        ("Sociétés", f"{len(rows)}"),
    ]
    for i, (k, v) in enumerate(lines, start=1):
        notes.cell(row=i, column=1, value=k).font = HEADER_FONT
        notes.cell(row=i, column=2, value=v).font = Font(name="Arial", size=10)
    notes.column_dimensions["A"].width = 18
    notes.column_dimensions["B"].width = 120

    wb.calculation.fullCalcOnLoad = True        # Excel recalcule toutes les formules à l'ouverture
    return wb


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", help="offering_table.csv à exporter (par défaut : data/tables/, sinon S3).")
    args = parser.parse_args()

    lifecycle = read_lifecycle()
    rows = [{**r, **lifecycle.get(r["cik"], {})} for r in read_input(args.input)]
    # Titre et artiste : ceux des circulaires d'abord ; sinon ceux des annonces de vente
    for r in rows:
        title = r.get("artwork_title") or ""
        year = re.fullmatch(r"\s*(.+?)\s*\((1[5-9]\d\d|20[0-2]\d)\)\s*", title)
        if not r.get("title") and title:
            r["title"] = year.group(1) if year else title
        if not r.get("execution_year") and year:
            r["execution_year"] = year.group(2)
        if not r.get("artist_name") and r.get("artwork_artist"):
            r["artist_name"] = r["artwork_artist"]
    rows = sorted(rows, key=lambda r: r.get("issuer_name_edgar", ""))
    print(f"fin de vie disponible pour {sum(1 for r in rows if r['cik'] in lifecycle)}/{len(rows)} société(s)")
    wb = build_workbook(rows)
    LOCAL_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    wb.save(LOCAL_OUTPUT)
    try:
        boto3.client("s3", region_name=AWS_REGION).upload_file(str(LOCAL_OUTPUT), BUCKET_NAME, S3_OUTPUT_KEY)
        where = f"{LOCAL_OUTPUT} et s3://{BUCKET_NAME}/{S3_OUTPUT_KEY}"
    except Exception as e:                       # pas d'accès S3 : le fichier local suffit
        where = f"{LOCAL_OUTPUT} (S3 non mis à jour : {e})"
    print(f"{len(rows)} société(s) -> {where}")


if __name__ == "__main__":
    main()