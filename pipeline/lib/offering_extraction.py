"""
Extraction STRICTE des colonnes « offre » du tableau Masterworks, depuis le texte d'une circulaire
d'offre (253G2), découpé au préalable par extract_sections.py --profile offering.

Même principe que bedrock_extract_stable.py : aucune valeur n'est acceptée sur parole.
Chaque valeur doit venir avec sa preuve (les mots exacts du document), et le CODE vérifie :
    1. la preuve existe ;
    2. la preuve se trouve réellement dans le document ;
    3. la valeur se lit dans la preuve ;
    4. la preuve contient le mot-clé attendu pour ce champ (« true-up » pour le true-up,
       « Class B » pour les actions de classe B...). C'est ce qui empêche de ranger un nombre
       juste dans le mauvais champ.
Une valeur qui échoue à un seul contrôle est SUPPRIMÉE, et la raison est enregistrée.

Usage :
    python pipeline/lib/offering_extraction.py offering.txt --issuer "Masterworks 001, LLC"
    -> offering_extractions.json (valeurs vérifiées) + offering_audit.json (preuves, suppressions)

    --issuer : le nom officiel de la société chez EDGAR (colonne company_name de filings.csv).
               Il est plus sûr que celui lu par le modèle. Sans --issuer, le modèle le cherche.

Depuis votre code :
    from offering_extraction import process_offering
    result = process_offering(texte, issuer_name="Masterworks 001, LLC")   -> OfferingResult

Aucun fichier local requis. Bibliothèques : pip install boto3 pydantic
"""

import argparse
import json
import re
import sys
import unicodedata
from typing import Generic, List, Optional, TypeVar

import boto3
from pydantic import BaseModel, Field


# =====================================================================================
# RÉGLAGES
# =====================================================================================

MODEL_ID = "us.amazon.nova-lite-v1:0"
REGION = "us-east-1"


# =====================================================================================
# 1. CE QUE LE MODÈLE DOIT REMPLIR : chaque valeur avec sa preuve
# =====================================================================================

T = TypeVar("T")


class Evidenced(BaseModel, Generic[T]):
    value: Optional[T] = None
    source_text: Optional[str] = None


def ev(description):
    return Field(None, description=description)


class OfferingExtraction(BaseModel):
    issuer_name: Optional[Evidenced[str]] = ev("Legal name of the issuing company, e.g. 'Masterworks 001, LLC'.")
    artist_name: Optional[Evidenced[str]] = ev("Artist of the Painting.")
    title: Optional[Evidenced[str]] = ev("Title of the Painting, without the year.")
    execution_year: Optional[Evidenced[int]] = ev("Year the Painting was created.")
    dimensions_text: Optional[Evidenced[str]] = ev("Dimensions exactly as written, e.g. '46.4 cm by 34.9 cm'.")
    height_cm: Optional[Evidenced[float]] = ev("Height in centimetres, the FIRST number in cm. Null if no cm given.")
    width_cm: Optional[Evidenced[float]] = ev("Width in centimetres, the SECOND number in cm. Null if no cm given.")
    incorporation_date: Optional[Evidenced[str]] = ev("Date the company was formed, YYYY-MM-DD.")
    purchase_date: Optional[Evidenced[str]] = ev("Date the Painting was purchased, YYYY-MM-DD.")
    purchase_price: Optional[Evidenced[float]] = ev("Price paid for the Painting, in the currency written.")
    purchase_currency: Optional[Evidenced[str]] = ev("ISO code of the purchase price currency: USD, GBP, EUR, HKD...")
    purchase_price_usd: Optional[Evidenced[float]] = ev("Price paid in US dollars, only if written in dollars.")
    true_up: Optional[Evidenced[float]] = ev("Amount of the true-up payable to Masterworks, in US dollars.")
    class_a_shares_offered: Optional[Evidenced[int]] = ev("Maximum number of Class A ordinary shares offered.")
    price_per_class_a_share: Optional[Evidenced[float]] = ev("Offering price per Class A ordinary share.")
    aggregate_offering: Optional[Evidenced[float]] = ev("Maximum aggregate offering amount for the Class A shares.")
    class_b_shares_masterworks: Optional[Evidenced[int]] = ev("Number of Class B ordinary shares held by Masterworks.")
    class_b_aggregate_cash_cost: Optional[Evidenced[float]] = ev("Aggregate cash cost to Masterworks for its Class B shares.")


# =====================================================================================
# 2. CE QUE LE SCRIPT RENVOIE : des valeurs vérifiées, sans les preuves
# =====================================================================================

class OfferingRecord(BaseModel):
    issuer_name: Optional[str] = None
    artist_name: Optional[str] = None
    title: Optional[str] = None
    execution_year: Optional[int] = None
    dimensions_text: Optional[str] = None
    height_cm: Optional[float] = None
    width_cm: Optional[float] = None
    incorporation_date: Optional[str] = None
    purchase_date: Optional[str] = None
    purchase_price: Optional[float] = None
    purchase_currency: Optional[str] = None
    purchase_price_usd: Optional[float] = None
    true_up: Optional[float] = None
    class_a_shares_offered: Optional[int] = None
    price_per_class_a_share: Optional[float] = None
    aggregate_offering: Optional[float] = None
    class_b_shares_masterworks: Optional[int] = None
    class_b_aggregate_cash_cost: Optional[float] = None


class OfferingResult(BaseModel):
    record: OfferingRecord
    rejected: List[str] = []    # valeurs SUPPRIMÉES, avec la raison
    warnings: List[str] = []    # valeurs conservées mais à vérifier
    missing: List[str] = []     # champs laissés vides par le modèle
    audit: List[dict] = []      # chaque valeur acceptée, avec le vrai passage du document


# Correspondance avec les colonnes de votre Excel (feuille Database)
EXCEL_COLUMNS = {
    "issuer_name": 'Issuer LLC / "The Company"',
    "artist_name": "Artist",
    "title": "Title",
    "dimensions_text": "Dimensions",
    "execution_year": "Year of creation of artwork",
    "incorporation_date": "Date of Incorporation",
    "purchase_date": "Date of artwork purchase",
    "purchase_price_usd": "Purchase price",
    "true_up": "True-up",
    "class_a_shares_offered": "# of Class A ordinary Shares offered",
    "price_per_class_a_share": "Price per Class A share offered",
    "aggregate_offering": "Aggregate offering (class A shares)",
    "class_b_shares_masterworks": "# of class B ordinary shares held by Masterworks",
    "class_b_aggregate_cash_cost": "Aggregate cash cost to Masterworks for the Class B",
}


# =====================================================================================
# 3. LE PROMPT
# =====================================================================================

PROMPT = """You extract the terms of ONE offering from sections of an SEC offering circular (Form 253G2)
for a company that owns one painting.

ABSOLUTE RULES (any value breaking them is automatically discarded):
- Never invent, guess, estimate, compute or convert any value. Every value must be written in the document.
- A missing value is always better than an invented one: leave it null.
- For EVERY field, give the value AND source_text: the SHORTEST passage of the document that contains the
  value, at most one sentence, copied character for character, including any typo in the document.
- The value must be readable inside its source_text, and the source_text must name what the value is
  (e.g. the true-up passage contains "true-up", the Class B passage contains "Class B").

Field rules:
- issuer_name: the company's legal name, ending with "LLC" or similar.
- incorporation_date: the date the company was "formed" or "organized". purchase_date: the date the Painting
  was purchased or acquired. Dates as YYYY-MM-DD.
- purchase_price: what was paid for the Painting. If two currencies are given ("£4,733,750, or $6,239,556"),
  purchase_price is the first amount in its currency and purchase_price_usd the dollar amount.
  If only dollars are given, purchase_price and purchase_price_usd are the same amount.
- true_up: the true-up amount payable to Masterworks.
- class_a_shares_offered, aggregate_offering: the MAXIMUM offered (e.g. "up to 99,825 ... up to $1,996,500").
- price_per_class_a_share: the offering price per Class A share, cited from a sentence where it is WRITTEN
  as "$20.00 per Class A ordinary share" or "$20.00 per share based on the offering price".
  NEVER compute it from a formula ("determined by dividing ..."): if it is not written, leave it null.
- class_b_shares_masterworks: the Class B ordinary shares owned by Masterworks.
- class_b_aggregate_cash_cost: the "aggregate cash cost to Masterworks" for its Class B shares.
- height_cm and width_cm: from the dimensions in cm, height first.

SUPPLEMENTS: a supplement may change amounts ("a decrease in the purchase price ... from $6,239,556 to
$6,224,266"). Then extract the NEW value, the one after "to". Never the old value, never the difference.
A supplement that does not mention a field: leave that field null.

IGNORE hypothetical figures: conversion examples, "Hypothetical Class A Ordinary Share Value", dilution
tables "based on hypothetical conversion", per-share book values, estimates of other paintings,
investment limits ("up to 5,000 Class A ordinary shares ... $100,000")."""


# =====================================================================================
# 4. LES CONTRÔLES (même moteur que bedrock_extract_stable.py)
# =====================================================================================

QUOTES = {"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"}
FRAGILE_CHARS = set("£₤¤€$¥₹⅛⅜⅝⅞¼½¾⅓⅔")   # recopiés souvent de travers par le modèle
MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]
CURRENCY_SIGNS = {"GBP": ["£", "₤", "gbp", "pound"], "USD": ["$", "usd", "dollar"], "EUR": ["€", "eur", "euro"],
                  "HKD": ["hk$", "hkd"], "CHF": ["chf"], "JPY": ["¥", "jpy", "yen"]}

# Le mot-clé que la preuve DOIT contenir, pour chaque champ (au moins un de la liste)
KEYWORDS = {
    "issuer_name": ["llc", "inc", "ltd", "spc", "company"],
    "execution_year": ["created", "painted", "executed", "completed", "dated", "(",
                       "worked on", "made in", "produced", "between"],   # « worked on … from 2007 to 2008 »
    "height_cm": ["cm"],
    "width_cm": ["cm"],
    "incorporation_date": ["formed", "organized", "incorporated", "organised"],
    "purchase_date": ["purchas", "acquir", "auction", "bought"],
    # Pas « auction » : une ESTIMATION parle aussi d'enchères. Il faut un vrai verbe d'achat.
    "purchase_price": ["purchas", "acquir", "paid", "bought"],
    "purchase_price_usd": ["purchas", "acquir", "paid", "bought"],
    "true_up": ["true-up", "true up", "trueup"],
    "class_a_shares_offered": ["class a"],
    "price_per_class_a_share": ["per class a", "per share", "per ordinary share", "price"],
    "aggregate_offering": ["aggregate", "maximum offering", "offering amount", "consideration"],
    "class_b_shares_masterworks": ["class b"],
    "class_b_aggregate_cash_cost": ["cash cost", "class b"],
}

KINDS = {
    "issuer_name": "text", "artist_name": "text", "title": "text", "dimensions_text": "text",
    "execution_year": "number", "height_cm": "number", "width_cm": "number",
    "incorporation_date": "date", "purchase_date": "date",
    "purchase_price": "number", "purchase_currency": "currency", "purchase_price_usd": "number",
    "true_up": "number", "class_a_shares_offered": "number", "price_per_class_a_share": "number",
    "aggregate_offering": "number", "class_b_shares_masterworks": "number",
    "class_b_aggregate_cash_cost": "number",
}


def base_letter(ch):
    """« ü » -> « u », « é » -> « e » : un seul caractère pour un seul caractère (les positions restent justes)."""
    if ch.isascii():
        return ch
    decomposed = unicodedata.normalize("NFKD", ch)
    return decomposed[0] if decomposed and decomposed[0].isascii() else ch


def normalize(s):
    for a, b in {**QUOTES, "₤": "£"}.items():
        s = s.replace(a, b)
    s = "".join(base_letter(ch) for ch in s)
    return re.sub(r"\s+", " ", s).strip().lower()


def numbers_in(text):
    """Tous les nombres d'un texte : « $1,815,000 » -> 1815000, « $4.9 million » -> 4900000, « 167,6 » -> 167.6."""
    found = []
    text = re.sub(r"(\d[\d,.]*)\s*(-|–|—|to)\s*(\d[\d,.]*)\s*(million|billion|bn|m\b)",
                  r"\1 \4 \2 \3 \4", text.lower())
    for num, mult in re.findall(r"(\d[\d,.]*)\s*(million|billion|bn|m\b|k\b)?", text):
        num = num.rstrip(".,")
        if num.count(".") > 1:
            num = num.replace(".", "")
        if "," in num and "." not in num:
            parts = num.split(",")
            if all(len(p) == 3 for p in parts[1:]):
                num = num.replace(",", "")
            elif len(parts) == 2:
                num = num.replace(",", ".")
        num = num.replace(",", "")
        try:
            value = float(num)
        except ValueError:
            continue
        found.append(value * {"million": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "k": 1e3}.get(mult, 1))
    return found


def number_matches(value, source):
    """Comparaison EXACTE."""
    return any(abs(n - value) < 0.01 for n in numbers_in(source))


def date_matches(value, source):
    parts = value.split("-")
    if not parts[0].isdigit() or not number_matches(int(parts[0]), source):
        return False
    s = normalize(source)
    if len(parts) >= 2:
        if not parts[1].isdigit() or not 1 <= int(parts[1]) <= 12:
            return False
        month = MONTHS[int(parts[1]) - 1]
        if month not in s and month[:3] not in s and not number_matches(int(parts[1]), source):
            return False
    if len(parts) == 3 and (not parts[2].isdigit() or not number_matches(int(parts[2]), source)):
        return False
    return True


def value_matches(kind, value, source):
    s = normalize(source)
    if kind == "text":
        return normalize(str(value)) in s
    if kind == "number":
        return number_matches(float(value), source)
    if kind == "date":
        return date_matches(str(value), source)
    if kind == "currency":
        return any(sign in s for sign in CURRENCY_SIGNS.get(str(value).upper(), [str(value).lower()]))
    return False


def fragments_of(source):
    return [f.strip(" ,;") for f in re.split(r"\.\.\.|…", source) if f.strip(" ,;")]


def normalize_with_positions(text, tolerant):
    chars, positions, previous_space = [], [], True
    for i, ch in enumerate(text):
        ch = QUOTES.get(ch, ch)
        if ch not in FRAGILE_CHARS:
            ch = base_letter(ch)
        if tolerant and ch in FRAGILE_CHARS:
            ch = "\x00"
        if ch.isspace():
            if previous_space:
                continue
            ch, previous_space = " ", True
        else:
            previous_space = False
        chars.append(ch.lower())
        positions.append(i)
    return "".join(chars).rstrip(), positions


def locate(source, document):
    """Le VRAI passage du document qui correspond à la preuve, et sa position : (texte, début, fin),
    ou None. Tolère seulement les caractères fragiles (symboles monétaires, fractions)."""
    for tolerant in (False, True):
        doc, positions = normalize_with_positions(document, tolerant)
        found_parts, start, first, last = [], 0, None, None
        for fragment in fragments_of(source):
            frag, _ = normalize_with_positions(fragment, tolerant)
            found = doc.find(frag, start) if frag else -1
            if found == -1:
                break
            end = found + len(frag) - 1
            found_parts.append(document[positions[found]:positions[end] + 1])
            first = positions[found] if first is None else first
            last = positions[end] + 1
            start = found + len(frag)
        else:
            return " ".join(found_parts), first, last
    return None


# Ce qui ferme une « phrase » : un point, un retour à la ligne, une puce, un point-virgule.
# Les puces comptent : un supplément liste plusieurs changements dans UNE phrase à puces, et chaque
# puce parle d'un seul champ (sinon, « true-up » et « purchase price » seraient dans la même « phrase »).
BOUNDARIES = (". ", "\n", "●", "•", "▪", "; ")


def sentence_around(document, start, end):
    """La phrase (ou la puce, ou la ligne de tableau) du document qui contient le passage."""
    left = max(document.rfind(b, 0, start) for b in BOUNDARIES)
    right_candidates = [i for i in (document.find(b, end) for b in BOUNDARIES) if i != -1]
    right = min(right_candidates) if right_candidates else len(document)
    return document[left + 1:right + 1]


# Une phrase hypothétique ne donne jamais les conditions de l'offre
HYPOTHETICAL = ["if the value", "for example", "hypothetical", "assumed", "assuming", "would receive",
                "would be convertible", "will be convertible"]
NUMERIC_FIELDS = {"price_per_class_a_share", "class_a_shares_offered", "aggregate_offering", "true_up",
                  "class_b_shares_masterworks", "class_b_aggregate_cash_cost", "purchase_price", "purchase_price_usd"}


def check(field, item, document, report):
    """Les quatre contrôles. Renvoie la valeur si elle les passe tous, sinon None."""
    if item is None or item.value in (None, "", []):
        return None
    value, source = item.value, item.source_text
    found = locate(source, document) if source and fragments_of(source) else None
    real, sentence = (found[0], sentence_around(document, found[1], found[2])) if found else (None, "")
    keywords = KEYWORDS.get(field)
    if not source or not fragments_of(source):
        reason = "aucune preuve"
    elif real is None:
        reason = f"preuve absente du document : {source!r}"
    elif not value_matches(KINDS[field], value, real):
        reason = f"la valeur ne se lit pas dans la preuve {real!r}"
    elif keywords and not any(k in normalize(sentence) for k in keywords):
        # le mot-clé est cherché dans la PHRASE entière du document, pas seulement dans la preuve
        reason = f"la phrase ne dit pas de quoi il s'agit (attendu : {' / '.join(keywords)}) : {sentence.strip()!r}"
    elif field in NUMERIC_FIELDS and any(h in normalize(sentence) for h in HYPOTHETICAL):
        reason = f"phrase hypothétique, pas une condition de l'offre : {sentence.strip()!r}"
    else:
        if KINDS[field] == "text":
            value = as_written(value, real)
        report["audit"].append({"field": field, "value": value, "source_text": real})
        return value
    report["rejected"].append(f"{field} = {value!r} supprimé : {reason}")
    return None


def as_written(value, passage):
    """Le texte tel qu'il est ÉCRIT dans le document : « Gunther Forg » lu par le modèle -> « Günther Förg »."""
    norm_passage, positions = normalize_with_positions(passage, tolerant=False)
    norm_value, _ = normalize_with_positions(str(value), tolerant=False)
    i = norm_passage.find(norm_value)
    if i == -1 or not norm_value:
        return value
    return passage[positions[i]:positions[i + len(norm_value) - 1] + 1]


PRICE_PER_SHARE = re.compile(r"\$\s?(\d{1,4}(?:\.\d{2})?)\s+per\s+(?:class a\s+)?(?:ordinary\s+)?share", re.I)
# « $0.01 per share », valeur comptable, et les distributions des ventes passées d'AUTRES sociétés
PRICE_EXCLUDED = ["cash cost", "book value", "less than", "distribut", "proceeds", "previously", "prior ",
                  "sold", "sale of", "net annualized", "return"]


def regex_price_per_share(document):
    """Secours si le modèle n'a pas donné de prix par action valable : on cherche « $20.00 per share »
    dans le texte, en écartant les phrases hypothétiques et les autres montants « par action ».
    Renvoie (prix, phrase) seulement si les phrases qui parlent de l'OFFRE donnent un prix unique."""
    candidates = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", document):
        low = normalize(sentence)
        if any(h in low for h in HYPOTHETICAL) or any(x in low for x in PRICE_EXCLUDED):
            continue
        for m in PRICE_PER_SHARE.finditer(sentence):
            candidates.append((float(m.group(1)), sentence.strip(), "offering" in low))
    preferred = [c for c in candidates if c[2]] or candidates
    values = {c[0] for c in preferred}
    if len(values) == 1:
        return preferred[0][0], preferred[0][1]
    return None, (f"prix par action ambigu dans le texte : {sorted(values)}" if values else None)


CHANGE = re.compile(r"from\s+(\$?\s?\d[\d,]*(?:\.\d+)?)[^●•;]*?\bto\s+(\$?\s?\d[\d,]*(?:\.\d+)?)", re.I)


def explicit_changes(document):
    """Les changements écrits « from X to Y » dans un supplément, puce par puce.
    Renvoie {champ: (nouvelle valeur, ancienne valeur, puce)}. Ce n'est pas un calcul :
    la nouvelle valeur est ÉCRITE dans le document, après « to »."""
    changes = {}
    for bullet in re.split(r"[●•▪;]|\n|(?<=[.!?])\s+", document):
        low = normalize(bullet)
        for m in CHANGE.finditer(bullet):
            old_txt, new_txt = m.group(1), m.group(2)
            dollars = "$" in old_txt or "$" in new_txt
            old, new = numbers_in(old_txt), numbers_in(new_txt)
            if not old or not new:
                continue
            if "true-up" in low or "true up" in low:
                field = "true_up"
            elif "purchase price" in low and dollars:
                field = "purchase_price_usd"
            elif "class a" in low and "share" in low and not dollars:
                field = "class_a_shares_offered"
            elif ("aggregate" in low or "offering size" in low) and dollars:
                field = "aggregate_offering"
            elif "per share" in low or "per class a" in low:
                field = "price_per_class_a_share"
            else:
                continue
            if field in changes and changes[field][0] != new[0]:
                changes[field] = (None, None, "deux changements contradictoires pour ce champ")
                continue
            changes[field] = (new[0], old[0], bullet.strip())
    return changes


# « for £739,461.38, or $944,600 » ou « for £4,733,750 ($6,239,556) » : le prix d'origine, puis en dollars
DUAL_PRICE = re.compile(
    r"(£|₤|€|¥|HK\$|CHF|GBP|EUR|HKD|JPY)\s?(\d[\d,]*(?:\.\d+)?)\s*(?:\(|,?\s*or\s+)\s*(?:US)?\$\s?(\d[\d,]*(?:\.\d+)?)")
SYMBOL_TO_CURRENCY = {"£": "GBP", "₤": "GBP", "€": "EUR", "¥": "JPY", "HK$": "HKD", "CHF": "CHF",
                      "GBP": "GBP", "EUR": "EUR", "HKD": "HKD", "JPY": "JPY"}   # « GBP 900,000, or $1,168,065 »
PURCHASE_WORDS = ["purchas", "acquir", "paid", "bought"]


def dual_currency_purchase(document):
    """Le prix d'achat écrit dans deux devises, dans une phrase d'achat : (prix, devise, prix USD, phrase).
    Ce n'est pas une conversion : les deux montants sont ÉCRITS dans le document."""
    found = set()
    for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])|\n+|[●•▪]", document):
        low = normalize(sentence)
        if not any(w in low for w in PURCHASE_WORDS) or "estimate" in low:
            continue
        for m in DUAL_PRICE.finditer(sentence):
            price, usd = numbers_in(m.group(2)), numbers_in(m.group(3))
            if price and usd:
                found.add((price[0], SYMBOL_TO_CURRENCY[m.group(1)], usd[0], sentence.strip()))
    distinct = {(p, c, u) for p, c, u, _ in found}
    if len(distinct) == 1:
        return next(iter(found))
    return None


YEAR_RX = r"(1[5-9]\d\d|20[0-2]\d)"
# « (the "Painting") », « (the “Artwork”) », « (the “Artwork” or the “Painting”) »
PAINTING_TAG = r"\(the [\"“](?:Painting|Artwork|Work)[\"”](?:\s+or\s+the\s+[\"“](?:Painting|Artwork|Work)[\"”])?\)"
# Une fin de phrase, mais pas après « No. », « Inc. », « Co. »… (« Untitled No. 10 » est un seul titre)
SENTENCE_END = r"(?<!\bNo\.)(?<!\bNos\.)(?<!\bInc\.)(?<!\bCo\.)(?<!\bSt\.)(?<!\bVol\.)(?<=[.!?])\s+|\n+"
SCRAMBLED = re.compile(r"(?:\b\w\b\s+x\s+){3,}")   # texte brouillé : « x C x o x u x p … »


def artwork_facts(text):
    """L'artiste, le titre et l'année, lus par le code dans les tournures fixes des circulaires :
      « ### The Artist Agnes Martin (b. 1912 … »
      « … a single work of art by Agnes Martin (the "Painting") »
      « … created in 1979 by Andy Warhol entitled, 1 Colored Marilyn (Reversal Series) (the "Painting") »
      « … a single untitled work of art created by Keith Haring (the "Painting") »
      « … acquire the Painting (1990), which measures … »
    Renvoie {champ: (valeur, phrase du document)}."""
    facts = {}
    m = re.search(r"###\s*The Artist\s+([A-Z][^()\n]{1,60}?)\s*\((?:b\.|born)", text)
    if m:
        facts["artist_name"] = (m.group(1).strip(" ,"), text[m.start():m.end() + 40].replace("\n", " "))
    for sentence in re.split(SENTENCE_END, text):
        if not re.search(PAINTING_TAG, sentence) or "work of art" not in sentence.lower():
            continue
        a = re.search(r"\bby\s+([A-Z][^()\"“”,]{1,60}?)\s*(?:\([^)]*\))?(?:\s+in\s+" + YEAR_RX + r")?"
                      r"(?:,?\s+entitled|\s*" + PAINTING_TAG + ")", sentence)
        if a and "artist_name" not in facts:
            facts["artist_name"] = (a.group(1).strip(), sentence.strip())
        t = re.search(r"entitled,?\s+[\"“]?(.+?)[\"”]?,?\s*" + PAINTING_TAG, sentence)
        if t and not SCRAMBLED.search(t.group(1)) and "title" not in facts:
            facts["title"] = (t.group(1).strip(" ,"), sentence.strip())
        elif re.search(r"\buntitled work of art", sentence, re.I) and "title" not in facts:
            facts["title"] = ("Untitled", sentence.strip())
        y = re.search(r"created in " + YEAR_RX, sentence) or re.search(r"\bby\s+[^,()]{1,60}?\s+in\s+" + YEAR_RX + r"\s+entitled", sentence)
        if y and "execution_year" not in facts:
            facts["execution_year"] = (int(y.group(1)), sentence.strip())
    # « Masterworks agreed to acquire Maxine (1974), which … » : le titre ET l'année
    acq = re.search(r"agreed to acquire\s+(?!the Painting)([A-Z][^,()]{0,80}?)\s*\(" + YEAR_RX + r"\)", text)
    if acq:
        passage = text[max(0, acq.start() - 20):acq.end() + 30].replace("\n", " ")
        facts.setdefault("title", (acq.group(1).strip(), passage))
        facts.setdefault("execution_year", (int(acq.group(2)), passage))
    y = re.search(r"the Painting \(" + YEAR_RX + r"\)", text)
    if y and "execution_year" not in facts:
        facts["execution_year"] = (int(y.group(1)), text[max(0, y.start() - 60):y.end() + 40].replace("\n", " "))
    return facts


def coherence_warnings(r):
    """Contrôles de bon sens entre les champs : signalés, jamais corrigés automatiquement."""
    w = []
    if r.class_a_shares_offered and r.price_per_class_a_share and r.aggregate_offering:
        expected = r.class_a_shares_offered * r.price_per_class_a_share
        if abs(expected - r.aggregate_offering) > 1:
            w.append(f"actions A × prix ({expected:,.0f}) ≠ offre totale ({r.aggregate_offering:,.0f})")
    price = r.purchase_price_usd if r.purchase_price_usd is not None else (
        r.purchase_price if r.purchase_currency in (None, "USD") else None)
    if price and r.true_up and r.aggregate_offering and abs(price + r.true_up - r.aggregate_offering) > 1:
        w.append(f"prix d'achat + true-up ({price + r.true_up:,.0f}) ≠ offre totale ({r.aggregate_offering:,.0f})")
    if price and r.true_up and not 0.05 <= r.true_up / price <= 0.15:
        w.append(f"true-up de {r.true_up / price:.1%} du prix d'achat (attendu : environ 10 %)")
    if r.incorporation_date and r.purchase_date and r.purchase_date > r.incorporation_date:
        pass  # normal : l'œuvre peut être achetée avant ou après la constitution de la société
    return w


# =====================================================================================
# 5. L'APPEL À BEDROCK
# =====================================================================================

def schema_for_bedrock(model):
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def inline(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return inline(defs[node["$ref"].split("/")[-1]])
            return {k: inline(v) for k, v in node.items() if not (k == "title" and isinstance(v, str))}
        if isinstance(node, list):
            return [inline(x) for x in node]
        return node

    return inline(schema)


def find_extraction(node, depth=0):
    """Retrouve nos champs à n'importe quelle profondeur de la réponse (Nova emballe parfois sa réponse)."""
    if isinstance(node, str):
        match = re.search(r"\{.*\}", node, re.DOTALL)
        try:
            node = json.loads(match.group(0)) if match else None
        except ValueError:
            node = None
    if isinstance(node, dict):
        if set(OfferingExtraction.model_fields) & node.keys():
            return node
        if depth < 5:
            for value in node.values():
                found = find_extraction(value, depth + 1)
                if found is not None:
                    return found
    return None


def call_bedrock(client, text, use_tool):
    schema = schema_for_bedrock(OfferingExtraction)
    kwargs = {
        "modelId": MODEL_ID,
        "messages": [{"role": "user", "content": [{"text": f"<document>\n{text}\n</document>"}]}],
        "inferenceConfig": {"temperature": 0, "maxTokens": 4000},
    }
    if use_tool:
        kwargs["system"] = [{"text": PROMPT}]
        kwargs["toolConfig"] = {"tools": [{"toolSpec": {"name": "save", "inputSchema": {"json": schema}}}],
                                "toolChoice": {"any": {}}}
    else:
        kwargs["system"] = [{"text": PROMPT + "\n\nAnswer ONLY with one JSON object following this JSON schema:\n"
                                     + json.dumps(schema)}]
    blocks = client.converse(**kwargs)["output"]["message"]["content"]
    if use_tool:
        return next((b["toolUse"]["input"] for b in blocks if "toolUse" in b), None)
    return "".join(b.get("text", "") for b in blocks)


def filled_fields(data):
    """Nombre de champs pour lesquels le modèle a donné une valeur."""
    if not isinstance(data, dict):
        return 0
    return sum(1 for f in OfferingExtraction.model_fields
               if isinstance(data.get(f), dict) and data[f].get("value") not in (None, ""))


MIN_FILLED = len(OfferingExtraction.model_fields) // 2   # en dessous : réponse jugée trop pauvre


def sanitize(data):
    """Écarte les valeurs au mauvais format, champ par champ, au lieu de rejeter toute la réponse.
    Cas particulier : une plage d'années pour l'année de création (« from 2007 to 2008 », que le modèle
    renvoie en liste) -> la DERNIÈRE année, comme l'ancien Excel. Renvoie (données nettoyées, remarques)."""
    from pydantic import TypeAdapter, ValidationError
    notes, clean_data = [], {}
    for field, info in OfferingExtraction.model_fields.items():
        if field not in data:
            continue
        value = data[field]
        if isinstance(value, dict) and isinstance(value.get("value"), list) and len(value["value"]) == 1:
            value = {**value, "value": value["value"][0]}
        if field == "execution_year" and isinstance(value, dict) and isinstance(value.get("value"), list):
            years = [y for y in value["value"] if isinstance(y, int)]
            if years:
                notes.append(("warning", f"execution_year : plage d'années {years}, dernière année retenue ({max(years)})"))
                value = {**value, "value": max(years)}
        try:
            TypeAdapter(info.annotation).validate_python(value)
            clean_data[field] = value
        except ValidationError:
            notes.append(("rejected", f"{field} = {value!r} supprimé : format invalide"))
    return clean_data, notes


def ask_model(client, text):
    """1er essai : l'outil. S'il plante, ou s'il remplit moins de la moitié des champs,
    2e essai : JSON en texte. On garde la réponse la PLUS COMPLÈTE des deux.
    Les contrôles qui suivent sont identiques dans les deux cas."""
    attempts, best = [], None
    for use_tool in (True, False):
        try:
            raw = call_bedrock(client, text, use_tool)
        except Exception as e:
            if "ModelErrorException" not in type(e).__name__ and "invalid sequence" not in str(e):
                raise
            raw = f"ERREUR : {e}"
        data = find_extraction(raw)
        n = filled_fields(data)
        attempts.append({"mode": "outil" if use_tool else "JSON texte", "champs_remplis": n, "raw": raw})
        if best is None or n > filled_fields(best):
            best = data
        if n >= MIN_FILLED:
            break
    with open("last_raw_response.json", "w", encoding="utf-8") as f:
        json.dump(attempts, f, ensure_ascii=False, indent=2, default=str)
    clean_data, notes = sanitize(best or {})
    return OfferingExtraction.model_validate(clean_data), attempts, notes


# =====================================================================================
# 6. LES FONCTIONS PRINCIPALES
# =====================================================================================

def process_offering(text, client=None, issuer_name=None) -> OfferingResult:
    """Un texte de circulaire -> un OfferingResult dont CHAQUE valeur a été vérifiée.
    issuer_name : le nom officiel EDGAR de la société (filings.csv). S'il est donné, il remplace
    celui du modèle, qui n'est alors que comparé."""
    client = client or boto3.client("bedrock-runtime", region_name=REGION)
    raw, attempts, notes = ask_model(client, text)
    report = {"rejected": [], "warnings": [], "missing": [], "audit": []}
    for kind, note in notes:
        report["rejected" if kind == "rejected" else "warnings"].append(note)
    summary = ", ".join(f"{a['mode']} : {a['champs_remplis']} champ(s)" for a in attempts)
    if len(attempts) > 1:
        report["warnings"].append(f"essais du modèle : {summary}")

    values = {}
    for field in OfferingExtraction.model_fields:
        item = getattr(raw, field)
        if field == "issuer_name" and issuer_name:
            continue                                     # remplacé par le nom EDGAR, ci-dessous
        values[field] = check(field, item, text, report)
        if item is None or item.value in (None, ""):
            report["missing"].append(field)

    # Nom de la société : celui d'EDGAR, exact, plutôt que celui du modèle
    if issuer_name:
        values["issuer_name"] = issuer_name
        report["audit"].append({"field": "issuer_name", "value": issuer_name, "source": "EDGAR (filings.csv)"})
        model_value = raw.issuer_name.value if raw.issuer_name else None
        if model_value and normalize(str(model_value)) != normalize(issuer_name):
            report["warnings"].append(f"nom lu par le modèle ({model_value!r}) ≠ nom EDGAR ({issuer_name!r})")

    # « Hammer 5 (2010) » : l'année entre parenthèses a sa propre colonne. Elle est retirée du titre,
    # et sert d'année de création si le modèle n'en a pas donné (elle est écrite dans la preuve du titre).
    # Artiste, titre, année : si le modèle ne les a pas trouvés, on les lit dans les tournures fixes
    for field, (value, sentence) in artwork_facts(text).items():
        if values.get(field) in (None, ""):
            values[field] = value
            report["audit"].append({"field": field, "value": value, "source_text": sentence, "source": "tournure fixe"})
        elif normalize(str(values[field])) != normalize(str(value)) and field != "title":
            report["warnings"].append(f"{field} : le modèle a lu {values[field]!r}, la tournure fixe donne {value!r}")

    # « “Rat & Heart” » : les guillemets autour du titre ne font pas partie du titre
    if values.get("title"):
        values["title"] = values["title"].strip().strip('"“”\'‘’').strip() or None

    # « Coup de Vent, or Gust of Wind » : le titre d'origine, sans sa traduction
    if values.get("title") and re.search(r",\s+or\s+", values["title"]):
        values["title"] = re.split(r",\s+or\s+", values["title"])[0].strip()
    # « Brian Donnelly, better known as KAWS » : le nom d'artiste
    known_as = re.search(r"(?:better |also |professionally )?known as\s+(.+)$", values.get("artist_name") or "", re.I)
    if known_as:
        values["artist_name"] = known_as.group(1).strip(" ,.")
    # « Titre (2010) » ou « Titre, 2009 » : l'année a sa propre colonne
    title_match = re.fullmatch(r"\s*(.+?)\s*(?:\((1[5-9]\d\d|20[0-2]\d)\)|,\s*(1[5-9]\d\d|20[0-2]\d))\s*",
                               values.get("title") or "")
    if title_match:
        values["title"] = title_match.group(1)
        year = int(title_match.group(2) or title_match.group(3))
        if values.get("execution_year") is None:
            values["execution_year"] = year
            report["audit"].append({"field": "execution_year", "value": year, "source": "année écrite dans le titre",
                                    "source_text": raw.title.source_text if raw.title else None})
        elif values["execution_year"] != year:
            report["warnings"].append(f"année du titre ({year}) ≠ année de création lue ({values['execution_year']})")

    # Changements écrits « from X to Y » (suppléments) : lus par le code, prioritaires sur le modèle
    for field, (new, old, bullet) in explicit_changes(text).items():
        if new is None:
            report["warnings"].append(f"{field} : {bullet}")
            continue
        if field in ("class_a_shares_offered",):
            new = int(new)
        model_value = values.get(field)
        if model_value is not None and abs(float(model_value) - new) > 0.01:
            kind = "l'ANCIENNE valeur" if abs(float(model_value) - old) < 0.01 else "une autre valeur"
            report["warnings"].append(f"{field} : le modèle a lu {model_value} ({kind}), le document écrit "
                                      f"« from {old:,.0f} to {new:,.0f} » : nouvelle valeur retenue")
        values[field] = new
        report["audit"].append({"field": field, "value": new, "source_text": bullet, "source": "changement écrit (from → to)"})

    # Prix écrit dans deux devises (« £739,461.38, or $944,600 ») : lu par le code, prioritaire sur le modèle
    dual = dual_currency_purchase(text)
    if dual:
        price, currency, usd, sentence = dual
        for field, value in (("purchase_price", price), ("purchase_currency", currency), ("purchase_price_usd", usd)):
            model_value = values.get(field)
            if model_value is not None and str(model_value).upper() != str(value).upper() and not (
                    isinstance(value, float) and isinstance(model_value, (int, float)) and abs(model_value - value) < 0.01):
                report["warnings"].append(f"{field} : le modèle a lu {model_value!r}, le document écrit "
                                          f"le prix dans deux devises ({currency} {price:,.2f} / USD {usd:,.0f}) : "
                                          f"{value!r} retenu")
            values[field] = value
            report["audit"].append({"field": field, "value": value, "source_text": sentence,
                                    "source": "prix écrit dans deux devises"})

    # Un prix « d'origine » identique au prix en dollars, alors que la devise n'est pas l'USD (ou inconnue) :
    # c'est le montant en dollars recopié dans le mauvais champ (cas d'un supplément qui ne parle que de dollars)
    if (values.get("purchase_price") is not None and values.get("purchase_price_usd") is not None
            and abs(values["purchase_price"] - values["purchase_price_usd"]) < 0.01
            and (values.get("purchase_currency") or "").upper() != "USD"):
        report["rejected"].append(f"purchase_price = {values['purchase_price']!r} supprimé : identique au prix en "
                                  f"dollars alors que la devise d'origine n'est pas l'USD "
                                  f"({values.get('purchase_currency') or 'inconnue'})")
        values["purchase_price"] = None

    # Prix par action : secours par expression régulière, s'il n'a pas de preuve valable
    if values.get("price_per_class_a_share") is None:
        price, found = regex_price_per_share(text)
        shares, total = values.get("class_a_shares_offered"), values.get("aggregate_offering")
        if price is not None and shares and total and abs(shares * price - total) > 1:
            report["warnings"].append(f"prix par action {price} écarté : {shares:,} actions × {price} ≠ offre totale {total:,.0f}")
            price, found = None, None
        if price is not None:
            values["price_per_class_a_share"] = price
            report["audit"].append({"field": "price_per_class_a_share", "value": price,
                                    "source_text": found, "source": "regex"})
        elif found:
            report["warnings"].append(found)

    if values["purchase_currency"]:
        values["purchase_currency"] = str(values["purchase_currency"]).upper()
    record = OfferingRecord(**values)
    report["warnings"] += coherence_warnings(record)
    if not report["audit"] and not report["rejected"]:
        report["warnings"].append("le modèle n'a renvoyé AUCUNE valeur lisible : voir last_raw_response.json")
    return OfferingResult(record=record, **report)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("documents", nargs="+", help="Textes produits par extract_sections.py --profile offering.")
    parser.add_argument("--issuer", help="Nom officiel EDGAR de la société (un seul document à la fois).")
    args = parser.parse_args()
    if args.issuer and len(args.documents) > 1:
        parser.error("--issuer ne s'utilise qu'avec un seul document.")

    client = boto3.client("bedrock-runtime", region_name=REGION)
    results = []
    for path in args.documents:
        text = open(path, encoding="utf-8").read()
        result = process_offering(text, client=client, issuer_name=args.issuer)
        results.append((path, result))

        r = result.record
        print(f"\n{path} : {r.issuer_name or '?'} - {r.artist_name or '?'}, {r.title or '?'}")
        print(f"   {len(result.audit)} valeur(s) vérifiée(s), {len(result.rejected)} supprimée(s), "
              f"{len(result.missing)} laissée(s) vide(s) par le modèle")
        for x in result.rejected:
            print(f"   ✗ {x}")
        for x in result.warnings:
            print(f"   ⚠ {x}")
        if result.missing:
            print(f"   ∅ vides : {', '.join(result.missing)}")

    with open("offering_extractions.json", "w", encoding="utf-8") as f:
        json.dump([{"document": p, **res.record.model_dump(mode="json")} for p, res in results],
                  f, ensure_ascii=False, indent=2)
    with open("offering_audit.json", "w", encoding="utf-8") as f:
        json.dump([{"document": p, **res.model_dump(mode="json", include={"rejected", "warnings", "missing", "audit"})}
                   for p, res in results], f, ensure_ascii=False, indent=2)
    print(f"\n{len(results)} document(s) -> offering_extractions.json (+ offering_audit.json)")
    return results


if __name__ == "__main__":
    main()