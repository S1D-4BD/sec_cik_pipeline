"""
import argparse
import json
import re

def convert_label_studio_to_gliner(item):

    text = item.get("data", {}).get("text", "")
    if not text:
        return None

    # Tokenisation basée sur les mots et la ponctuation
    tokens = re.findall(r"\w+|[^\w\s]", text)
    
    # Alignement caractères -> index des tokens
    char_to_token = {}
    current_idx = 0
    for token_idx, token in enumerate(tokens):
        start = text.find(token, current_idx)
        end = start + len(token)
        for c in range(start, end):
            char_to_token[c] = token_idx
        current_idx = end

    ner_labels = []
    annotations = item.get("annotations", [])
    if annotations and "result" in annotations[0]:
        for res in annotations[0]["result"]:
            value = res.get("value", {})
            start_char = value.get("start")
            end_char = value.get("end") - 1
            labels = value.get("labels", [])

            if start_char in char_to_token and end_char in char_to_token and labels:
                start_token = char_to_token[start_char]
                end_token = char_to_token[end_char]
                ner_labels.append([start_token, end_token, labels[0]])

    return {
        "tokenized_text": tokens,
        "ner": ner_labels
    }

def main():
    parser = argparse.ArgumentParser(description="Convertit des annotations Label Studio au format GLiNER JSON.")
    
    # Arguments CLI
    parser.add_argument("-i", "--input", nargs="+", required=True, help="Fichier(s) JSON source(s) (Label Studio).")
    parser.add_argument("-o", "--output", type=str, required=True, help="Chemin du fichier JSON de sortie.")
    parser.add_argument("--keep-empty", action="store_true", help="Conserver les phrases sans aucune entité annotée.")

    args = parser.parse_args()

    converted_dataset = []
    total_files = len(args.input)
    
    print(f"Traitement de {total_files} fichier(s)...")

    for file_path in args.input:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                
                for item in data:
                    converted_item = convert_label_studio_to_gliner(item)
                    if converted_item:
                        # Filtrer ou garder les exemples sans entités selon l'option
                        if args.keep_empty or len(converted_item["ner"]) > 0:
                            converted_dataset.append(converted_item)
                            
        except Exception as e:
            print(f"Erreur lors de la lecture de {file_path}: {e}")

    # Sauvegarde au format JSON
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(converted_dataset, f, ensure_ascii=False, indent=2)

    total_entities = sum(len(ex["ner"]) for ex in converted_dataset)
    print(f"\nConversion terminée avec succès !")
    print(f"  └─ Exemples exportés : {len(converted_dataset)}")
    print(f"  └─ Total d'entités : {total_entities}")
    print(f"  └─ Fichier créé : {args.output}")

if __name__ == "__main__":
    main()
"""

# python convert_to_gliner.py -i annotations_1.json annotations_2.json -o train.json --eval-output eval.json --humanize-labels
import argparse
import json
import random
import re


def humanize(label):
    """ARTIST_NAME -> 'artist name' : GLiNER comprend mieux les labels en langage naturel."""
    return label.lower().replace("_", " ")
## faut que je le change car c'est les labels de label studio a changer, pas ici vrm

def convert_label_studio_to_gliner(item, stats):
    """
    Convertit un enregistrement Label Studio vers le format requis par GLiNER.
    """
    text = item.get("data", {}).get("text", "")
    if not text:
        return None

    # Tokenisation basée sur les mots et la ponctuation
    tokens = re.findall(r"\w+|[^\w\s]", text)

    # Alignement caractères -> index des tokens
    char_to_token = {}
    token_starts, token_ends = set(), set()
    current_idx = 0
    for token_idx, token in enumerate(tokens):
        start = text.find(token, current_idx)
        end = start + len(token)
        for c in range(start, end):
            char_to_token[c] = token_idx
        token_starts.add(start)
        token_ends.add(end - 1)
        current_idx = end

    ner_labels = []
    annotations = item.get("annotations", [])
    if annotations and "result" in annotations[0]:
        for res in annotations[0]["result"]:
            value = res.get("value", {})
            start_char = value.get("start")
            end_char = value.get("end")
            labels = value.get("labels", [])

            # Ignore les résultats qui ne sont pas des spans (ex. classification)
            if start_char is None or end_char is None or not labels:
                continue
            end_char -= 1  # Index inclusif

            if start_char in char_to_token and end_char in char_to_token:
                start_token = char_to_token[start_char]
                end_token = char_to_token[end_char]
                ner_labels.append([start_token, end_token, labels[0]])
                # Le span coupe un mot : il est élargi au token entier, on le signale
                if start_char not in token_starts or end_char not in token_ends:
                    stats["misaligned"].append(
                        f"{text[value['start']:value['end']]!r} -> {' '.join(tokens[start_token:end_token + 1])!r}"
                    )
            else:
                stats["dropped"].append(repr(text[value["start"]:value["end"]]))

    return {
        "tokenized_text": tokens,
        "ner": ner_labels
    }


def save(path, dataset):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Convertit des annotations Label Studio au format GLiNER JSON.")

    # Arguments CLI
    parser.add_argument("-i", "--input", nargs="+", required=True, help="Fichier(s) JSON source(s) (Label Studio).")
    parser.add_argument("-o", "--output", type=str, required=True, help="Fichier JSON de sortie (jeu d'entraînement si --eval-output est donné).")
    parser.add_argument("--eval-output", type=str, help="Fichier JSON du jeu de validation. Active le découpage train/eval.")
    parser.add_argument("--eval-ratio", type=float, default=0.15, help="Part des exemples réservée à la validation (défaut : 0.15).")
    parser.add_argument("--seed", type=int, default=42, help="Graine du mélange aléatoire, pour un découpage reproductible.")
    parser.add_argument("--humanize-labels", action="store_true", help="Convertit ARTIST_NAME en 'artist name'.")
    parser.add_argument("--keep-empty", action="store_true", help="Conserver les phrases sans aucune entité annotée.")

    args = parser.parse_args()

    converted_dataset = []
    stats = {"dropped": [], "misaligned": []}
    total_files = len(args.input)

    print(f"Traitement de {total_files} fichier(s)...")

    for file_path in args.input:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

                for item in data:
                    converted_item = convert_label_studio_to_gliner(item, stats)
                    if converted_item:
                        # Filtrer ou garder les exemples sans entités selon l'option
                        if args.keep_empty or len(converted_item["ner"]) > 0:
                            converted_dataset.append(converted_item)

        except Exception as e:
            print(f"Erreur lors de la lecture de {file_path}: {e}")

    # Renommage des labels
    if args.humanize_labels:
        for ex in converted_dataset:
            ex["ner"] = [[s, e, humanize(lab)] for s, e, lab in ex["ner"]]

    # Découpage train / eval
    if args.eval_output:
        random.seed(args.seed)
        random.shuffle(converted_dataset)
        n_eval = max(1, int(len(converted_dataset) * args.eval_ratio))
        eval_set, train_set = converted_dataset[:n_eval], converted_dataset[n_eval:]
        save(args.output, train_set)
        save(args.eval_output, eval_set)
    else:
        train_set, eval_set = converted_dataset, []
        save(args.output, train_set)

    total_entities = sum(len(ex["ner"]) for ex in converted_dataset)
    labels = sorted({lab for ex in converted_dataset for _, _, lab in ex["ner"]})

    print(f"\nConversion terminée avec succès !")
    print(f"  └─ Exemples exportés : {len(converted_dataset)} (train : {len(train_set)}, eval : {len(eval_set)})")
    print(f"  └─ Total d'entités : {total_entities}")
    print(f"  └─ Labels : {labels}")
    print(f"  └─ Fichier(s) créé(s) : {args.output}" + (f", {args.eval_output}" if args.eval_output else ""))

    if stats["dropped"]:
        print(f"\n⚠ {len(stats['dropped'])} span(s) ignoré(s) car ils commencent ou finissent sur un espace :")
        for s in stats["dropped"][:10]:
            print(f"    {s}")
        print("  Corrigez ces annotations dans Label Studio pour ne pas les perdre.")

    if stats["misaligned"]:
        print(f"\n⚠ {len(stats['misaligned'])} span(s) coupent un mot et ont été élargis au mot entier :")
        for s in stats["misaligned"][:10]:
            print(f"    {s}")
        print("  Vérifiez que l'élargissement est correct.")


if __name__ == "__main__":
    main()