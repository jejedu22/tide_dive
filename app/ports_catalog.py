"""
Catalogue de ports proposés par défaut dans l'application, avec leurs
coordonnées géographiques (nécessaires à pyTMD et au calcul du crépuscule
nautique). Contrairement à api-maree.fr, on n'est pas limité à ces noms :
`precompute.py --lat --lon --name` permet d'ajouter n'importe quel point.

Coordonnées approximatives (sortie de port / mouillage habituel), à
ajuster si besoin pour coller précisément à un spot de plongée donné.
"""

PORTS = [
    {"name": "Binic", "latitude": 48.6017, "longitude": -2.8244},
    {"name": "Saint-Quay-Portrieux", "latitude": 48.6497, "longitude": -2.8419},
    {"name": "Erquy", "latitude": 48.6386, "longitude": -2.4667},
    {"name": "Dahouet", "latitude": 48.5875, "longitude": -2.5875},
    {"name": "Paimpol", "latitude": 48.7822, "longitude": -3.0367},
    {"name": "Saint-Cast-le-Guildo", "latitude": 48.6386, "longitude": -2.2494},
    {"name": "Saint-Malo", "latitude": 48.6497, "longitude": -2.0256},
    {"name": "Perros-Guirec", "latitude": 48.8117, "longitude": -3.4425},
    {"name": "Brest", "latitude": 48.3833, "longitude": -4.4931},
    {"name": "Camaret-sur-Mer", "latitude": 48.2725, "longitude": -4.5936},
    {"name": "Douarnenez", "latitude": 48.0928, "longitude": -4.3286},
    {"name": "Concarneau", "latitude": 47.8742, "longitude": -3.9147},
    {"name": "Lorient", "latitude": 47.7481, "longitude": -3.3661},
    {"name": "La Trinité-sur-Mer", "latitude": 47.5808, "longitude": -3.0169},
    {"name": "Quiberon (Port-Maria)", "latitude": 47.4808, "longitude": -3.1197},
]
