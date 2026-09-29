# syntax=docker/dockerfile:1
# =============================================================================
#  sec-pipeline — image Lambda ARM64, HTTP pur (plus de navigateur)
# =============================================================================
#  Build :
#    docker buildx build --platform linux/arm64 --provenance=false \
#      -t sec-pipeline:latest --load .
# =============================================================================

FROM public.ecr.aws/lambda/python:3.12

# Sur Lambda, seul /tmp est inscriptible.
ENV HOME=/tmp \
    TMPDIR=/tmp \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR ${LAMBDA_TASK_ROOT}

# Couche mise en cache : ne se reconstruit que si requirements.txt change.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Couche volatile : le code applicatif, copie en dernier.
COPY scrapers/sec_cik_scraper.py \
     scrapers/sec_253G2_scrapper.py \
     scrapers/handler.py \
     ./

# L'image AWS fournit deja son runtime interface client : pas d'ENTRYPOINT
# a declarer, seulement le point d'entree de la fonction.
CMD ["handler.lambda_handler"]