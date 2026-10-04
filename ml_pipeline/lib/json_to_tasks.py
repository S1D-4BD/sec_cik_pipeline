import json
import nltk
from nltk.tokenize import sent_tokenize
nltk.download("punkt_tab", quiet=True)

ENTREE = "./lib/section.json"
SORTIE = "./lib/tasks.json"

data = json.load(open(ENTREE, encoding="utf-8"))

phrases = []
for obj in data:
    texte = obj.get("text", "")
    for bloc in texte.split("\n"):
        bloc = bloc.strip()
        if not bloc or bloc.startswith("#"):
            continue
        for ph in sent_tokenize(bloc):
            ph = ph.strip()
            if len(ph) >= 15:
                phrases.append({"text": ph})

json.dump(phrases, open(SORTIE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

print(f"{len(phrases)} phrases -> {SORTIE}")
for i, p in enumerate(phrases[:5], 1):
    print(f"  {i}. {p['text'][:90]}")