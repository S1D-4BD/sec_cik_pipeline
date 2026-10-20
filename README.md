# sec_cik_pipeline
---
##### AUTOMATIC (GiHub Actions)
- run sec_cik_pipeline
##### Manually : scraping the CIK's
- /scrapers/sec_cik_scraper.py
##### Manually : scraping the 253G2 filings (from CIK's)
- /scrapers/sec_253G2_scraper.py

---
##### Prepare text for ML Pipeline from filing :
- ml_pipeline/lib/extract_sections.py
- ml_pipeline/lib/txt_to_json.py
- ml_pipeline/lib/bedrock_extraction.py
##### saved file is in extractions.json

---
## Utilisation

Toutes les commandes se lancent depuis la racine du projet.

### Installation (une seule fois)

    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install -r requirements.txt

### Lancer le pipeline

Les étapes se lancent dans cet ordre. Chacune utilise le résultat de la précédente.

    # 1. Liste des sociétés Masterworks
    python3 scrapers/sec_cik_scraper.py

    # 2. Liste de leurs dépôts à la SEC
    python3 scrapers/sec_filings_index.py

    # 3. Téléchargement des documents (long)
    caffeinate -i python3 scrapers/sec_documents_downloader.py

    # 4. Extraction de l'offre, avec Bedrock (long, payant)
    caffeinate -i python3 ml_pipeline/lib/offering_pipeline.py

    # 5. Extraction de la vente et de la liquidation
    python3 ml_pipeline/lib/lifecycle_pipeline.py

    # 6. Création du tableau Excel
    python3 ml_pipeline/lib/export_database_xlsx.py

Résultat : `ml_pipeline/tables/masterworks_database.xlsx`

### Options utiles

- `--limit 10` : ne traite que les 10 premières sociétés (pour tester).
- `--force` (étape 4) : retraite les sociétés déjà faites, après une modification du code.

Si une étape s'arrête en cours de route, relancez la même commande : elle reprend où elle s'était arrêtée.

### Quand relancer quoi

**Les étapes 1 à 3 (collecte) servent à récupérer les nouvelles données de la SEC.**
Relancez-les quand :
- c'est la première utilisation ;
- vous voulez mettre le tableau à jour : de nouvelles sociétés et de nouveaux rapports
  (ventes, liquidations) sont publiés régulièrement. Une fois par mois suffit, par exemple.

Le téléchargement (étape 3) n'a jamais besoin d'être forcé : il ne télécharge que les
documents qu'il n'a pas encore, et saute les autres. Le relancer est donc rapide et sans risque.

**Les étapes 4 à 6 (extraction) servent à transformer les documents en tableau.**
Relancez-les :
- après chaque collecte (étapes 1 à 3), pour traiter les nouveaux documents ;
- après une modification du code, selon le fichier modifié :

| Fichier modifié | À relancer |
|---|---|
| `offering_extraction.py`, `offering_pipeline.py` ou `extract_sections.py` | Étape 4 avec `--force`, puis 5 et 6 |
| `lifecycle_pipeline.py` | Étapes 5 et 6 |
| `export_database_xlsx.py` | Étape 6 |

**Quand utiliser `--force` (étape 4 uniquement) ?**
Seulement après une modification du code d'extraction. Sans `--force`, les sociétés déjà
traitées gardent leur ancien résultat, même si le code a changé. Avec `--force`, toutes
les sociétés sont retraitées : comptez plusieurs heures et quelques dollars d'appels à Bedrock.
Après une simple collecte de nouvelles données, `--force` est inutile : les nouveaux
documents sont traités automatiquement.


---

### use cases

## Cas d'usage

Toutes les commandes se lancent depuis la racine du projet, avec l'environnement `.venv` actif.

### 1. Recréer seulement le tableau Excel

```bash
python3 ml_pipeline/lib/export_database_xlsx.py
```

Quelques secondes, gratuit.

### 2. Après une modification de `lifecycle_pipeline.py` (ventes, liquidations)

```bash
python3 ml_pipeline/lib/lifecycle_pipeline.py
python3 ml_pipeline/lib/export_database_xlsx.py
```

Quelques minutes, gratuit. Aucun appel à Bedrock.

### 3. Après une modification de `offering_pipeline.py` (fusion des circulaires)

```bash
python3 ml_pipeline/lib/offering_pipeline.py
python3 ml_pipeline/lib/export_database_xlsx.py
```

Sans `--force` : les résultats déjà lus par Bedrock sont réutilisés. Gratuit.

### 4. Après une modification de `offering_extraction.py` ou `extract_sections.py` (lecture des documents)

Toutes les sociétés doivent être relues par Bedrock :

```bash
caffeinate -i python3 ml_pipeline/lib/offering_pipeline.py --force
python3 ml_pipeline/lib/lifecycle_pipeline.py
python3 ml_pipeline/lib/export_database_xlsx.py
```

Plusieurs heures, quelques dollars.

### 5. Corriger seulement quelques sociétés

```bash
python3 ml_pipeline/lib/offering_pipeline.py --only 009,021,026 --force
python3 ml_pipeline/lib/lifecycle_pipeline.py --only 009,021,026
python3 ml_pipeline/lib/export_database_xlsx.py
```

Numéros sur 3 chiffres, séparés par des virgules, sans espace. Les autres sociétés restent intactes dans le tableau, et seules celles de la liste sont facturées.

Pour retraiter automatiquement toutes les sociétés sans titre (ou au titre brouillé) :

```bash
LISTE=$(python3 -c "import csv,re; print(','.join(re.search(r'Masterworks (\d{3}),', r['issuer_name_edgar']).group(1) for r in csv.DictReader(open('ml_pipeline/tables/offering_table.csv')) if r['status'] == 'ok' and re.search(r'Masterworks (\d{3}),', r['issuer_name_edgar']) and (not r['title'] or re.search(r'(?:x\w){4,}', r['title']))))")
caffeinate -i python3 ml_pipeline/lib/offering_pipeline.py --only "$LISTE" --force
python3 ml_pipeline/lib/lifecycle_pipeline.py --only "$LISTE"
python3 ml_pipeline/lib/export_database_xlsx.py
```

### 6. Récupérer les nouvelles sociétés et les nouveaux rapports de la SEC

```bash
python3 scrapers/sec_cik_scraper.py
python3 scrapers/sec_filings_index.py
caffeinate -i python3 scrapers/sec_documents_downloader.py
caffeinate -i python3 ml_pipeline/lib/offering_pipeline.py
python3 ml_pipeline/lib/lifecycle_pipeline.py
python3 ml_pipeline/lib/export_database_xlsx.py
```

Sans `--force` : seules les nouveautés sont traitées. Une fois par mois suffit.