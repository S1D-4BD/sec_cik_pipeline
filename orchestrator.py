#!/usr/bin/env python3

import subprocess
import sys
from datetime import date

SCRIPTS = [
    "sec_cik_scraper.py",
    "sec_253G2_scrapper.py",
]

day = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
print(f" Pipeline for {day} ")

for script in SCRIPTS:
    print(f"\n Starting {script}")
    resultat = subprocess.run([sys.executable, script, day])
    if resultat.returncode != 0:
        print(f"{script} failed, stop pipeline")
        sys.exit(1)
    print(f"{script} done")

print("\nPipeline done")