"""
Évalue un modèle GLiNER sur un jeu annoté : précision, rappel, F1 (global et par label).

Usage :
    python evaluate_gliner.py --model gliner_model/<run_id>/final --eval eval.json
    python evaluate_gliner.py --model gliner_model/<run_id>/final --eval eval.json --errors
        (--errors affiche chaque erreur avec sa phrase)
"""

import argparse
import json
from collections import Counter


def to_char_spans(tokens, ner):
    """Reconstruit le texte (tokens séparés par des espaces) et convertit
    les annotations [début, fin, label] en positions de caractères."""
    starts, ends, pos = [], [], 0
    for tok in tokens:
        starts.append(pos)
        ends.append(pos + len(tok))
        pos += len(tok) + 1
    text = " ".join(tokens)
    gold = {(starts[s], ends[e], label) for s, e, label in ner}
    return text, gold


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f1


def compute_scores(model, data, labels, threshold=0.5):
    """Renvoie (précision, rappel, F1) globaux, les compteurs par label, et la liste des erreurs."""
    tp, fp, fn = Counter(), Counter(), Counter()
    errors = []
    for ex in data:
        text, gold = to_char_spans(ex["tokenized_text"], ex["ner"])
        preds = model.predict_entities(text, labels, threshold=threshold)
        pred = {(p["start"], p["end"], p["label"]) for p in preds}
        for s, e, lab in pred & gold:
            tp[lab] += 1                                   # trouvé, et juste
        for s, e, lab in pred - gold:
            fp[lab] += 1                                   # trouvé, mais faux
            errors.append(("FAUX", text[s:e], lab, text))
        for s, e, lab in gold - pred:
            fn[lab] += 1                                   # raté
            errors.append(("RATÉ", text[s:e], lab, text))

    counts = {lab: (tp[lab], fp[lab], fn[lab]) for lab in labels}
    total = prf(sum(tp.values()), sum(fp.values()), sum(fn.values()))
    return total, counts, errors


def format_report(total, counts):
    """Tableau lisible. « - » = rien à mesurer (aucun exemple et aucune proposition)."""
    lines = [f"{'label':<20}{'justes':>8}{'faux':>6}{'ratés':>7}{'précision':>11}{'rappel':>8}{'F1':>6}"]
    for lab, (t, f, m) in counts.items():
        if t + f + m == 0:
            lines.append(f"{lab:<20}{'-':>8}{'-':>6}{'-':>7}{'-':>11}{'-':>8}{'-':>6}")
            continue
        p, r, f1 = prf(t, f, m)
        lines.append(f"{lab:<20}{t:>8}{f:>6}{m:>7}{p:>11.2f}{r:>8.2f}{f1:>6.2f}")
    p, r, f1 = total
    t, f, m = (sum(c[i] for c in counts.values()) for i in range(3))
    lines.append(f"\n{'TOTAL':<20}{t:>8}{f:>6}{m:>7}{p:>11.2f}{r:>8.2f}{f1:>6.2f}")
    return "\n".join(lines)


def format_errors(errors):
    lines = []
    for kind, span, lab, text in errors:
        lines.append(f"[{kind}] {span!r} -> {lab}\n        dans : {text}")
    return "\n".join(lines) if lines else "Aucune erreur."


if __name__ == "__main__":
    from gliner import GLiNER

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--eval", required=True)
    parser.add_argument("--labels-from", default="train.json")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--errors", action="store_true", help="Afficher chaque erreur avec sa phrase.")
    args = parser.parse_args()

    data = json.load(open(args.eval, encoding="utf-8"))
    with open(args.labels_from, encoding="utf-8") as f:
        labels = sorted({lab for ex in json.load(f) for _, _, lab in ex["ner"]})

    model = GLiNER.from_pretrained(args.model)
    total, counts, errors = compute_scores(model, data, labels, args.threshold)

    print("\n" + format_report(total, counts))
    if args.errors:
        print("\n" + format_errors(errors))