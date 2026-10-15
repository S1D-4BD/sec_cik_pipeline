#!/usr/bin/env python3

#  python3 lib/extract_sections.py ../scrapers/formulaires_253G2/0001738134_0001493152-19-013454_form253g2 > lib/section.txt
#  python3 extract_sections.py filing.htm --map  > map.txt
#  python3 extract_sections.py filing.htm --profile offering -o offering.txt    # sections for the Masterworks table


import argparse
import re
import sys

from bs4 import BeautifulSoup


# "artwork" : the original configuration (artwork + sales history extraction), unchanged
SECTIONS = [
    {"pattern": r"^DESCRIPTION OF BUSINESS$", "subtitles": ["The Painting"]},
    {"pattern": r"^DESCRIPTION OF BUSINESS$", "subtitles": ["The Artist"]},
    {"pattern": r"^DETERMINATION OF OFFERING PRICE$", "subtitles": None},
    #{"pattern": r"^SECURITY OWNERSHIP", "subtitles": None},
    {"pattern": r"^SUMMARY$", "subtitles": ["Overview"]},
]

# "offering" : the 14 offering columns of the Masterworks table, in the 4 smallest sections that hold them.
# Tested on Masterworks 001 (253G2 of 2019-05-10): all 14 values present, 14,433 characters
# instead of 438,315 for the full document.
#   SUMMARY / Overview            -> issuer, date of incorporation, purchase date and price, offering size
#   SUMMARY / The Artist          -> artist name, when The Painting only says "the artist" (Masterworks 005)
#                                    ("?" = optional subtitle: no warning if a circular does not have it)
#   SUMMARY / The Painting        -> artist, title, year
#   DETERMINATION OF OFFERING PRICE -> purchase price, true-up, number of Class A shares, Class B shares
#   DILUTION                      -> price per share, aggregate cash cost of the Class B shares
#   DESCRIPTION OF BUSINESS / The Painting -> dimensions
OFFERING_SECTIONS = [
    {"pattern": r"^SUMMARY$", "subtitles": ["Overview", "?The Artist", "The Painting"]},
    {"pattern": r"^DETERMINATION OF OFFERING PRICE$", "subtitles": None},
    {"pattern": r"^DILUTION$", "subtitles": None},
    {"pattern": r"^DESCRIPTION OF BUSINESS$", "subtitles": ["The Painting"]},
]

PROFILES = {
    "artwork": {"sections": SECTIONS, "cover_page": False},
    "offering": {"sections": OFFERING_SECTIONS, "cover_page": False},
}


KEEP_COVER_PAGE = False
MAX_TITLE_LENGTH = 120

def norm(text):
    return " ".join(text.replace("\xa0", " ").split())  # basically clearing out weird symbols and extra whitespace

def load_file(file_path):
    with open(file_path, encoding="utf-8", errors="replace") as file:
        soup = BeautifulSoup(file.read(), "lxml")  # not html files, its close its htm (so not perfect but its like XML)

    root = soup.find("text") or soup.body  # the root element is actually <text> in these htm files
    if root is None:
        raise ValueError("Empty document")

    return root.find_all(["p", "table"], recursive=False)  # avoiding the extra <p> and <b> tags as text


def mapping_sections(blocks):
    # block = [paragraph0, paragraph1, paragraph2, etc...]
    # so index = 84, block = <p><a name="b_001"></a>blablablabal</p>
    anchors = []
    for index, block in enumerate(blocks):
        for a in block.find_all("a", attrs={"name": True}):  # any a tag gives us a title for that block
            title = norm(block.get_text())
            anchors.append((index, a["name"], title))
            break  # only one needed
    anchors.sort(key=lambda r: r[0])

    sections = []
    for rank, (index, a, title) in enumerate(anchors):
        end = anchors[rank + 1][0] if rank + 1 < len(anchors) else len(blocks)
        sections.append({"start": index, "finish": end, "title": title, "anchor": a})
    return sections


def is_subtitle(block, label=None):

    if block.name != "p":
        return False
    text = norm(block.get_text())
    if not text or len(text) > MAX_TITLE_LENGTH:
        return False
    emphasis = block.find_all(["b", "u"])
    if not emphasis:
        return False
    if norm(" ".join(e.get_text() for e in emphasis)) != text:
        return False
    if text.endswith("."):
        return False
    if text.upper() == text:
        return False
    # "The Artist" is not "THE ARTIST" suuuper important
    return text == label if label is not None else True


def sub_sections(blocks, start, end):
    #[(start_index, end_index, title)] of subsections within a range
    anchors = [(i, norm(blocks[i].get_text()))
               for i in range(start, end) if is_subtitle(blocks[i])]
    result = []
    for rank, (index, title) in enumerate(anchors):
        limit = anchors[rank + 1][0] if rank + 1 < len(anchors) else end
        result.append({"start": index, "finish": limit, "title": title})
    return result


def render_table(table):
    lines = []
    for row in table.find_all("tr"):
        cells = [norm(c.get_text()) for c in row.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if cells:
            lines.append(" | ".join(cells))
    return lines


def render_slice(blocks, start, end, keep_tables=True):
    lines = []
    for block in blocks[start:end]:
        if block.name == "table":
            if keep_tables:
                lines.extend(render_table(block))
            continue
        text = norm(block.get_text())
        if text:  
            lines.append(text)
    return lines


def extract(blocks, config=SECTIONS, cover_page=KEEP_COVER_PAGE):

    sections = mapping_sections(blocks)
    lines, warnings = [], []

    if cover_page:
        end = sections[0]["start"] if sections else len(blocks)
        lines.append("## SECTION | COVER PAGE")
        lines.extend(render_slice(blocks, 0, end))
        lines.append("")

    for rule in config:
        pattern = re.compile(rule["pattern"], re.IGNORECASE)
        found = [s for s in sections if pattern.search(s["title"])]

        if not found:
            warnings.append(f"section not found: {rule['pattern']}")
            continue
        if len(found) > 1:
            names = ", ".join(s["anchor"] for s in found)
            warnings.append(f"ambiguous pattern {rule['pattern']} -> {names} (all kept)")

        for section in found:
            lines.append(f"## SECTION | {section['title']}")
            wanted = rule.get("subtitles")

            if wanted is None:
                # full section, subtitles tagged along the way
                current = section["start"] + 1
                for sub in sub_sections(blocks, section["start"], section["finish"]):
                    lines.extend(render_slice(blocks, current, sub["start"]))
                    lines.append(f"### {sub['title']}")
                    current = sub["start"] + 1
                lines.extend(render_slice(blocks, current, section["finish"]))
            else:
                available = sub_sections(blocks, section["start"], section["finish"])
                index = {s["title"]: s for s in available}
                for label in wanted:
                    optional = label.startswith("?")
                    label = label.lstrip("?")
                    sub = index.get(label)
                    if sub is None and optional:
                        continue
                    if sub is None:
                        warnings.append(
                            f"subtitle not found: {label!r} "
                            f"in {section['title']!r}")
                        continue
                    lines.append(f"### {sub['title']}")
                    lines.extend(
                        render_slice(blocks, sub["start"] + 1, sub["finish"]))
            lines.append("")

    return lines, warnings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", help="the .htm file to read")
    parser.add_argument("-o", "--output", help="output file (default: stdout)")
    parser.add_argument("--map", action="store_true",
                        help="displays structure and exits without extracting")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="artwork",
                        help="which sections to extract (default: artwork, the original configuration)")
    args = parser.parse_args()

    blocks = load_file(args.file)

    if args.map:
        for section in mapping_sections(blocks):
            size = section["finish"] - section["start"]
            print(f"{section['anchor']:<8} {section['start']:>5} "
                  f"({size:>4} <p>)  {section['title']}")
            for sub in sub_sections(blocks, section["start"], section["finish"]):
                print(f"{'':<8} {sub['start']:>5}                 -- {sub['title']}")
        return 0

    profile = PROFILES[args.profile]
    lines, warnings = extract(blocks, config=profile["sections"], cover_page=profile["cover_page"])

    for warning in warnings:
        print(f"[!] {warning}", file=sys.stderr)

    text = "\n".join(lines)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as output_file:
            output_file.write(text + "\n")
        print(f"{len(lines)} lines -> {args.output}", file=sys.stderr)
    else:
        print(text)

    return 1 if any(w.startswith("section not found") for w in warnings) else 0


if __name__ == "__main__":
    sys.exit(main())