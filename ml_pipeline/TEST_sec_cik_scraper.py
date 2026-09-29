#!/usr/bin/env python3

import logging
import time
import json
import os
import boto3
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "sec-filings20260901174817304000000001")
S3_CLIENT = boto3.client("s3", region_name=AWS_REGION)

def run(playwright):
    browser = playwright.chromium.launch(
    headless=True,
    args=[
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu"
    ]
)
    context = browser.new_context(user_agent="sid celina_sid@hotmail.com")
    page = context.new_page()

    all_ciks = {}
    start = 0
    count = 40

    while True:
        url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=Masterworks&owner=include&match=starts-with&start={start}&count={count}&hidefilings=0"
        page.goto(url, wait_until="domcontentloaded")

        try:
            page.wait_for_selector("table.tableFile2", timeout=5000)
        except Exception:
            logging.info("no page content, stop condition")
            break

        lignes = page.locator("table.tableFile2 tr:has(a[href*='CIK='])").all()
        if not lignes:
            logging.info("no lines found for CIK on this page")
            break

        nouveaux_ciks = 0
        for ligne in lignes:
            cik = ligne.locator("a[href*='CIK=']").inner_text().strip()
            tds = ligne.locator("td").all()
            
            if len(tds) >= 2:
                nom = tds[1].inner_text().split("\n")[0].strip()
                
                if cik.isdigit() and "masterworks" in nom.lower():
                    if cik not in all_ciks:
                        all_ciks[cik] = nom
                        nouveaux_ciks += 1

        logging.info(f"Page start={start} : {len(all_ciks)} total cik ({nouveaux_ciks} more results)")

        if nouveaux_ciks == 0:
            logging.info("end reached")
            break
        time.sleep(2)
        start += count

    logging.info(f"TOTAL FINAL : {len(all_ciks)} ciks")

    json_data = json.dumps(all_ciks, indent=4, ensure_ascii=False)
    S3_CLIENT.put_object(Bucket=BUCKET_NAME, Key="config/all_ciks.json", Body=json_data.encode("utf-8"),ContentType="application/json")
    logging.info("all_ciks.json saved to S3")

    context.close()
    browser.close()

if __name__ == "__main__":
    with sync_playwright() as playwright:
        run(playwright)
