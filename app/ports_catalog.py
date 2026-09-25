"""
Catalogue de ports proposés par défaut dans l'application, avec leurs
coordonnées géographiques (nécessaires à pyTMD et au calcul du crépuscule
nautique). Contrairement à api-maree.fr, on n'est pas limité à ces noms :
`precompute.py --lat --lon --name` permet d'ajouter n'importe quel point.

Coordonnées approximatives (sortie de port / mouillage habituel), à
ajuster si besoin pour coller précisément à un spot de plongée donné.

offset_zh_m
-----------
Hauteur du niveau moyen (NM) au-dessus du zéro hydrographique (zéro des
cartes marines), en mètres. Elle est ajoutée aux hauteurs du modèle FES
(exprimées par rapport au niveau moyen) pour obtenir des hauteurs comme
dans l'annuaire SHOM.

Source officielle : colonne « NM » des Références Altimétriques Maritimes
(RAM) du Shom, sur data.shom.fr ou diffusion.shom.fr.

Tant qu'une valeur vaut 0, precompute.py refuse de traiter ce port
(sauf si --offset-zh est passé en ligne de commande).
"""

PORTS = [
    {"name": "Binic", "latitude": 48.6017, "longitude": -2.8244, "offset_zh_m": 6.68},
    {"name": "Saint-Quay-Portrieux", "latitude": 48.6497, "longitude": -2.8419, "offset_zh_m": 6.57},
    {"name": "Erquy", "latitude": 48.6386, "longitude": -2.4667, "offset_zh_m": 6.62},
    {"name": "Dahouet", "latitude": 48.5875, "longitude": -2.5875, "offset_zh_m": 0},
    {"name": "Paimpol", "latitude": 48.7822, "longitude": -3.0367, "offset_zh_m": 6.25},
    {"name": "Saint-Cast-le-Guildo", "latitude": 48.6386, "longitude": -2.2494, "offset_zh_m": 0},
    {"name": "Saint-Malo", "latitude": 48.6497, "longitude": -2.0256, "offset_zh_m": 0},
    {"name": "Perros-Guirec", "latitude": 48.8117, "longitude": -3.4425, "offset_zh_m": 0},
    {"name": "Brest", "latitude": 48.3833, "longitude": -4.4931, "offset_zh_m": 4.32},
    {"name": "Camaret-sur-Mer", "latitude": 48.2725, "longitude": -4.5936, "offset_zh_m": 0},
    {"name": "Douarnenez", "latitude": 48.0928, "longitude": -4.3286, "offset_zh_m": 0},
    {"name": "Concarneau", "latitude": 47.8742, "longitude": -3.9147, "offset_zh_m": 0},
    {"name": "Lorient", "latitude": 47.7481, "longitude": -3.3661, "offset_zh_m": 0},
    {"name": "La Trinité-sur-Mer", "latitude": 47.5808, "longitude": -3.0169, "offset_zh_m": 0},
    {"name": "Quiberon (Port-Maria)", "latitude": 47.4808, "longitude": -3.1197, "offset_zh_m": 0},
]