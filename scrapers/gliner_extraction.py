#!/usr/bin/env python3
import json
import re
import sys
from pathlib import Path

from gliner import GLiNER


LABELS = [
    "artist",
    "painting title",
    "gallery",
    "auction house",
    "medium",
]

FIELD_OF_LABEL = {
    "artist":         "artist",
    "painting title": "artwork",
    "gallery":        "venue",
    "auction house":  "venue",
    "medium":         "medium",
}


# 1 500 000 $ / $1,500,000 / USD 1.2 million / EUR 850,000
PRICE_RE = re.compile(
    r"""(?:
            (?:US\$|\$|USD|EUR|GBP|CHF|€|£)\s?[\d][\d,.\s]*(?:\s?(?:million|billion|m|bn))?
          | [\d][\d,.\s]*\s?(?:USD|EUR|GBP|CHF|€|£|\$)
        )""",
    re.VERBOSE | re.IGNORECASE,
)

# 24 x 36 in. / 61 × 91,4 cm / 24 1/2 x 30 x 2 in
NOMBRE = r"\d+(?:[.,]\d+)?(?:\s?[¼½¾⅓⅔⅛⅜⅝⅞]|\s\d+/\d+)?"   # 46.4 | 18 ¼ | 24 1/2
UNITE  = r"(?:\s?(?:cm|mm|m|in\.?|inches|ft\.?|feet))?"      # facultative après chaque nombre
SEP    = r"\s?(?:x|×|by)\s?"

DIM_RE = re.compile(
    rf"(?<![\d$€£,.])"              # pas collé à un autre nombre ou à un prix
    rf"{NOMBRE}{UNITE}"
    rf"{SEP}{NOMBRE}{UNITE}"
    rf"(?:{SEP}{NOMBRE}{UNITE})?"   # 3e dimension, facultative
    rf"(?=\W|$)",
    re.I,
)

def dimensions(texte):
    for m in DIM_RE.finditer(texte):
        s = m.group().strip()
        if re.search(r"cm|mm|\bm\b|in\b|inches|ft|feet", s, re.I):   # au moins une unité
            return s
    return None


MATERIALS = (
    r"oil|acrylic|watercolou?r|gouache|ink|charcoal|graphite|pencil|tempera|"
    r"pastel|collage|silkscreen|screenprint|lithograph|etching|photograph|"
    r"mixed media|encaustic|enamel"
)
SUPPORTS = (
    r"canvas|linen|panel|paper|board|masonite|wood|copper|silk|vellum|"
    r"aluminium|aluminum|plexiglass"
)
MEDIUM_RE = re.compile(rf"\b(?:{MATERIALS})\s+on\s+(?:{SUPPORTS})\b", re.IGNORECASE)



WINDOW_WORDS = 200
OVERLAP_WORDS = 40

def windows(text, size=WINDOW_WORDS, overlap=OVERLAP_WORDS):
    words = text.split()
    if not words:
        return
    step = max(1, size - overlap)
    for i in range(0, len(words), step):
        chunk = words[i:i + size]
        if chunk:
            yield " ".join(chunk)
        if i + size >= len(words):
            break



def extract(model, text, threshold=0.35):
    found = {f: [] for f in ("artist", "artwork", "venue", "medium",
                             "price", "dimensions")}
    seen = set()

    def add(field, value, score=1.0):
        value = value.strip(" .,;:\n\t")
        if not value:
            return
        key = (field, value.lower())
        if key not in seen:
            seen.add(key)
            found[field].append({"text": value, "score": round(score, 2)})


    for chunk in windows(text):
        for ent in model.predict_entities(chunk, LABELS, threshold=threshold):
            field = FIELD_OF_LABEL.get(ent["label"])
            if field:
                add(field, ent["text"], ent["score"])

    for m in PRICE_RE.finditer(text):
        add("price", m.group())
    for m in DIM_RE.finditer(text):
        add("dimensions", m.group())
    for m in MEDIUM_RE.finditer(text):
        add("medium", m.group())

    return found



DEMO = ( 
    """
As evidenced by the artist’s signature and date on the verso of the canvas, 1 Colored Marilyn (reversal series), is one of the earliest works from a series of oil and silkscreen paintings completed by Andy Warhol between 1979 - 1986. In the works, Warhol revisited one of his most iconic and enduring subjects, the Hollywood star Marilyn Monroe. The Painting, which carried a pre-sale estimate of $1,500,000 - $2,000,000 in Phillips’ November 2017 New York auction, measures 46.4 cm by 34.9 cm (18 ¼ in x 13 ¾ in) and was created in 1979. Following its execution, the work was acquired by an American collector from Warhol’s primary dealer, Bruno Bischofberger, in 1984. The Painting remained in the Los Angeles collection of Douglas S. Cramer after its initial purchase, until its acquisition by Masterworks in 2017.
Provenance.
Bruno Bischofberger Gallery, Zurich
Douglas S. Cramer, Los Angeles (acquired from the above in 1984).
History of Prior Sales.
Set forth below is the historical data for Andy Warhol’s Marilyn Monroe Reversal Series sold at public auction from 1990 to 2018. Each of the considered works was created using the same technique between 1979 and 1986, with a singular image of Marilyn Monroe as the subject matter. We have included all works that feature at least one color, outside of the black negative of the image, and excluded those which lack color or hue, such as the Grey Marilyn or Black on Black Reversals, which we believe are not representative of the Painting. The data was sourced from publicly available auction records and does not include private sales. Such data may be incomplete or inaccurate. Although paintings in the Reversal Series have similar characteristics, each individual painting is unique in terms of artistic content, coloring, condition, provenance and other factors. We, therefore, cannot make any determination or representation that any of the data set forth below is useful in determining the value of the Painting and you are urged not to place undue reliance on such data.
Based on the auction sale data presented above, an investment in a composite index of Reversal Series paintings without grey or black works at the beginning of 2008 would have yielded a gross annualized return which is significantly higher than the performance of the S&P 500 Index with dividend reinvestment or the 10-Year U.S. Government Bond Index during the same period, as reflected in the chart below. The historical composite return of the Reversal Series is useful to show historical trends in the value of similar works, but it is not a direct proxy for performance of the Class A ordinary shares, since it reflects composite changes in sales prices and not the returns on any particular painting, there is no current market for the Class A ordinary shares, and it does not include the fees and selling costs described in this Offering Circular. While the below graph shows historical appreciation in the value of the Painting, investors in this offering will only receive net proceeds from the sale of the Painting, if any, after the sale of the Painting and only after reduction of fees and expenses payable by the company are paid. In addition, the past performance of the Reversal Series paintings is not necessarily indicative of future performance. Accordingly, investors should not place undue reliance on historical trends.
INDICES ARE UNMANAGED. AN INVESTOR CANNOT INVEST DIRECTLY IN AN INDEX. INDICES ARE USED FOR COMPARAT
IVE MODELLING PURPOSES ONLY. THE TIMING OF TRANSACTIONS RELATING TO AN ASSET OR PORTFOLIO, ADVISORY, AND TRANSACTION FEES, AND OTHER MANAGEMENT ACTIVITIES CAN CREATE SIGNIFICANT DIFFERENCES BETWEEN THE PERFORMANCE OF AN INDEX AND AN INVESTMENT SEEKING SIMILAR OR SUPERIOR RELATIVE PERFORMANCE RESULTS
The above disclosures in this section represent auction sales only and do not purport to include data regarding the total number of Andy Warhol Marilyn (Reversal Series) paintings currently in existence. The Company has been unable to find a reliable source of information regarding the total number of Andy Warhol Marilyn (Reversal Series) paintings currently in existence and therefore is unable to provide such information at this time.

"""
)


def main():
    print("Chargement du modele...")
    # gliner_small : rapide, suffisant pour demarrer.
    # Passe a "urchade/gliner_medium-v2.1" si la qualite ne suffit pas.
    model = GLiNER.from_pretrained("urchade/gliner_small-v2.1")

    if len(sys.argv) > 1:
        folder = Path(sys.argv[1])
        files = sorted(folder.glob("*.txt"))
        if not files:
            print(f"Aucun .txt trouve dans {folder}")
            return
        results = {}
        for path in files:
            print(f"  {path.name}")
            results[path.name] = extract(model, path.read_text(encoding="utf-8"))
        out = Path("resultats.json")
        out.write_text(json.dumps(results, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        print(f"\nEcrit dans {out.resolve()}")
    else:
        print("\n--- Demo ---\n")
        for field, values in extract(model, DEMO).items():
            shown = ", ".join(f"{v['text']} ({v['score']})" for v in values)
            print(f"{field:12} {shown or '-'}")


if __name__ == "__main__":
    main()