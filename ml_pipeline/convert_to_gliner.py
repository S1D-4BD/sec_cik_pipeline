import argparse
import json
import re

def convert_label_studio_to_gliner(item):
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
            end_char = value.get("end") - 1  # Index inclusif
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