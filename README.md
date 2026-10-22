# sec-cik-scraper README

```
python3 scrapers/sec_cik_scraper.py
python3 scrapers/sec_filings_index.py
caffeinate -i python3 scrapers/sec_documents_downloader.py
caffeinate -i python3 pipeline/lib/offering_pipeline.py
python3 pipeline/lib/lifecycle_pipeline.py
python3 pipeline/lib/vault_pipeline.py
python3 pipeline/lib/export_database_xlsx.py
```

```
python3 -c "import csv; 
r=list(csv.DictReader(open('data/tables/filings.csv'))); 
w=csv.DictWriter(open('data/tables/filings_test.csv','w',newline=''), fieldnames=r[0].keys()); 
w.writeheader(); 
w.writerows(x for x in r if x['company_name']=='XXNAMEHEREXX')"
```
---

## Structure

```
sec-scraper-aws/
├── scrapers/
│   ├── sec_cik_scraper.py          # scrapes the list of Masterworks LLCs
│   ├── sec_filings_index.py        # for each LLC, lists its filings on EDGAR
│   └── sec_documents_downloader.py # downloads the filings' documents to AWS S3
│                                   # (offering statements 1-A only for the vaults)
│
├── pipeline/lib/                   # Data extraction
│   ├── extract_sections.py         # splits an offering circular into sections
│   ├── offering_extraction.py      # LLM data extraction, with evidence checks
│   ├── offering_pipeline.py        # offering: pre-sale data, from the offering circulars
│   ├── lifecycle_pipeline.py       # lifecycle: post-sale data, from the reports (no LLM)
│   ├── vault_pipeline.py           # vaults: 1 row per series + Masterworks sales history (no LLM)
│   └── export_database_xlsx.py     # Excel export (LLCs + vault series)
│
├── data/
│   └── tables/                     # data files, ignored by Git:
│                                   # filings.csv, documents.csv, offering_table.csv, lifecycle_table.csv, vault_table.csv, exits_table.csv, masterworks_database.xlsx
│
└── infrastructure/terraform/       # S3 bucket
```

---

## note

All commands are run from the project root 

### installation (once)

    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install -r requirements.txt

### running the pipeline

Run the steps in this order. Each step uses the output of the previous one

    #### List of Masterworks LLCs
    python3 scrapers/sec_cik_scraper.py

    #### List of their SEC filings
    python3 scrapers/sec_filings_index.py

    #### Document download (long)
    caffeinate -i python3 scrapers/sec_documents_downloader.py

    #### Offering extraction, with Bedrock (long, paid)
    caffeinate -i python3 pipeline/lib/offering_pipeline.py

    #### Sale and liquidation extraction
    python3 pipeline/lib/lifecycle_pipeline.py

    #### Vault series and Masterworks' sales history (no LLM)
    python3 pipeline/lib/vault_pipeline.py

    #### Excel export
    python3 pipeline/lib/export_database_xlsx.py

Output: `data/tables/masterworks_database.xlsx`

`caffeinate -i` (macOS) prevents the computer from sleeping during long steps: the Mac must
stay open and plugged in

### great options

- `--limit 10`: processes only the first 10 LLCs (for testing)
- `--only 009,021`: processes only these LLCs (LLC numbers, names or CIKs). The rest of the table is kept
- `--force` (step 4): reprocesses LLCs that were already processed, after a code change
  With `--only`, only the selected LLCs are reprocessed
- `--only "Vault 5,Vault 12"` (step 6): processes only these vaults. The other vaults' rows are kept

If a step stops midway, run the same command again: it resumes where it stopped

### when to rerun what

**Steps 1 to 3 (collection) fetch new data from the SEC.**
Rerun them when:
- it is the first run;
- you want to update the table: new LLCs and new reports (sales, liquidations) are published
  regularly. Once a month is enough, for example.

The download (step 3) never needs to be forced: it only downloads the documents it does not
have yet, and skips the others. Rerunning it is fast and safe.

**Steps 4 to 7 (extraction) turn the documents into the table.**
Rerun them:
- after each collection (steps 1 to 3), to process the new documents;
- after a code change, depending on the modified file:

| Modified file | Rerun |
|---|---|
| `offering_extraction.py` or `extract_sections.py` | Step 4 with `--force`, then 5, 6 and 7 |
| `offering_pipeline.py` | Step 4 (without `--force`), then 7 |
| `lifecycle_pipeline.py` | Steps 5 and 7 |
| `vault_pipeline.py` | Steps 6 and 7 |
| `export_database_xlsx.py` | Step 7 |

**When to use `--force` (for step 4 only)?**
Only after changing the code that reads the documents. Without `--force`, LLCs that were already
processed keep their previous result, even if the code has changed. With `--force`, all LLCs are
reprocessed: expect several hours and a few dollars of Bedrock calls.
After a simple collection of new data, `--force` is not needed: new documents are processed
automatically

---

### Rebuild only the Excel table

```bash
python3 pipeline/lib/export_database_xlsx.py
```

### After changing `lifecycle_pipeline.py` (sales, liquidations)

```bash
python3 pipeline/lib/lifecycle_pipeline.py
python3 pipeline/lib/export_database_xlsx.py
```


### After changing `vault_pipeline.py` (vault series)

```bash
python3 pipeline/lib/vault_pipeline.py
python3 pipeline/lib/export_database_xlsx.py
```

### After changing `offering_pipeline.py` (merging of offering circulars)

```bash
python3 pipeline/lib/offering_pipeline.py
python3 pipeline/lib/export_database_xlsx.py
```

Without `--force`: results already read by Bedrock are reused

### After changing `offering_extraction.py` or `extract_sections.py` (document reading)

All LLCs must be read again by Bedrock

```bash
caffeinate -i python3 pipeline/lib/offering_pipeline.py --force
python3 pipeline/lib/lifecycle_pipeline.py
python3 pipeline/lib/vault_pipeline.py
python3 pipeline/lib/export_database_xlsx.py
```


### Fix only a few LLCs

```bash
python3 pipeline/lib/offering_pipeline.py --only 009,021,026 --force
python3 pipeline/lib/lifecycle_pipeline.py --only 009,021,026
python3 pipeline/lib/export_database_xlsx.py
```


### Fetch new LLCs, new vault series and new SEC reports

```bash
python3 scrapers/sec_cik_scraper.py
python3 scrapers/sec_filings_index.py
caffeinate -i python3 scrapers/sec_documents_downloader.py
caffeinate -i python3 pipeline/lib/offering_pipeline.py
python3 pipeline/lib/lifecycle_pipeline.py
python3 pipeline/lib/vault_pipeline.py
python3 pipeline/lib/export_database_xlsx.py
```

Without `--force`: only new data is processed. Once a month is enough.