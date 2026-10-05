
import json
import re
import sys
from datetime import datetime
from typing import Generic, List, Literal, Optional, TypeVar

import boto3
from pydantic import BaseModel, Field, computed_field, field_validator


class ArtWorkInfo(BaseModel):
    artist_name: str
    link: str
    title: str
    id: Optional[str] = None
    image_url: Optional[str] = None
    size: Optional[tuple] = None
    medium: Optional[str] = None
    signature: Optional[bool] = None
    execution_year: Optional[int] = None
    description: Optional[str] = None
    description_raw: Optional[str] = None
    provenance: Optional[List[str]] = None
    literature: Optional[List[str]] = None
    exhibition: Optional[List[str]] = None
    catalogue_reference_code: Optional[str] = None
    catalogue_note_link: Optional[str] = None
    catalogue_note_text: Optional[str] = None
    is_oil_on_canvas: Optional[bool] = None
    surface_area: Optional[float] = None
    log_surface_area: Optional[float] = None
    prestige_signature: Optional[List[List[int]]] = None
    had_prestige_owner: Optional[bool] = None
    dominantly_prestige_owners: Optional[bool] = None
    recent_prestige_owner: Optional[bool] = None
    medium_lower_level: Optional[str] = None
    medium_higher_level: Optional[str] = None
    material: Optional[str] = None
    len_provenance: Optional[int] = None
    h_w_ratio: Optional[float] = None
    death_date: Optional[int] = None
    birth_date: Optional[int] = None
    authenticity_verified: bool = False
    authenticity_words: Optional[List[str]] = None
    subject: Optional[List[str]] = None
    subject_cluster: Optional[List[int]] = None
    colours: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    type: Optional[str] = None
    extra_notes_json_str: Optional[str] = None


MEDIUM = {
    "Painting": [
        "oil",
        "oil paint",
        "acrylic",
        "watercolor",
        "watercolour",
        "gouache",
        "tempera",
        "encaustic",
        "enamel",
        "casein",
        "fresco",
        "spray paint",
        "egg tempera",
        "ink wash",
        "distemper",
        "vinyl paint",
        "lacquer",
        "alkyd",
    ],
    "Drawing": [
        "graphite",
        "charcoal",
        "pastel",
        "sanguine",
        "silverpoint",
        "marker",
        "pen",
        "ink",
        "pencil",
        "colored pencil",
        "conte crayon",
        "chalk",
        "crayon",
        "sepia",
        "brush and ink",
    ],
    "Printmaking": [
        "relief printing",
        "etching",
        "intaglio",
        "lithography",
        "monotyping",
        "open work print",
        "screenprint",
        "linocut",
        "woodcut",
        "aquatint",
        "mezzotint",
        "drypoint",
        "engraving",
        "collagraph",
        "serigraph",
        "photoengraving",
        "photogravure",
        "wood engraving",
        "monoprint",
        "print",
        "planographic",
        "stencil",
        "carborundum print",
        "chine-collé",
        "giclée",
        "heliogravure",
        "intaglio print",
        "relief print",
        "screen printing",
        "solarplate etching",
        "viscosity printing",
        "silver print",
        "embossing",
        "terragraph"
    ],
    "Sculpture": [
        "bronze",
        "steel",
        "metal",
        "painted metal",
        "cast iron",
        "plaster",
        "marble",
        "glass",
        "crystal",
        "resin",
        "aluminum",
        "copper",
        "stone",
        "wood",
        "terracotta",
        "fiberglass",
        "concrete",
        "granite",
        "limestone",
        "soapstone",
        "jade",
        "basalt",
        "polyester",
        "polyurethane",
        "epoxy",
        "wax",
        "tin",
        "brass",
        "papier-mâché",
        "plexiglass",
        "plastic"
    ],
    "Photography": ["photography"],
    "Textile art": ["textile art", "embroidery"],
    "Ceramics": ["ceramics", "porcelain", "clay", "faience"],
    "Miscellaneous": [
        "toy",
        "disc",
        "album",
        "poster",
        "book",
        "manuscript",
        "letter",
        "jewelry",
        "multiples"
    ],
}

MATERIAL = [
    "canvas",
    "paper",
    "gessoed board", "cardboard", "board",  # virgule ajoutée : sans elle, Python créait "boardmasonite"
    "masonite",
    "bronze",
    "marble",
    "clay",
    "metal",
    "steel",
    "burlap",
    "glass",
    "crystal",
    "textile",
    "ceramics",
    "faience",
    "porcelain",
    "wood",
    "plaster",
    "silver",
    "fabric",
    "linen",
    "aluminum",
    "cast iron",
    "copper",
    "wire",
    "platinum",
    "gold"
]


MODEL_ID = "global.amazon.nova-2-lite-v1:0"
REGION = "us-east-1"
MEDIUM_VALUES = [v for values in MEDIUM.values() for v in values]
MEDIUM_TO_HIGHER = {v: k for k, values in MEDIUM.items() for v in values}

AuctionHouse = Literal["Christie's", "Sotheby's", "Phillips", "other"]
SaleType = Literal["auction", "private_sale", "dealer_or_gallery"]


T = TypeVar("T")


class Evidenced(BaseModel, Generic[T]):
    value: Optional[T] = None
    source_text: Optional[str] = None


def ev(description):
    return Field(None, description=description)


class ExtractedTransaction(BaseModel):
    sale_type: Optional[Evidenced[SaleType]] = ev(
        "auction = public auction. private_sale = NOT at public auction, even if an auction house arranged it. "
        "dealer_or_gallery = bought from a dealer or gallery.")
    auction_house_name: Optional[Evidenced[AuctionHouse]] = ev(
        "House that held the auction OR arranged the private sale. 'other' for any other house.")
    dealer_or_gallery_name: Optional[Evidenced[str]] = ev("Dealer or gallery name, copied exactly.")
    auction_name: Optional[Evidenced[str]] = ev(
        "Printed title of the sale, e.g. 'Impressionist and Modern Art Evening Sale'. "
        "Never a person, a buyer or a collector. Null if no sale title is written.")
    auction_location: Optional[Evidenced[str]] = ev("City of the sale, copied exactly.")
    auction_lot_number: Optional[Evidenced[str]] = ev("Lot number exactly as written, letters included (e.g. '18B').")
    auction_date: Optional[Evidenced[str]] = ev("YYYY-MM-DD, or YYYY-MM if the day is not written.")
    auction_year: Optional[Evidenced[int]] = ev("Year of the sale.")
    currency: Optional[Evidenced[str]] = ev("ISO 4217 code (GBP, USD, EUR, HKD...) of the currency written.")
    low_estimate: Optional[Evidenced[float]] = ev("Lower bound of an estimate or appraisal range. Null for a single value.")
    low_estimate_usd: Optional[Evidenced[float]] = ev("USD lower estimate, only if written in USD.")
    high_estimate: Optional[Evidenced[float]] = ev(
        "Upper bound of an estimate or appraisal range, or the single estimated or appraised value.")
    high_estimate_usd: Optional[Evidenced[float]] = ev("USD upper estimate, only if written in USD.")
    hammer_price: Optional[Evidenced[float]] = ev(
        "Hammer price WITHOUT premium, ONLY if the document uses the word 'hammer'. A price paid is premium_price.")
    hammer_price_usd: Optional[Evidenced[float]] = ev("USD hammer price, only if written in USD.")
    premium_price: Optional[Evidenced[float]] = ev("Price paid INCLUDING premium, or private sale price.")
    premium_price_usd: Optional[Evidenced[float]] = ev("USD price paid, only if written in USD.")
    bought_in: Optional[Evidenced[bool]] = ev("True only if written that the work did not sell at auction.")


class Extraction(BaseModel):
    artist_name: Optional[Evidenced[str]] = ev("Artist who made the artwork, copied exactly.")
    birth_date: Optional[Evidenced[int]] = ev(
        "Year the ARTIST was born. Look for 'born ... 1840', 'b. 1840', or the first year in '(1840-1926)'. "
        "source_text must include the word 'born'/'b.' or the parentheses.")
    death_date: Optional[Evidenced[int]] = ev(
        "Year the ARTIST died. Look for 'died in', 'his/her death in', 'd. 1926', or the second year in "
        "'(1840-1926)'. Null if the artist is alive or no death is written.")
    title: Optional[Evidenced[str]] = ev(
    "ONE title of the artwork, without the year, in its original language. If the document provides both the original title and an English translation (e.g., 'Le Rêve' (French) / 'The Dream' (English)), keep the ORIGINAL title ('Le Rêve'). Only use the English title if the original title is *not* provided or is in English.")
    execution_year: Optional[Evidenced[int]] = ev("Year the artwork was made.")
    medium: Optional[Evidenced[str]] = ev("Medium description, copied exactly, e.g. 'oil on canvas'.")
    medium_lower_level: Optional[Evidenced[Literal[tuple(MEDIUM_VALUES)]]] = ev(
        "Main medium from the allowed list. 'canvas' alone does not mean oil.")
    material: Optional[Evidenced[Literal[tuple(MATERIAL)]]] = ev("Main support or material from the allowed list.")
    size: Optional[Evidenced[List[float]]] = ev("Dimensions as numbers, height first, e.g. [90, 114].")
    signature: Optional[Evidenced[bool]] = ev("True if described as signed, False if described as unsigned.")
    subject: List[str] = Field(default_factory=list, description="What the artwork depicts: words copied from the document.")
    provenance: List[str] = Field(default_factory=list, description="Provenance entries, each copied exactly.")
    exhibition: List[str] = Field(default_factory=list, description="Exhibitions of THIS artwork, each copied exactly.")
    literature: List[str] = Field(default_factory=list, description="Publications about THIS artwork, each copied exactly.")
    transactions: List[ExtractedTransaction] = Field(
        default_factory=list, description="Every sale of THIS artwork, oldest first. Never other artworks.")

    @field_validator("subject", "provenance", "exhibition", "literature", mode="before")
    @classmethod
    def _unwrap_items(cls, items):
        """Nova emballe parfois chaque élément en {"value", "source_text"} : on garde la valeur.
        Chaque élément reste ensuite vérifié mot pour mot dans le document."""
        if items is None:
            return []
        if isinstance(items, (str, dict)):
            items = [items]
        cleaned = []
        for item in items:
            if isinstance(item, dict):
                item = item.get("value") or item.get("source_text")
            if item not in (None, ""):
                cleaned.append(str(item))
        return cleaned


# =====================================================================================
# 2. CE QUE LE SCRIPT RENVOIE : des valeurs vérifiées, sans les preuves
# =====================================================================================

class TransactionInfo(BaseModel):
    """Une vente vérifiée. Champs alignés sur votre dataclass Transaction, plus le type de vente."""
    sale_type: Optional[SaleType] = None
    auction_house_name: Optional[str] = None
    dealer_or_gallery_name: Optional[str] = None
    auction_name: Optional[str] = None
    auction_location: Optional[str] = None
    auction_lot_number: Optional[str] = None
    auction_date: Optional[str] = None
    auction_year: Optional[int] = None
    currency: Optional[str] = None
    low_estimate: Optional[float] = None
    low_estimate_usd: Optional[float] = None
    high_estimate: Optional[float] = None
    high_estimate_usd: Optional[float] = None
    hammer_price: Optional[float] = None
    hammer_price_usd: Optional[float] = None
    premium_price: Optional[float] = None
    premium_price_usd: Optional[float] = None
    bought_in: Optional[bool] = None

    @computed_field
    @property
    def is_private_sale(self) -> bool:
        return self.sale_type == "private_sale"

    def auction_datetime(self) -> Optional[datetime]:
        """La date au format datetime de votre dataclass Transaction (None si le jour manque)."""
        try:
            return datetime.strptime(self.auction_date, "%Y-%m-%d") if self.auction_date else None
        except ValueError:
            return None

class ArtworkOutput(ArtWorkInfo):
    """Votre ArtWorkInfo, champ pour champ, plus la liste de ses ventes (comme ArtWorkDetail.transactions)."""
    transactions: List[TransactionInfo] = []

class ArtworkRecord(BaseModel):
    artwork: ArtWorkInfo
    transactions: List[TransactionInfo]
    rejected: List[str] = []    # valeurs SUPPRIMÉES, avec la raison
    warnings: List[str] = []    # valeurs conservées mais à vérifier
    missing: List[str] = []     # champs laissés vides par le modèle (ni trouvés, ni supprimés)
    audit: List[dict] = []      # chaque valeur acceptée, avec sa preuve


# =====================================================================================
# 3. LE PROMPT
# =====================================================================================
PROMPT = """You extract structured data about ONE artwork from a document (usually an SEC offering circular),
including its full sales history.

GOAL: COMPLETE, BUT ONLY WHAT IS WRITTEN
- Fill EVERY field the document states. Leaving out a value that is written is an error.
- Every value is checked automatically against the document, so extract every written value without hesitation.
- Never invent, guess, convert currencies or use outside knowledge. If a value is not written, leave it null.
- Classification fields (sale_type, hammer_price vs premium_price, low vs high estimate) need a written clue.
  Without the clue, leave them null.

EVIDENCE
- source_text = the SHORTEST passage of the document that contains the value, at most one sentence,
  copied character for character.
- The value must be readable inside its source_text.
- Only exception: if a value's parts are in two places (a day and its year), cite both in document order,
  joined by "..." (e.g. "In 2018...on June 20").

STEP 1 - Locate these parts. They can be in any order, under any heading:
  a. Artwork description: title, year, medium, support, size, signature, what it depicts.
  b. Artist biography: birth and death years.
  c. Provenance list: usually under a "Provenance" heading, one owner per line.
  d. Sale paragraphs: how the current owner acquired the work, earlier auctions, estimates, appraisals, prices.
  e. Exhibition and literature lists, if any.
  Ignore everything else: market statistics, sales of OTHER artworks, legal text, fees, the share offering.

STEP 2 - Artwork fields
- title: ONE title. If an original title and a translation are both given ("X, or Y", "X (Y)"), keep X.
- execution_year: the year the work was made or dated.
- medium: the words naming the technique, as written ("oil", "oil on canvas").
- medium_lower_level: closest allowed value, cited from words containing it ("oil paintings" -> "oil").
- material: the support, from the allowed list. Any mention counts ("corner of the canvas" -> "canvas").
- size: numbers, centimetres if given, height first.
- signature: true if the document mentions a signature, signed, inscribed, monogram or stamp on the work;
  false only if it says unsigned.
- subject: short phrases copied from the document saying what the work depicts.
- birth_date / death_date: the ARTIST's years only. Take the year out of a full date. source_text must contain
  "born", "b.", "died", "death", "d." or the range "(1840-1926)".

STEP 3 - Lists
- provenance: copy EVERY line of the provenance list, in order, one entry per line, without bullets.
  If the document has a provenance list, provenance is never empty.
- exhibition / literature: every entry about THIS artwork, copied exactly.

STEP 4 - Transactions: one per sale of THIS artwork, plus auctions where it did not sell.
Build them from BOTH the provenance lines and the sale paragraphs:
- For each provenance line, decide whether the owner BOUGHT the work.
  YES: "acquired from", "purchased", "bought", "sold", "his/her sale", "through <house>", "lot <n>".
  NO: "by descent", "gift", "bequest", "by <year>" with no acquisition verb, "Private collection" alone.
- A provenance line and a paragraph about the same sale (same date, or same house and year) are ONE
  transaction: merge their details.
- An auction where the work did not sell: bought_in true.
- sale_type:
  "auction" if the document says auction, "his/her sale" or gives a lot number;
  "private_sale" if privately, private treaty or negotiated, EVEN IF an auction house arranged it;
  "dealer_or_gallery" if a dealer or gallery is the buyer or seller (dealer_or_gallery_name = its name);
  otherwise null.
- auction_house_name, auction_location (city), auction_date (YYYY-MM-DD, or YYYY-MM if no day), auction_year,
  auction_lot_number exactly as written ("18B").
- auction_name: only a printed sale title ("Impressionist and Modern Art Evening Sale"). Otherwise null.
- Estimates: a range "A - B" -> low_estimate = A, high_estimate = B. A single estimate or appraisal ->
  high_estimate. An estimate is NEVER a price. An appraisal never creates a transaction.
- premium_price: the price paid, in the sale currency. ALWAYS extract a private sale price when written.
- hammer_price: only if the word "hammer" is used.
- currency: the currency of the estimates and prices.
- *_usd fields: only an amount the document writes in US dollars. If the sale currency is USD, fill both.
- Order transactions oldest first. Never invent a date.

STEP 5 - Before answering, check:
- every provenance line saying the work was bought has its transaction;
- every transaction has house, city, date, lot and price whenever the document gives them."""
# =====================================================================================
# 4. LES CONTRÔLES : c'est ici que se joue la sécurité
# =====================================================================================

def normalize(s):
    """Minuscules, guillemets typographiques remplacés, espaces regroupés."""
    for a, b in {"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "₤": "£"}.items():
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip().lower()


def numbers_in(text):
    """Tous les nombres écrits dans un texte : « £800,625 » -> 800625, « $4.9 million » -> 4900000."""
    found = []
    # « 1-1.5 million » : le multiplicateur s'applique aussi au premier nombre
    text = re.sub(r"(\d[\d,.]*)\s*(-|–|—|to)\s*(\d[\d,.]*)\s*(million|billion|bn|m\b)",
                  r"\1 \4 \2 \3 \4", text.lower())
    for num, mult in re.findall(r"(\d[\d,.]*)\s*(million|billion|bn|m\b|k\b)?", text):
        num = num.rstrip(".,")
        if num.count(".") > 1:          # 1.800.000 -> 1800000
            num = num.replace(".", "")
        if "," in num and "." not in num:
            parts = num.split(",")
            if all(len(p) == 3 for p in parts[1:]):
                num = num.replace(",", "")          # 800,625 -> 800625 (milliers)
            elif len(parts) == 2:
                num = num.replace(",", ".")         # 167,6 -> 167.6 (décimale française)
        num = num.replace(",", "")
        try:
            value = float(num)
        except ValueError:
            continue
        value *= {"million": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "k": 1e3}.get(mult, 1)
        found.append(value)
    return found


def number_matches(value, source):
    """Comparaison EXACTE : 1927 ne se lit pas dans « 1926 »."""
    return any(abs(n - value) < 0.01 for n in numbers_in(source))


MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]

CURRENCY_SIGNS = {"GBP": ["£", "₤", "gbp", "pound"], "USD": ["$", "usd", "dollar"], "EUR": ["€", "eur", "euro"],
                  "HKD": ["hk$", "hkd"], "CHF": ["chf"], "JPY": ["¥", "jpy", "yen"], "CNY": ["rmb", "cny", "yuan"]}

SALE_TYPE_SIGNS = {"private_sale": ["privat", "private treaty", "negotiated"],
                   "auction": ["auction", "evening sale", "day sale", "lot "],
                   "dealer_or_gallery": ["gallery", "galerie", "dealer", "fine art",
                                         "acquired from the artist", "purchased from the artist"]}

BOUGHT_IN_SIGNS = ["bought in", "bought-in", "unsold", "did not sell", "not sold", "failed to sell",
                   "no sale", "failed to reach", "reserve", "passed", "did not find a buyer"]

BIRTH_SIGNS = ["born", "b.", "née", "nee "]
DEATH_SIGNS = ["died", "death", "d.", "passed away"]
YEAR_RANGE = re.compile(r"\d{4}\s*-\s*\d{4}")
SALE_NAME_SIGNS = ["sale", "auction", "vente"]

HOUSE_SIGNS = {"Christie's": ["christie"], "Sotheby's": ["sotheby"], "Phillips": ["phillips"], "other": []}


def date_matches(value, source):
    """'2019-10-04' doit se lire dans la preuve : l'année, le mois et le jour."""
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
    """La valeur se lit-elle dans sa preuve ? Un contrôle adapté à chaque type de champ."""
    s = normalize(source)
    if kind == "text":
        return normalize(str(value)) in s
    if kind == "number":
        return number_matches(float(value), source)
    if kind == "numbers":
        return all(number_matches(float(v), source) for v in value)
    if kind == "date":
        return date_matches(str(value), source)
    if kind == "currency":
        return any(sign in s for sign in CURRENCY_SIGNS.get(value, [value.lower()]))
    if kind == "sale_type":
        return any(sign in s for sign in SALE_TYPE_SIGNS[value])
    if kind == "house":
        return all(sign in s for sign in HOUSE_SIGNS[value])
    if kind == "signature":
        return any(w in s for w in (["unsigned", "not signed"] if value is False else ["sign", "inscri", "monogram", "stamp"]))
    if kind == "bought_in":
        return value is False or any(w in s for w in BOUGHT_IN_SIGNS)
    if kind == "birth":
        return number_matches(float(value), source) and (any(w in s for w in BIRTH_SIGNS) or bool(YEAR_RANGE.search(s)))
    if kind == "death":
        return number_matches(float(value), source) and (any(w in s for w in DEATH_SIGNS) or bool(YEAR_RANGE.search(s)))
    if kind == "hammer":
        return number_matches(float(value), source) and "hammer" in s
    if kind == "lot":
        return normalize(str(value)) in s
    if kind == "sale_name":
        return normalize(str(value)) in s and any(w in normalize(str(value)) for w in SALE_NAME_SIGNS)
    return False


def fragments_of(source):
    """Une preuve coupée (« Monet...until his death in 1926 ») = plusieurs morceaux du document."""
    return [f.strip(" ,;") for f in re.split(r"\.\.\.|…", source) if f.strip(" ,;")]


CURRENCY_CHARS = re.compile(r"[$\u00a2-\u00a5\u20a0-\u20cf]")   # $ ¢ £ ¤ ¥ et ₠…₿ (€, ₤, ₹…)


def loose(s):
    """normalize(), puis tous les symboles monétaires remplacés par « ¤ » : le modèle les confond."""
    return CURRENCY_CHARS.sub("¤", normalize(s))


def in_document(source, document):
    """Chaque morceau de la preuve doit se trouver dans le document, dans le même ordre.
    Renvoie les passages TELS QU'ÉCRITS dans le document (avec le bon symbole), ou None."""
    doc, real, position, passages = loose(document), normalize(document), 0, []
    for fragment in fragments_of(source):
        frag = loose(fragment)
        found = doc.find(frag, position)
        if found == -1:
            return None
        passages.append(real[found:found + len(frag)])
        position = found + len(frag)
    return " ".join(passages)


def check(field, item, kind, document, report):
    """Les trois contrôles. Renvoie la valeur si elle les passe tous, sinon None."""
    if item is None or item.value is None or item.value == []:
        return None
    value, source = item.value, item.source_text
    if not source or not fragments_of(source):
        reason = "aucune preuve"
    elif (passage := in_document(source, document)) is None:
        reason = f"preuve absente du document : {source!r}"
    elif not value_matches(kind, value, passage):
        reason = f"la valeur ne se lit pas dans la preuve {source!r}"

    else:
        report["audit"].append({"field": field, "value": value, "source_text": source})
        return value
    report["rejected"].append(f"{field} = {value!r} supprimé : {reason}")
    return None


def check_list(field, items, document, report):
    """Chaque élément d'une liste doit être recopié du document, sinon il est supprimé."""
    kept = []
    for item in items:
        if item and normalize(item) in normalize(document):
            kept.append(item)
        else:
            report["rejected"].append(f"{field} : {item!r} supprimé : absent du document")
    return kept


ARTWORK_KINDS = {"artist_name": "text", "title": "text", "execution_year": "number", "medium": "text",
                 "medium_lower_level": "text", "material": "text", "size": "numbers",
                 "signature": "signature", "birth_date": "birth", "death_date": "death"}

TRANSACTION_KINDS = {"sale_type": "sale_type", "auction_house_name": "house", "dealer_or_gallery_name": "text",
                     "auction_name": "sale_name", "auction_location": "text", "auction_lot_number": "lot",
                     "auction_date": "date", "auction_year": "number", "currency": "currency",
                     "low_estimate": "number", "low_estimate_usd": "number", "high_estimate": "number",
                     "high_estimate_usd": "number", "hammer_price": "hammer", "hammer_price_usd": "hammer",
                     "premium_price": "number", "premium_price_usd": "number", "bought_in": "bought_in"}


# =====================================================================================
# 5. LES FONCTIONS PRINCIPALES
# =====================================================================================

def schema_for_bedrock(model):
    """Le schéma JSON du modèle Pydantic, avec les sous-modèles recopiés en place (sans "$ref")."""
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


def ask_model(client, text):
    """Envoie UN document à Bedrock et renvoie la réponse brute, validée par Pydantic."""
    response = client.converse(
        modelId=MODEL_ID,
        system=[{"text": PROMPT}],
        messages=[{"role": "user", "content": [{"text": f"<document>\n{text}\n</document>"}]}],
        toolConfig={
            "tools": [{"toolSpec": {"name": "save", "inputSchema": {"json": schema_for_bedrock(Extraction)}}}],
            "toolChoice": {"any": {}},
        },
        inferenceConfig={"temperature": 0, "maxTokens": 8000},
    )
    for block in response["output"]["message"]["content"]:
        if "toolUse" in block:
            return Extraction.model_validate(block["toolUse"]["input"])
    raise ValueError("Le modèle n'a pas renvoyé de réponse structurée.")


YEAR = r"(1[5-9]\d\d|20[0-2]\d)"
BORN = re.compile(r"born\b[^.]{0,80}?\b" + YEAR + r"\b", re.I)
DIED = re.compile(r"(?:died|death)\b[^.]{0,60}?\b" + YEAR + r"\b", re.I)
LIFESPAN = re.compile(r"\(\s*(?:b\.\s*)?" + YEAR + r"\s*[-–]\s*" + YEAR + r"?\s*\)")   # (1840-1926)

# Pour chaque champ : (motif, numéro du groupe qui contient l'année), dans l'ordre d'essai
BORN_ABBR = re.compile(r"(?<![a-z])b\.\s*" + YEAR + r"\b", re.I)                          # b. 1977
DIED_ABBR = re.compile(r"(?<![a-z])d\.\s*" + YEAR + r"\b", re.I)                          # d. 1926
REGEX_FALLBACKS = {"birth_date": [(BORN, 1), (BORN_ABBR, 1), (LIFESPAN, 1)],
                   "death_date": [(DIED, 1), (DIED_ABBR, 1), (LIFESPAN, 2)]}


# Fin de phrase : un point suivi d'un espace ou d'un retour à la ligne, ou un retour à la ligne seul.
# « b. 1977 » et « d. 1926 » ne sont pas des fins de phrase.
SENTENCE_END = re.compile(r"(?<![\s(]b\.)(?<![\s(]d\.)(?<=[.!?])\s+|\n+")


def regex_year(options, text, surname):
    """Secours quand le modèle n'a pas donné d'année valable : on cherche l'année dans le texte, mais
    seulement dans une phrase qui contient le nom de famille de l'artiste. Renvoie (année, texte trouvé)."""
    if not surname:
        return None, None
    for pattern, group in options:
        for sentence in SENTENCE_END.split(text):
            if surname.lower() not in sentence.lower():
                continue
            for m in pattern.finditer(sentence):
                if m.group(group) is not None:
                    return int(m.group(group)), m.group(0)
    return None, None


def coherence_warnings(birth, death, execution_year):
    """Contrôles de bon sens : signalés, jamais corrigés automatiquement."""
    w = []
    if birth and death:
        if birth >= death:
            w.append(f"naissance ({birth}) après ou égale au décès ({death})")
        elif not 15 <= death - birth <= 110:
            w.append(f"durée de vie improbable : {death - birth} ans ({birth}-{death})")
    if execution_year and birth and execution_year <= birth:
        w.append(f"œuvre ({execution_year}) antérieure à la naissance ({birth})")
    if execution_year and death and execution_year > death:
        w.append(f"œuvre ({execution_year}) postérieure au décès ({death}) : à vérifier")
    return w


def process_document(text, link="", client=None) -> ArtworkRecord:
    """Un texte -> un ArtworkRecord dont CHAQUE valeur a été vérifiée dans le document."""
    client = client or boto3.client("bedrock-runtime", region_name=REGION)
    raw = ask_model(client, text)
    report = {"rejected": [], "warnings": [], "audit": [], "missing": []}

    a = {f: check(f, getattr(raw, f), kind, text, report) for f, kind in ARTWORK_KINDS.items()}

    # Secours regex pour les années de l'artiste, si le modèle ne les a pas renvoyées
    surname = a["artist_name"].split()[-1] if a["artist_name"] else ""
    for field, options in REGEX_FALLBACKS.items():
        if a[field] is None:          # absente OU supprimée : on cherche une preuve valable dans le texte
            year, found = regex_year(options, text, surname)
            if year is not None:
                a[field] = year
                report["audit"].append({"field": field, "value": year, "source_text": found, "source": "regex"})

    # Champs laissés vides par le modèle : ni trouvés, ni supprimés
    for f in ARTWORK_KINDS:
        item = getattr(raw, f)
        if a[f] is None and (item is None or item.value is None):
            report["missing"].append(f)
    for f in ("subject", "provenance", "exhibition", "literature"):
        if not getattr(raw, f):
            report["missing"].append(f)

    report["warnings"] += coherence_warnings(a["birth_date"], a["death_date"], a["execution_year"])
    artwork = ArtWorkInfo(
        artist_name=a["artist_name"] or "",
        title=a["title"] or "",
        link=link,
        execution_year=a["execution_year"],
        medium=a["medium"],
        medium_lower_level=a["medium_lower_level"],
        medium_higher_level=MEDIUM_TO_HIGHER.get(a["medium_lower_level"]),
        material=a["material"],
        size=tuple(a["size"]) if a["size"] else None,
        signature=a["signature"],
        birth_date=a["birth_date"],
        death_date=a["death_date"],
        subject=check_list("subject", raw.subject, text, report) or None,
        provenance=check_list("provenance", raw.provenance, text, report) or None,
        exhibition=check_list("exhibition", raw.exhibition, text, report) or None,
        literature=check_list("literature", raw.literature, text, report) or None,
    )

    transactions = []
    for i, t in enumerate(raw.transactions):
        values = {f: check(f"transaction {i}.{f}", getattr(t, f), kind, text, report)
                  for f, kind in TRANSACTION_KINDS.items()}
        if all(v is None for v in values.values()):
            report["rejected"].append(f"transaction {i} supprimée : aucune valeur vérifiable")
            continue
        # Un prix identique en devise d'origine et en USD alors que la devise n'est pas l'USD :
        # c'est le montant USD recopié à tort dans le champ principal
        for name in ("premium_price", "hammer_price", "low_estimate", "high_estimate"):
            if (values["currency"] and values["currency"] != "USD" and values[name] is not None
                    and values[name] == values[name + "_usd"]):
                report["rejected"].append(f"transaction {i}.{name} = {values[name]!r} supprimé : "
                                          f"identique au montant USD alors que la devise est {values['currency']}")
                values[name] = None
        tx = TransactionInfo(**values)
        if tx.sale_type is None and (tx.premium_price or tx.hammer_price):
            report["warnings"].append(f"transaction {i} : un prix sans type de vente vérifié (vente privée ?)")
        transactions.append(tx)

    return ArtworkRecord(artwork=artwork, transactions=transactions, **report)


def main():
    """En ligne de commande : traite les fichiers donnés et écrit extractions.json."""
    client = boto3.client("bedrock-runtime", region_name=REGION)
    records = []
    for path in sys.argv[1:]:
        text = open(path, encoding="utf-8").read()
        record = process_document(text, client=client)
        records.append(record)

        print(f"\n{path} : {record.artwork.artist_name or '?'} - {record.artwork.title or '?'}")
        for t in record.transactions:
            flag = "★ VENTE PRIVÉE" if t.is_private_sale else (t.sale_type or "type non vérifié")
            price = t.premium_price or t.hammer_price
            usd = t.premium_price_usd or t.hammer_price_usd
            if price:
                price_txt = f"{price:,.0f} {t.currency or ''}".strip()
                if usd and t.currency != "USD":
                    price_txt += f" ({usd:,.0f} USD)"
            else:
                price_txt = f"{usd:,.0f} USD" if usd else "-"
            print(f"   {flag} | {t.auction_house_name or t.dealer_or_gallery_name or '?'} | "
                  f"{t.auction_date or t.auction_year or '?'} | {price_txt}")
        print(f"   {len(record.audit)} valeur(s) vérifiée(s), {len(record.rejected)} supprimée(s)")
        for r in record.rejected:
            print(f"   ✗ {r}")
        for w in record.warnings:
            print(f"   ⚠ {w}")
        if record.missing:
            print(f"   ∅ laissés vides par le modèle : {', '.join(record.missing)}")

    # Le fichier propre : votre ArtWorkInfo à plat, plus ses ventes, sans aucune preuve
    clean = [ArtworkOutput(**r.artwork.model_dump(), transactions=r.transactions).model_dump(mode="json")
             for r in records]
    with open("extractions.json", "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)

    # Le fichier de contrôle : preuves, valeurs supprimées, avertissements
    audit = [{"document": path, **r.model_dump(mode="json", include={"rejected", "warnings", "missing", "audit"})}
             for path, r in zip(sys.argv[1:], records)]
    with open("extractions_audit.json", "w", encoding="utf-8") as f:
        json.dump(audit, f, ensure_ascii=False, indent=2)

    print(f"\n{len(records)} document(s) -> extractions.json (+ extractions_audit.json)")
    return records


if __name__ == "__main__":
    main()