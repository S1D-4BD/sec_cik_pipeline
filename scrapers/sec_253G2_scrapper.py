#!/usr/bin/env python3

import json
from botocore.exceptions import ClientError
from pathlib import Path
import time
import os
import warnings
import logging
import requests
import boto3

#### login aws ########################################
AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "sec-filings20260901174817304000000001")
S3_CLIENT = boto3.client("s3", region_name=AWS_REGION)
#######################################################

#### local #########################
LOCAL_DIR = Path(__file__).parent / "formulaires_253G2"
LOCAL_DIR.mkdir(parents=True, exist_ok=True) 
####################################
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
warnings.filterwarnings("ignore", category=UserWarning, module="urllib3") ### makes console easy to read, no warnings

def main():

    headers = {"User-Agent": "scraper scraper@scraper.com"}

    try:
        response = S3_CLIENT.get_object(Bucket=BUCKET_NAME, Key="config/all_ciks.json")
        ciks_dict = json.loads(response['Body'].read().decode('utf-8'))
        logging.info("Successfully loaded all_ciks.json from S3")
    except Exception as e:
        logging.error(f"Error loading all_ciks.json from S3: {e}")
        #exit(1)  
        raise


    # pour chaque tuple cik-name dans le dict, on va ajouter des 0 pour faire un cik de longueur 10, et batir l'url avec le cik formatté 
    for cik, name in ciks_dict.items():
        formatted_cik = str(cik).zfill(10) 
        logging.info(f"searching for: {name} (CIK: {formatted_cik})")
        url_api = f"https://efts.sec.gov/LATEST/search-index?ciks={formatted_cik}&forms=253G2" 

        try:
            res = requests.get(url_api, headers=headers) # 
            time.sleep(0.1)
            if res.status_code != 200: # si fail, passer au suivant
                logging.warning(f"failed to fetch data for CIK {formatted_cik}")
                continue

            hits = res.json().get("hits", {}).get("hits", []) # car hits est un array de dicts de forme [ {…}, {…} ], ou chaque dict est un 253g2 a download 

            if not hits:
                logging.info("no 253G2 found")
                continue

            latest_hit = hits[0] # premier dict de l'array de hits 
            doc = latest_hit["_source"] # le 2iem dict, dedans la data "_source" contient le adsh

            file_name = doc.get("file_name") or latest_hit["_id"].split(":")[-1] # id est de forme "adsh:form253g2.html", donc on peut l'utiliser si file_name pas la
            cik_clean = formatted_cik.lstrip("0") # enlever les 0 car EDGAR ne les utilise pas dans l'url de l'api de download
            accession_clean = doc["adsh"].replace("-", "") # cleaning "-"

            save_name = f"{formatted_cik}_{doc['adsh']}_{file_name}"
            s3_key = f"formulaires_253G2/{save_name}"

            """ 
            Verifier si on a deja 
            download le fichier dans s3, dans local, ou les 2
            """
            local_path= LOCAL_DIR / save_name
            try:
                S3_CLIENT.head_object(Bucket=BUCKET_NAME, Key=s3_key)
                in_s3 = True
            except ClientError:
                in_s3 = False
            in_local = local_path.exists()

            # cas 1 : deja partout, rien a faire
            if in_s3 and in_local:
                logging.info(f"file already exists in s3 and locally: {save_name}")
                continue

            # cas 2 : dans s3 mais pas en local -> on copie depuis s3
            if in_s3 and not in_local:
                S3_CLIENT.download_file(BUCKET_NAME, s3_key, str(local_path))
                logging.info(f"downloaded from s3 to local: {local_path}")
                continue

            # cas 3 : pas dans s3 -> on telecharge depuis la SEC
            url_doc = f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{accession_clean}/{file_name}"
            file_res = requests.get(url_doc, headers=headers)

            if file_res.status_code == 200:
                # sauvegarde locale
                local_path.write_bytes(file_res.content)
                logging.info(f"saved latest file locally: {local_path}")

                # sauvegarde s3
                S3_CLIENT.put_object(Bucket=BUCKET_NAME,
                    Key=s3_key,
                    Body=file_res.content)
                logging.info(f"saved latest file in s3: {s3_key}")            
                time.sleep(0.1)

        except Exception as e:
            logging.error(f"error for CIK {formatted_cik}: {e}")

if __name__ == "__main__":
    main()