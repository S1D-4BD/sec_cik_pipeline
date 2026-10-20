# sec-scraper-aws

Pipeline that rebuilds the Masterworks dataset (one LLC per artwork) from the documents published
by the SEC on EDGAR, in the format of the dashboard's MastwerworksDB sheet


---

## Structure

```
sec-scraper-aws/
├── scrapers/
│   ├── sec_cik_scraper.py          # scrapes the list of Masterworks LLCs
│   ├── sec_filings_index.py        # for each LLC, lists its filings on EDGAR
│   └── sec_documents_downloader.py # downloads the filings' documents to AWS S3
│
├── pipeline/lib/                   # Data extraction
│   ├── extract_sections.py         # Script that splits an offering circular into sections
│   ├── offering_extraction.py      # Script that runs LLM data extraction
│   ├── offering_pipeline.py        # Script offering: pre-sale data, from the offering circulars
│   ├── lifecycle_pipeline.py       # Script lifecycle: post-sale data, from the reports (no LLM)
│   └── export_database_xlsx.py     # Script for Excel export
│
├── data/                           # local data, not versioned (the reference copy lives in S3)
│   ├── all_ciks.json               # list of Masterworks LLCs (copy of S3 config/all_ciks.json)
│   ├── formulaires_253G2/          # 253G2 offering circulars (.htm)
│   ├── documents/                  # local copy of the downloaded filings (downloader --local)
│   └── tables/                     # filings.csv, documents.csv, offering_table.csv,
│                                   # lifecycle_table.csv, masterworks_database.xlsx
│
└── infrastructure/terraform/       # S3 bucket
```


## usage

ALL commands are run from the project root

### installation (once)

    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install -r requirements.txt

### running the pipeline

run the steps in this order, each step uses the output of the previous one.

    # 1 List of Masterworks LLCs
    python3 scrapers/sec_cik_scraper.py

    # 2 List of their SEC filings
    python3 scrapers/sec_filings_index.py

    # 3 Document download (long)
    caffeinate -i python3 scrapers/sec_documents_downloader.py

    # 4 Offering extraction, with Bedrock (long, paid)
    caffeinate -i python3 pipeline/lib/offering_pipeline.py

    # 5 Sale and liquidation extraction
    python3 pipeline/lib/lifecycle_pipeline.py

    # 6 Excel export
    python3 pipeline/lib/export_database_xlsx.py

Output: `data/tables/masterworks_database.xlsx`

`caffeinate -i` prevents the computer from sleeping during long steps if the LLC's treated are more than 100

### options

- `--limit 10`: processes only the first 10 LLCs 
- `--only 009,021`: processes only these LLCs (rest is unchanged)
- `--force` (for step 4): reprocesses LLCs that were already processed, after a code change, with `--only`, only the selected LLCs are reprocessed.

If a step stops midway, run the same command again since it resumes where it stopped

### when to rerun what

**Steps 1 to 3 == data-collection : fetch new data from the SEC**

Rerun them when:
- it is the first run;
- need to update the table: new LLCs and new reports (sales, liquidations) are published

The download (step 3) never needs to be forced: it only downloads the documents it does not
have yet, and skips the other

**Steps 4 to 6 (extraction) turn the documents into the table**

Rerun them:
- after each collection (steps 1 to 3), to process the new documents
- after a code change, depending on the modified file:

| modified file | what to rerun |
|---|---|
| `offering_extraction.py` or `extract_sections.py` | Step 4 with `--force`, then 5 and 6 |
| `offering_pipeline.py` | Step 4 (without `--force`), then 6 |
| `lifecycle_pipeline.py` | Steps 5 and 6 |
| `export_database_xlsx.py` | Step 6 |


Without `--force`, LLCs that were already
processed keep their previous result, even if the code has changed. With `--force`, all LLCs are
reprocessed, might take several hours

---

## Use cases

All commands are run from the project root, with the `.venv` environment active

### 1 Rebuild only the Excel table

```bash
python3 pipeline/lib/export_database_xlsx.py
```

### 2 After changing `lifecycle_pipeline.py` (sales, liquidations)

```bash
python3 pipeline/lib/lifecycle_pipeline.py
python3 pipeline/lib/export_database_xlsx.py
```

### 3 After changing `offering_pipeline.py` (merging of offering circulars)

```bash
python3 pipeline/lib/offering_pipeline.py
python3 pipeline/lib/export_database_xlsx.py
```


### 4 After changing `offering_extraction.py` or `extract_sections.py` (document reading)


```bash
caffeinate -i python3 pipeline/lib/offering_pipeline.py --force
python3 pipeline/lib/lifecycle_pipeline.py
python3 pipeline/lib/export_database_xlsx.py
```


### 5 Fix only a few LLCs

```bash
python3 pipeline/lib/offering_pipeline.py --only 009,021,026 --force
python3 pipeline/lib/lifecycle_pipeline.py --only 009,021,026
python3 pipeline/lib/export_database_xlsx.py
```


### 6 Fetch new LLCs and new SEC reports

```bash
python3 scrapers/sec_cik_scraper.py
python3 scrapers/sec_filings_index.py
caffeinate -i python3 scrapers/sec_documents_downloader.py
caffeinate -i python3 pipeline/lib/offering_pipeline.py
python3 pipeline/lib/lifecycle_pipeline.py
python3 pipeline/lib/export_database_xlsx.py
```

