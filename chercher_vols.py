#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Veille de prix de billets d'avion - balayage large + export pour l'interface web.

Interroge Google Flights sur une large fenetre de dates, puis depose tout ce
qu'il a trouve dans donnees.json. La page index.html se charge ensuite du tri
et du filtrage, cote navigateur, sans aucune attente.

Ne fait aucune reservation et ne demande aucun moyen de paiement.
"""

import csv
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

import yaml

# La bibliotheque a change d'interface entre ses versions 2 et 3.
_module = None
BIBLIOTHEQUE = None
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

API_MODERNE = FlightQuery is not None and create_query is not None

if get_flights is None or Passengers is None or (
        not API_MODERNE and FlightData is None):
    expose = [n for n in dir(_module) if not n.startswith("_")]
    print(f"ERREUR : le module {BIBLIOTHEQUE} n'expose pas les fonctions attendues.")
    print(f"Il contient : {expose}")
    sys.exit(1)

RACINE = Path(__file__).resolve().parent
FICHIER_JSON = RACINE / "donnees.json"
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
    brut = cfg.get("destinations") or [cfg.get("destination")]
    if isinstance(brut, str):
        brut = [brut]
    cfg["destinations"] = [str(d).upper().strip() for d in brut if d]
    cfg["destination"] = cfg["destinations"][0]
    cfg["depart_le_plus_tot"] = en_date(cfg["depart_le_plus_tot"])
    cfg["depart_le_plus_tard"] = en_date(cfg["depart_le_plus_tard"])
    cfg.setdefault("durees_jours", [30, 60])
    cfg.setdefault("tolerance_jours", 2)
    cfg.setdefault("pas_tolerance", 2)
    cfg.setdefault("max_requetes", 120)
    cfg.setdefault("pause_secondes", 8)
    cfg.setdefault("vols_directs_uniquement", False)
    cfg.setdefault("nombre_de_resultats", 20)
    cfg.setdefault("exclure_low_cost", True)
    cfg.setdefault("compagnies_low_cost", [])
    cfg.setdefault("devise", "EUR")

    cfg["compagnies_low_cost"] = [
        str(c).lower().strip() for c in (cfg["compagnies_low_cost"] or [])
    ]

    if cfg["depart_le_plus_tard"] < cfg["depart_le_plus_tot"]:
        sys.exit("ERREUR : depart_le_plus_tard est avant depart_le_plus_tot.")

    for code in [cfg["origine"]] + cfg["destinations"]:
        if len(code) != 3:
            sys.exit(f"ERREUR : '{code}' n'est pas un code d'aeroport a 3 lettres.")

    return cfg


def durees_a_tester(cfg):
    """Durees echantillonnees : -2, 0, +2 plutot que -2, -1, 0, +1, +2."""
    valides = set()
    pas = max(1, int(cfg["pas_tolerance"]))
    for duree in cfg["durees_jours"]:
        ecart = -abs(cfg["tolerance_jours"])
        while ecart <= abs(cfg["tolerance_jours"]):
            if duree + ecart > 0:
                valides.add(duree + ecart)
            ecart += pas
    return sorted(valides)


def paires_de_dates(cfg):
    couples = []
    jour = cfg["depart_le_plus_tot"]
    while jour <= cfg["depart_le_plus_tard"]:
        for duree in durees_a_tester(cfg):
            couples.append((jour, jour + dt.timedelta(days=duree)))
        jour += dt.timedelta(days=1)

    plafond = int(cfg["max_requetes"])
    if len(couples) > plafond:
        pas = len(couples) / plafond
        echantillon = [couples[int(i * pas)] for i in range(plafond)]
        print(f"  ({len(couples)} combinaisons ramenees a {len(echantillon)})")
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
    for nom in noms:
        valeur = getattr(objet, nom, None)
        if valeur not in (None, ""):
            return valeur
    return None


_PREMIER_VOL_INSPECTE = False


def inspecter(vol):
    global _PREMIER_VOL_INSPECTE
    if _PREMIER_VOL_INSPECTE:
        return
    _PREMIER_VOL_INSPECTE = True
    print(f"  (structure d'un vol : {[n for n in dir(vol) if not n.startswith('_')]})")


def compagnie_de(vol):
    valeur = texte(vol, "airlines", "name", "airline", "carrier")
    if isinstance(valeur, (list, tuple, set)):
        return ", ".join(str(v) for v in valeur if v) or "?"
    return str(valeur) if valeur else "?"


def _appeler(cfg, legs, trip):
    """legs = [(date, depuis, vers), ...]. trip = 'round-trip' ou 'one-way'."""
    if API_MODERNE:
        requete = create_query(
            flights=[FlightQuery(date=d.isoformat(), from_airport=a, to_airport=b)
                     for d, a, b in legs],
            trip=trip, seat="economy", passengers=Passengers(adults=1),
            language="fr-FR", currency=cfg["devise"],
        )
        return list(get_flights(requete) or [])

    resultat = get_flights(
        flight_data=[FlightData(date=d.isoformat(), from_airport=a, to_airport=b)
                     for d, a, b in legs],
        trip=trip, seat="economy",
        passengers=Passengers(adults=1, children=0,
                              infants_in_seat=0, infants_on_lap=0),
        fetch_mode="fallback",
    )
    return list(getattr(resultat, "flights", None) or [])


def _lire(vol):
    """Extrait prix, compagnie et escales d'un vol renvoye par la bibliotheque."""
    inspecter(vol)
    prix = prix_en_nombre(texte(vol, "price"))
    if not prix:
        return None
    escales = texte(vol, "stops")
    if escales is None:
        segments = getattr(vol, "flights", None)
        if isinstance(segments, (list, tuple)) and segments:
            escales = len(segments) - 1
    return {"prix": prix, "compagnie": compagnie_de(vol), "escales": escales}


def interroger_ar(cfg, depart, retour, dst):
    """Aller-retour classique. Google ne repond que jusqu'a 30 jours de sejour."""
    legs = [(depart, cfg["origine"], dst), (retour, dst, cfg["origine"])]
    sorties = []
    for vol in _appeler(cfg, legs, "round-trip"):
        lu = _lire(vol)
        if lu:
            sorties.append({**lu, "depart": depart, "retour": retour,
                            "duree": (retour - depart).days,
                            "aller_retour": True, "destination": dst})
    return sorties


def interroger_simple(cfg, date, depuis, vers):
    """Aller simple. Aucune limite de duree, puisqu'il n'y a pas de retour."""
    vols = []
    for vol in _appeler(cfg, [(date, depuis, vers)], "one-way"):
        lu = _lire(vol)
        if lu:
            vols.append(lu)
    return sorted(vols, key=lambda v: v["prix"])


def est_low_cost(compagnie, cfg):
    minuscule = compagnie.lower()
    return any(bas in minuscule for bas in cfg["compagnies_low_cost"])


def dedupliquer(brutes, cfg):
    """Supprime les doublons et marque les compagnies low-cost."""
    gardees = {}
    for offre in brutes:
        cle = (offre.get("destination"), offre["depart"], offre["retour"],
               round(offre["prix"]), offre["compagnie"])
        if cle not in gardees:
            offre["low_cost"] = est_low_cost(offre["compagnie"], cfg)
            offre.setdefault("aller_retour", True)
            gardees[cle] = offre
    return sorted(gardees.values(), key=lambda o: o["prix"])


# ------------------------------------------------------------------
# Sorties
# ------------------------------------------------------------------

def ecrire_json(offres, cfg, horodatage, testees):
    donnees = {
        "genere_le": horodatage.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "origine": cfg["origine"],
        "destination": cfg["destinations"][0],
        "destinations": cfg["destinations"],
        "devise": cfg["devise"],
        "fenetre": {
            "debut": cfg["depart_le_plus_tot"].isoformat(),
            "fin": cfg["depart_le_plus_tard"].isoformat(),
        },
        "durees_cibles": cfg["durees_jours"],
        "combinaisons_testees": testees,
        "offres": [
            {
                "d": o["depart"].isoformat(),
                "r": o["retour"].isoformat(),
                "j": o["duree"],
                "p": round(o["prix"]),
                "c": o["compagnie"],
                "e": o["escales"] if o["escales"] is not None else None,
                "lc": bool(o["low_cost"]),
                "ar": bool(o.get("aller_retour", True)),
                "t": o.get("destination", cfg["destinations"][0]),
            }
            for o in offres
        ],
    }
    FICHIER_JSON.write_text(
        json.dumps(donnees, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def lien_google(cfg, depart, retour):
    return (
        "https://www.google.com/travel/flights?q="
        f"Flights%20from%20{cfg['origine']}%20to%20{cfg['destination']}%20"
        f"on%20{depart}%20through%20{retour}"
    )


def ecrire_resultats(offres, cfg, horodatage, testees):
    """Version texte, conservee comme filet de securite."""
    retenues = [o for o in offres
                if not (cfg["exclure_low_cost"] and o["low_cost"])]

    lignes = [
        f"# Prix les moins chers : {cfg['origine']} vers {cfg['destination']}",
        "",
        f"*Releve du {horodatage:%d/%m/%Y a %Hh%M} UTC, "
        f"{testees} combinaisons testees.*",
        "",
        "La version consultable est la page web du depot. Ce fichier est "
        "une copie de secours.",
        "",
    ]

    if not retenues:
        lignes += ["## Aucun resultat", "",
                   "Consulte le journal de l'onglet Actions pour la cause."]
    else:
        m = retenues[0]
        lignes += [
            "## Le moins cher en ce moment", "",
            f"**{m['prix']:.0f} {cfg['devise']}** - depart le "
            f"{m['depart']:%d/%m/%Y}, retour le {m['retour']:%d/%m/%Y} "
            f"({m['duree']} jours), {m['compagnie']}", "",
            "| Prix | Depart | Retour | Duree | Compagnie | Escales | Voir |",
            "|---|---|---|---|---|---|---|",
        ]
        for o in retenues[: cfg["nombre_de_resultats"]]:
            lien = lien_google(cfg, o["depart"].isoformat(), o["retour"].isoformat())
            lignes.append(
                f"| {o['prix']:.0f} {cfg['devise']} | {o['depart']:%d/%m} "
                f"| {o['retour']:%d/%m} | {o['duree']} j | {o['compagnie']} "
                f"| {o['escales'] if o['escales'] is not None else '?'} "
                f"| [ouvrir]({lien}) |"
            )

    lignes += ["", "---", "",
               "Prix releves sur Google Flights au moment du passage. "
               "Verifie le tarif reel avant de reserver."]
    FICHIER_RESULTATS.write_text("\n".join(lignes) + "\n", encoding="utf-8")


def ajouter_historique(offres, cfg, horodatage):
    retenues = [o for o in offres
                if not (cfg["exclure_low_cost"] and o["low_cost"])]
    meilleur = retenues[0] if retenues else None
    nouveau = not FICHIER_HISTORIQUE.exists()

    with open(FICHIER_HISTORIQUE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if nouveau:
            writer.writerow(["releve", "origine", "destination", "prix_min",
                             "devise", "depart", "retour", "duree_jours",
                             "compagnie", "nb_offres"])
        writer.writerow([
            horodatage.strftime("%Y-%m-%d %H:%M"),
            cfg["origine"], cfg["destination"],
            f"{meilleur['prix']:.0f}" if meilleur else "",
            cfg["devise"],
            meilleur["depart"].isoformat() if meilleur else "",
            meilleur["retour"].isoformat() if meilleur else "",
            meilleur["duree"] if meilleur else "",
            meilleur["compagnie"] if meilleur else "",
            len(retenues),
        ])


# ------------------------------------------------------------------

def dates_de_depart(cfg):
    jours, j = [], cfg["depart_le_plus_tot"]
    while j <= cfg["depart_le_plus_tard"]:
        jours.append(j)
        j += dt.timedelta(days=1)
    return jours


def chercher_destination(cfg, dst, departs, courtes, longues, pause):
    """Balaye une destination : aller-retours courts + allers simples longs."""
    brutes, echecs, premiere = [], 0, None

    couples = [(d, d + dt.timedelta(days=n)) for d in departs for n in courtes]
    plafond = int(cfg["max_requetes"])
    if len(couples) > plafond:
        pas = len(couples) / plafond
        couples = [couples[int(i * pas)] for i in range(plafond)]

    print(f"  Phase 1 : {len(couples)} aller-retours")
    for i, (depart, retour) in enumerate(couples, 1):
        try:
            vols = interroger_ar(cfg, depart, retour, dst)
            brutes.extend(vols)
            marque = f"{len(vols)} vols"
        except Exception as e:
            echecs += 1
            marque = "echec"
            premiere = premiere or f"{type(e).__name__} : {e}"
        print(f"    [AR {i}/{len(couples)}] {depart} -> {retour} "
              f"({(retour-depart).days} j) : {marque}")
        time.sleep(pause)

    if longues:
        retours_voulus = sorted({d + dt.timedelta(days=n)
                                 for d in departs for n in longues})
        print(f"  Phase 2 : {len(departs)} allers + {len(retours_voulus)} retours")
        allers, retours = {}, {}

        for i, d in enumerate(departs, 1):
            try:
                allers[d] = interroger_simple(cfg, d, cfg["origine"], dst)
                marque = f"{len(allers[d])} vols"
            except Exception as e:
                echecs += 1
                marque = "echec"
                premiere = premiere or f"{type(e).__name__} : {e}"
            print(f"    [ALLER {i}/{len(departs)}] {d} : {marque}")
            time.sleep(pause)

        for i, r in enumerate(retours_voulus, 1):
            try:
                retours[r] = interroger_simple(cfg, r, dst, cfg["origine"])
                marque = f"{len(retours[r])} vols"
            except Exception as e:
                echecs += 1
                marque = "echec"
                premiere = premiere or f"{type(e).__name__} : {e}"
            print(f"    [RETOUR {i}/{len(retours_voulus)}] {r} : {marque}")
            time.sleep(pause)

        combines = 0
        for d in departs:
            for n in longues:
                r = d + dt.timedelta(days=n)
                a, b = allers.get(d), retours.get(r)
                if not a or not b:
                    continue
                va, vb = a[0], b[0]
                escales = None
                if va["escales"] is not None and vb["escales"] is not None:
                    escales = max(va["escales"], vb["escales"])
                brutes.append({
                    "depart": d, "retour": r, "duree": n,
                    "prix": va["prix"] + vb["prix"],
                    "compagnie": f"{va['compagnie']} + {vb['compagnie']}",
                    "escales": escales, "aller_retour": False,
                    "destination": dst,
                })
                combines += 1
        print(f"    -> {combines} longs sejours reconstitues")

    return brutes, echecs, premiere


def dates_de_depart(cfg):
    jours, j = [], cfg["depart_le_plus_tot"]
    while j <= cfg["depart_le_plus_tard"]:
        jours.append(j)
        j += dt.timedelta(days=1)
    return jours


def main():
    cfg = charger_config()
    horodatage = dt.datetime.now(dt.timezone.utc)
    seuil = int(cfg.get("seuil_aller_retour", 30))
    pause = cfg["pause_secondes"]

    departs = dates_de_depart(cfg)
    durees = durees_a_tester(cfg)
    courtes = [d for d in durees if d <= seuil]
    longues = [d for d in durees if d > seuil]

    print(f"Depart de {cfg['origine']} vers {', '.join(cfg['destinations'])}")
    print(f"Bibliotheque : {BIBLIOTHEQUE}, interface "
          f"{'v3' if API_MODERNE else 'v2'}")
    print(f"Depart du {cfg['depart_le_plus_tot']} au {cfg['depart_le_plus_tard']}")
    print(f"Sejours courts (aller-retour) : {courtes}")
    print(f"Sejours longs (2 allers simples) : {longues}")
    print()

    brutes, echecs, premiere = [], 0, None
    for n, dst in enumerate(cfg["destinations"], 1):
        print(f"=== Destination {n}/{len(cfg['destinations'])} : {dst} ===")
        b, e, p = chercher_destination(cfg, dst, departs, courtes, longues, pause)
        brutes.extend(b)
        echecs += e
        premiere = premiere or p
        print()

    print(f"{len(brutes)} vols recuperes, {echecs} interrogations en echec.")
    if premiere:
        print(f"Premiere erreur : {premiere}")

    offres = dedupliquer(brutes, cfg)
    longs = sum(1 for o in offres if not o.get("aller_retour", True))
    print(f"{len(offres)} offres uniques, dont {longs} en 2 allers simples.")
    for dst in cfg["destinations"]:
        n = sum(1 for o in offres if o.get("destination") == dst)
        print(f"  {dst} : {n} offres")

    ecrire_json(offres, cfg, horodatage, len(brutes))
    ecrire_resultats(offres, cfg, horodatage, len(brutes))
    ajouter_historique(offres, cfg, horodatage)

    if offres:
        m = offres[0]
        print(f"\nMoins cher : {m['prix']:.0f} {cfg['devise']} vers "
              f"{m.get('destination')} ({m['depart']:%d/%m} -> {m['retour']:%d/%m}, "
              f"{m['duree']} j, {m['compagnie']})")

    print(f"\nEcrit dans {FICHIER_JSON.name}, {FICHIER_RESULTATS.name} "
          f"et {FICHIER_HISTORIQUE.name}.")


if __name__ == "__main__":
    main()
