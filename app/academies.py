"""
Académies (vacances scolaires) : liste de celles de l'open data de l'Éducation nationale (jeu
« fr-en-calendrier-scolaire », champ « location ») et académie d'un port de la Manche ou de l'Atlantique déduite de sa
position (academy_for), sans réseau : marche aussi pour un point en mer (bouée, île) où aucune commune ne répond.

Découpage du littoral (les limites suivent les estuaires) : Lille (Nord, Pas-de-Calais) jusqu'à l'Authie ; Amiens
(Somme) jusqu'à la Bresle ; Normandie jusqu'au Couesnon (îles Chausey comprises) ; Rennes (Bretagne) jusqu'à la
Vilaine (Houat, Hoëdic, Belle-Île comprises) ; Nantes (Loire-Atlantique, Vendée) jusqu'à la baie de l'Aiguillon ;
Poitiers (Charente-Maritime, rive droite de la Gironde) ; Bordeaux (Gironde, Landes, Pyrénées-Atlantiques).
Ailleurs (Méditerranée, outre-mer) : None, l'académie se choisit à la main dans l'onglet Ports.
"""

from __future__ import annotations

# académies de métropole, telles que les écrit l'open data (champ « location »)
ACADEMIES = (
    "Aix-Marseille", "Amiens", "Besançon", "Bordeaux", "Clermont-Ferrand", "Corse", "Créteil", "Dijon", "Grenoble",
    "Lille", "Limoges", "Lyon", "Montpellier", "Nancy-Metz", "Nantes", "Nice", "Normandie", "Orléans-Tours", "Paris",
    "Poitiers", "Reims", "Rennes", "Strasbourg", "Toulouse", "Versailles",
)


# axe approché de l'estuaire de la Gironde (latitude, longitude), de l'embouchure vers l'amont
_GIRONDE_AXIS = ((45.585, -1.06), (45.45, -0.87), (45.33, -0.74))


def _gironde_right_bank(lat: float, lon: float) -> bool:
    """Rive droite (nord-est) de l'estuaire de la Gironde, en Charente-Maritime (Royan, Meschers) ? La rive gauche
    (Médoc) et l'amont de 45,33° (Blaye, Bourg) sont en Gironde."""
    if lat < _GIRONDE_AXIS[-1][0]:
        return False
    if lat >= _GIRONDE_AXIS[0][0]:
        return lon > _GIRONDE_AXIS[0][1]
    for (lat1, lon1), (lat2, lon2) in zip(_GIRONDE_AXIS, _GIRONDE_AXIS[1:]):
        if lat2 <= lat <= lat1:
            axis_lon = lon2 + (lon1 - lon2) * (lat - lat2) / (lat1 - lat2)
            return lon > axis_lon
    return False


def academy_for(latitude: float, longitude: float) -> str | None:
    """Académie d'un point de la côte Manche-Atlantique (ou de ses îles), None ailleurs."""
    lat, lon = latitude, longitude
    if not (43.25 <= lat <= 51.4 and -5.6 <= lon <= 2.7):
        return None
    if lon > -1.0 and lat < 47.0 and lon > 0.0:    # intérieur des terres au sud : hors littoral atlantique
        return None
    # Manche est : de la frontière belge à la baie du Mont-Saint-Michel
    if lat >= 50.37:                               # au nord de l'Authie (bouées du large comprises)
        return "Lille"
    if lat >= 50.05 and lon >= 1.385:
        return "Amiens"
    if lat >= 48.6 and (lon >= -1.525 or (lat >= 48.8 and lon >= -2.0)):
        return "Normandie"
    # Bretagne : au nord de la Vilaine (Houat, Hoëdic et Belle-Île plus au sud, mais dans le Morbihan)
    if lat >= 47.45 or (lat >= 47.25 and lon < -2.6):
        return "Rennes"
    # Pays de la Loire : jusqu'à la baie de l'Aiguillon (Sèvre Niortaise, 46,31°) ; la pointe de l'Aiguillon, flèche
    # de sable vendéenne, descend plus au sud que Charron et Esnandes (Charente-Maritime, à l'est de −1,15°) mais pas
    # jusqu'à l'île de Ré (à l'ouest de −1,40°)
    if lat >= 46.31 or (lat >= 46.25 and -1.40 < lon < -1.15):
        return "Nantes"
    # Charente-Maritime, jusqu'à la rive droite de la Gironde
    if lat >= 45.62 or _gironde_right_bank(lat, lon):
        return "Poitiers"
    return "Bordeaux"
