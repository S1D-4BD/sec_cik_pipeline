import json

with open("root_artists.json", "r", encoding="utf-8") as f:
    artists = json.load(f)


target_name = "Fernand Léger"
index = next((i for i, artist in enumerate(artists) if artist.get("NAME") == target_name), None)

print(f"L'index de {target_name} est : {index}")
