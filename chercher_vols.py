#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Veille de prix de billets d'avion a dates flexibles - version Google Flights.

Interroge Google Flights via la bibliotheque faster-flights pour chaque
combinaison (date de depart, date de retour) definie dans config.yaml,
puis ecrit un tableau dans RESULTATS.md et une ligne d'historique dans
historique.csv.

Ne fait aucune reservation et ne demande aucun moyen de paiement.
"""

import csv
import datetime as dt
import re
import sys
import time
from pathlib import Path

import yaml

# Le paquet s'appelle "faster-flights" (fork) mais a longtemps expose le
# module "fast_flights". On essaie les deux plutot que de parier.
# La bibliotheque a change d'interface entre ses versions 2 et 3.
# On charge le module, puis on regarde ce qu'il expose reellement.
_module = None
for _nom in ("fast_flights", "faster_flights"):
    try:
        _module = __import__(_nom)
        BIBLIOTHEQUE = _nom
        break
    except ImportError:
        continue

if _module is None:
    import pkgutil
    candidats = sorted(m.name for m in pkgutil.iter_modules()
                       if "flight" in m.name.lower())
    print("ERREUR : bibliotheque de recherche introuvable.")
    print(f"Modules contenant 'flight' : {candidats or 'aucun'}")
    sys.exit(1)

Passengers = getattr(_module, "Passengers", None)
get_flights = getattr(_module, "get_flights", None)
FlightQuery = getattr(_module, "FlightQuery", None)
create_query = getattr(_module, "create_query", None)
FlightData = getattr(_module, "FlightData", None)

# Version 3 : create_query + FlightQuery. Version 2 : get_flights(flight_data=...)
API_MODERNE = FlightQuery is not None and create_query is not None

if get_flights is None or Passengers is None or (
        not API_MODERNE and FlightData is None):
    expose = [n for n in dir(_module) if not n.startswith("_")]
    print(f"ERREUR : le module {BIBLIOTHEQUE} n'expose pas les fonctions attendues.")
    print(f"Il contient : {expose}")
    print("Envoie cette liste pour qu'on adapte le code.")
    sys.exit(1)


RACINE = Path(__file__).resolve().parent
FICHIER_RESULTATS = RACINE / "RESULTATS.md"
FICHIER_HISTORIQUE = RACINE / "historique.csv"


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

def en_date(valeur):
    if isinstance(valeur, dt.date):
        return valeur
    return dt.date.fromisoformat(str(valeur).strip())


def charger_config():
    chemin = RACINE / "config.yaml"
    if not chemin.exists():
        sys.exit("ERREUR : le fichier config.yaml est introuvable.")

    with open(chemin, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    cfg["origine"] = str(cfg["origine"]).upper().strip()
    cfg["destination"] = str(cfg["destination"]).upper().strip()
    cfg["depart_le_plus_tot"] = en_date(cfg["depart_le_plus_tot"])
    cfg["depart_le_plus_tard"] = en_date(cfg["depart_le_plus_tard"])
    cfg.setdefault("durees_jours", [30, 60])
    cfg.setdefault("tolerance_jours", 0)
    cfg.setdefault("max_requetes", 60)
    cfg.setdefault("pause_secondes", 3)
    cfg.setdefault("vols_directs_uniquement", False)
    cfg.setdefault("nombre_de_resultats", 20)
    cfg.setdefault("exclure_low_cost", True)
    cfg.setdefault("compagnies_low_cost", [])

    cfg["compagnies_low_cost"] = [
        str(c).lower().strip() for c in (cfg["compagnies_low_cost"] or [])
    ]

    if cfg["depart_le_plus_tard"] < cfg["depart_le_plus_tot"]:
        sys.exit("ERREUR : depart_le_plus_tard est avant depart_le_plus_tot.")

    for code in (cfg["origine"], cfg["destination"]):
        if len(code) != 3:
            sys.exit(f"ERREUR : '{code}' n'est pas un code d'aeroport a 3 lettres.")

    return cfg


def paires_de_dates(cfg):
    """Couples (depart, retour) a interroger, plafonnes par max_requetes."""
    couples = []
    jour = cfg["depart_le_plus_tot"]
    while jour <= cfg["depart_le_plus_tard"]:
        for duree in cfg["durees_jours"]:
            for ecart in range(-cfg["tolerance_jours"], cfg["tolerance_jours"] + 1):
                if duree + ecart > 0:
                    couples.append((jour, jour + dt.timedelta(days=duree + ecart)))
        jour += dt.timedelta(days=1)

    plafond = cfg["max_requetes"]
    if len(couples) > plafond:
        pas = len(couples) / plafond
        echantillon = [couples[int(i * pas)] for i in range(plafond)]
        print(f"  ({len(couples)} combinaisons ramenees a {len(echantillon)} "
              f"pour ne pas se faire bloquer par Google)")
        return echantillon

    return couples


# ------------------------------------------------------------------
# Interrogation
# ------------------------------------------------------------------

def prix_en_nombre(valeur):
    """'1 034 EUR', 'EUR1,034.50' ou 878.0 -> nombre."""
    if valeur is None:
        return None
    if isinstance(valeur, (int, float)):
        return float(valeur)

    txt = re.sub(r"[^\d.,]", "", str(valeur))
    if not txt:
        return None

    if "," in txt and "." in txt:
        # le dernier separateur rencontre est le separateur decimal
        if txt.rfind(",") > txt.rfind("."):
            txt = txt.replace(".", "").replace(",", ".")
        else:
            txt = txt.replace(",", "")
    elif "," in txt:
        txt = (txt.replace(",", ".") if len(txt.split(",")[-1]) in (1, 2)
               else txt.replace(",", ""))
    elif "." in txt:
        if len(txt.split(".")[-1]) not in (1, 2):
            txt = txt.replace(".", "")

    try:
        return float(txt)
    except ValueError:
        return None


def texte(objet, *noms):
    """Recupere le premier attribut existant, la bibliotheque ayant evolue."""
    for nom in noms:
        valeur = getattr(objet, nom, None)
        if valeur not in (None, ""):
            return valeur
    return None


_PREMIER_VOL_INSPECTE = False


def inspecter(vol):
    """Affiche une seule fois les attributs reels d'un vol, pour diagnostic."""
    global _PREMIER_VOL_INSPECTE
    if _PREMIER_VOL_INSPECTE:
        return
    _PREMIER_VOL_INSPECTE = True
    champs = [n for n in dir(vol) if not n.startswith("_")]
    print(f"  (structure d'un vol : {champs})")


def compagnie_de(vol):
    valeur = texte(vol, "airlines", "name", "airline", "carrier")
    if isinstance(valeur, (list, tuple, set)):
        return ", ".join(str(v) for v in valeur if v) or "?"
    return str(valeur) if valeur else "?"


def interroger(cfg, depart, retour):
    """Renvoie la liste des vols trouves pour ce couple de dates."""
    trajets = [
        (depart, cfg["origine"], cfg["destination"]),
        (retour, cfg["destination"], cfg["origine"]),
    ]

    if API_MODERNE:
        requete = create_query(
            flights=[
                FlightQuery(date=d.isoformat(), from_airport=a, to_airport=b)
                for d, a, b in trajets
            ],
            trip="round-trip",
            seat="economy",
            passengers=Passengers(adults=1),
            language="fr-FR",
            currency=cfg.get("devise", "EUR"),
        )
        vols = list(get_flights(requete) or [])
    else:
        resultat = get_flights(
            flight_data=[
                FlightData(date=d.isoformat(), from_airport=a, to_airport=b)
                for d, a, b in trajets
            ],
            trip="round-trip",
            seat="economy",
            passengers=Passengers(
                adults=1, children=0, infants_in_seat=0, infants_on_lap=0
            ),
            fetch_mode="fallback",
        )
        vols = list(getattr(resultat, "flights", None) or [])

    sorties = []
    for vol in vols:
        inspecter(vol)
        prix = prix_en_nombre(texte(vol, "price"))
        if not prix:
            continue

        escales = texte(vol, "stops")
        if cfg["vols_directs_uniquement"] and escales not in (0, "0", None):
            continue

        sorties.append({
            "depart": depart,
            "retour": retour,
            "duree": (retour - depart).days,
            "prix": prix,
            "compagnie": compagnie_de(vol),
            "escales": escales if escales is not None else "?",
            "horaire": str(texte(vol, "departure") or ""),
        })

    return sorties


def est_low_cost(compagnie, cfg):
    minuscule = compagnie.lower()
    return any(bas in minuscule for bas in cfg["compagnies_low_cost"])


def filtrer(brutes, cfg):
    gardees = {}
    ecartees = 0

    for offre in brutes:
        if cfg["exclure_low_cost"] and est_low_cost(offre["compagnie"], cfg):
            ecartees += 1
            continue

        cle = (offre["depart"], offre["retour"],
               round(offre["prix"]), offre["compagnie"])
        gardees.setdefault(cle, offre)

    if ecartees:
        print(f"  ({ecartees} offres ecartees : compagnie sans bagage inclus)")

    return sorted(gardees.values(), key=lambda o: o["prix"])


# ------------------------------------------------------------------
# Sorties
# ------------------------------------------------------------------

def lien_google(cfg, depart, retour):
    return (
        "https://www.google.com/travel/flights?q="
        f"Flights%20from%20{cfg['origine']}%20to%20{cfg['destination']}%20"
        f"on%20{depart.isoformat()}%20through%20{retour.isoformat()}"
    )


def ecrire_resultats(resultats, cfg, horodatage, testees):
    lignes = []
    lignes.append(f"# Prix les moins chers : {cfg['origine']} vers {cfg['destination']}")
    lignes.append("")
    lignes.append(f"*Mis a jour le {horodatage:%d/%m/%Y a %Hh%M} (UTC). "
                  f"Source : Google Flights, {testees} combinaisons testees.*")
    lignes.append("")
    lignes.append(
        f"Depart entre le **{cfg['depart_le_plus_tot']:%d/%m/%Y}** et le "
        f"**{cfg['depart_le_plus_tard']:%d/%m/%Y}**, sejour de "
        + " ou ".join(f"{d} jours" for d in cfg["durees_jours"])
        + f" (tolerance {cfg['tolerance_jours']} jours)."
    )
    lignes.append("")
    if cfg["exclure_low_cost"]:
        lignes.append("Compagnies low-cost ecartees : ces resultats visent "
                      "des billets avec bagage en soute.")
    else:
        lignes.append("Toutes les compagnies sont incluses, "
                      "bagage en soute non garanti.")
    lignes.append("")

    if not resultats:
        lignes.append("## Aucun resultat")
        lignes.append("")
        lignes.append(
            "Google n'a rien renvoye. Deux causes possibles : les codes "
            "d'aeroport de `config.yaml` sont errones, ou Google a bloque "
            "les requetes venant de GitHub. Le journal de l'onglet Actions "
            "precise laquelle."
        )
    else:
        meilleur = resultats[0]
        lignes.append("## Le moins cher en ce moment")
        lignes.append("")
        lignes.append(
            f"**{meilleur['prix']:.0f}** - depart le {meilleur['depart']:%d/%m/%Y}, "
            f"retour le {meilleur['retour']:%d/%m/%Y} ({meilleur['duree']} jours), "
            f"{meilleur['compagnie']}"
        )
        lignes.append("")
        lignes.append("## Les autres options")
        lignes.append("")
        lignes.append("| Prix | Depart | Retour | Duree | Compagnie | Escales | Voir |")
        lignes.append("|---|---|---|---|---|---|---|")
        for offre in resultats[: cfg["nombre_de_resultats"]]:
            lien = lien_google(cfg, offre["depart"], offre["retour"])
            lignes.append(
                f"| {offre['prix']:.0f} "
                f"| {offre['depart']:%d/%m} "
                f"| {offre['retour']:%d/%m} "
                f"| {offre['duree']} j "
                f"| {offre['compagnie']} "
                f"| {offre['escales']} "
                f"| [ouvrir]({lien}) |"
            )

    lignes.append("")
    lignes.append("---")
    lignes.append("")
    lignes.append(
        "Prix releves sur Google Flights au moment du passage. Ils bougent en "
        "permanence : verifie le tarif reel avant de reserver. L'historique "
        "est dans `historique.csv`."
    )
    lignes.append("")
    lignes.append(
        "Le filtre bagage porte sur la compagnie, pas sur le billet. Meme sur "
        "une grande compagnie, un tarif *basic* ou *light* peut exclure la soute."
    )
    lignes.append("")

    FICHIER_RESULTATS.write_text("\n".join(lignes), encoding="utf-8")


def ajouter_historique(resultats, cfg, horodatage):
    nouveau = not FICHIER_HISTORIQUE.exists()
    meilleur = resultats[0] if resultats else None

    with open(FICHIER_HISTORIQUE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if nouveau:
            writer.writerow(["releve", "origine", "destination", "prix_min",
                             "depart", "retour", "duree_jours",
                             "compagnie", "nb_offres"])
        writer.writerow([
            horodatage.strftime("%Y-%m-%d %H:%M"),
            cfg["origine"],
            cfg["destination"],
            f"{meilleur['prix']:.0f}" if meilleur else "",
            meilleur["depart"].isoformat() if meilleur else "",
            meilleur["retour"].isoformat() if meilleur else "",
            meilleur["duree"] if meilleur else "",
            meilleur["compagnie"] if meilleur else "",
            len(resultats),
        ])


# ------------------------------------------------------------------

def main():
    cfg = charger_config()
    horodatage = dt.datetime.now(dt.timezone.utc)

    print(f"Recherche {cfg['origine']} -> {cfg['destination']} (Google Flights)")
    print(f"Bibliotheque : {BIBLIOTHEQUE}, interface "
          f"{'v3' if API_MODERNE else 'v2'}")
    print(f"Depart du {cfg['depart_le_plus_tot']} au {cfg['depart_le_plus_tard']}")
    print(f"Sejours de {cfg['durees_jours']} jours (+/- {cfg['tolerance_jours']})")
    print()

    couples = paires_de_dates(cfg)
    print(f"{len(couples)} combinaisons a tester, "
          f"{cfg['pause_secondes']}s de pause entre chaque.")
    print(f"Duree estimee : environ {len(couples) * (cfg['pause_secondes'] + 2) // 60 + 1} minutes.")
    print()

    brutes = []
    echecs = 0
    premier_message = None

    for i, (depart, retour) in enumerate(couples, 1):
        try:
            vols = interroger(cfg, depart, retour)
            brutes.extend(vols)
            marque = f"{len(vols)} vols"
        except Exception as erreur:
            echecs += 1
            marque = "echec"
            if premier_message is None:
                premier_message = f"{type(erreur).__name__} : {erreur}"

        print(f"  [{i}/{len(couples)}] {depart} -> {retour} : {marque}")
        time.sleep(cfg["pause_secondes"])

    print()
    print(f"{len(brutes)} vols recuperes, {echecs} interrogations en echec.")

    if echecs and premier_message:
        print(f"\nPremiere erreur rencontree :\n  {premier_message}")
        if echecs == len(couples):
            print(
                "\nToutes les interrogations ont echoue. C'est probablement que "
                "Google bloque les requetes venant des serveurs GitHub, ou que "
                "la bibliotheque a change. Le message ci-dessus le precise."
            )

    resultats = filtrer(brutes, cfg)
    print(f"{len(resultats)} offres retenues apres filtrage.")

    ecrire_resultats(resultats, cfg, horodatage, len(couples))
    ajouter_historique(resultats, cfg, horodatage)

    if resultats:
        m = resultats[0]
        print(f"\nMeilleur prix : {m['prix']:.0f} "
              f"({m['depart']:%d/%m} -> {m['retour']:%d/%m}, "
              f"{m['duree']} jours, {m['compagnie']})")

    print(f"\nEcrit dans {FICHIER_RESULTATS.name} et {FICHIER_HISTORIQUE.name}.")


if __name__ == "__main__":
    main()
