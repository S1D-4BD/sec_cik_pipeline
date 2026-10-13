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