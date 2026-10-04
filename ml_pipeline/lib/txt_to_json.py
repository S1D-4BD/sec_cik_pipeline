#!/usr/bin/env python3

import json
import os

with open("lib/section.txt","r", encoding="utf-8") as f:
    text= f.read()
data=[
    {
        "text": text
    }
]

with open("lib/section.json", "w", encoding="utf-8") as f:
    json.dump(data,f,ensure_ascii=False, indent=2)