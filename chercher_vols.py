#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Veille de prix de billets d'avion a dates flexibles.

Interroge l'API de donnees Travelpayouts (Aviasales), filtre les offres
selon la fenetre de depart et les durees de sejour definies dans
config.yaml, puis ecrit un tableau dans RESULTATS.md et une ligne
d'historique dans historique.csv.

Ne fait aucune reservation, ne demande aucun moyen de paiement :
le script se contente de lire des prix publics.
"""

import csv
import datetime as dt
import os
import sys
import time
from pathlib import Path

import requests
import yaml

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
RACINE = Path(__file__).resolve().parent
FICHIER_RESULTATS = RACINE / "RESULTATS.md"
FICHIER_HISTORIQUE = RACINE / "historique.csv"

SYMBOLES = {"eur": "EUR", "usd": "USD", "gbp": "GBP", "chf": "CHF", "cad": "CAD"}


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

def en_date(valeur):
    """Accepte une date YAML ou une chaine 'AAAA-MM-JJ'."""
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
    cfg["devise"] = str(cfg.get("devise", "eur")).lower()
    cfg.setdefault("durees_jours", [30, 60])
    cfg.setdefault("tolerance_jours", 3)
    cfg.setdefault("vols_directs_uniquement", False)
    cfg.setdefault("nombre_de_resultats", 20)
    cfg["marche"] = str(cfg.get("marche", "fr")).lower().strip()
    cfg.setdefault("exclure_low_cost", True)
    cfg.setdefault("compagnies_low_cost", [])
    cfg.setdefault("compagnies_exclues", [])

    cfg["compagnies_low_cost"] = {
        str(c).upper().strip() for c in (cfg["compagnies_low_cost"] or [])
    }
    cfg["compagnies_exclues"] = {
        str(c).upper().strip() for c in (cfg["compagnies_exclues"] or [])
    }

    if cfg["depart_le_plus_tard"] < cfg["depart_le_plus_tot"]:
        sys.exit("ERREUR : depart_le_plus_tard est avant depart_le_plus_tot.")

    return cfg


def durees_acceptees(cfg):
    """Ensemble de toutes les durees de sejour valides, en jours."""
    valides = set()
    for duree in cfg["durees_jours"]:
        for ecart in range(-cfg["tolerance_jours"], cfg["tolerance_jours"] + 1):
            if duree + ecart > 0:
                valides.add(duree + ecart)
    return valides


def paires_de_dates(cfg):
    """
    Liste des couples (date de depart, date de retour) exacts a demander.
    On interroge jour par jour : le format "mois entier" est refuse par
    l'API des que l'ecart depart/retour depasse un mois.
    """
    couples = []
    jour = cfg["depart_le_plus_tot"]
    while jour <= cfg["depart_le_plus_tard"]:
        for duree in sorted(durees_acceptees(cfg)):
            couples.append((jour, jour + dt.timedelta(days=duree)))
        jour += dt.timedelta(days=1)
    return couples


# ------------------------------------------------------------------
# Appels API
# ------------------------------------------------------------------

def recuperer_token():
    token = os.environ.get("TRAVELPAYOUTS_TOKEN", "").strip()
    if not token:
        sys.exit(
            "ERREUR : aucun jeton trouve.\n"
            "Sur GitHub, verifie que le secret TRAVELPAYOUTS_TOKEN existe bien\n"
            "dans Settings > Secrets and variables > Actions."
        )
    return token


def interroger_api(cfg, token, depart, retour, silencieux=False):
    params = {
        "origin": cfg["origine"],
        "destination": cfg["destination"],
        "departure_at": depart if isinstance(depart, str) else depart.isoformat(),
        "currency": cfg["devise"],
        "market": cfg["marche"],
        "sorting": "price",
        "limit": 100,
        "page": 1,
        "one_way": "false",
        "direct": "true" if cfg["vols_directs_uniquement"] else "false",
        "token": token,
    }
    if retour is not None:
        params["return_at"] = retour if isinstance(retour, str) else retour.isoformat()
    try:
        reponse = requests.get(API_URL, params=params, timeout=45)
    except requests.RequestException as erreur:
        if not silencieux:
            print(f"  ! probleme reseau ({erreur})")
        return []

    if reponse.status_code != 200:
        if not silencieux:
            print(f"  ! l'API a repondu {reponse.status_code} "
                  f"({reponse.text[:120]})")
        return []

    donnees = reponse.json()
    if not donnees.get("success", False):
        if not silencieux:
            print(f"  ! erreur API : {donnees.get('error')}")
        return []

    return donnees.get("data") or []


# ------------------------------------------------------------------
# Filtrage
# ------------------------------------------------------------------

def date_seule(horodatage):
    """'2026-08-19T10:20:00+02:00' -> date(2026, 8, 19)"""
    try:
        return dt.date.fromisoformat(str(horodatage)[:10])
    except ValueError:
        return None


def note_bagage(compagnie, cfg):
    """
    Indication approximative, deduite de la compagnie.
    L'API ne fournit aucune donnee sur les bagages : c'est une estimation,
    jamais une garantie. A verifier au moment de reserver.
    """
    if compagnie in cfg["compagnies_low_cost"]:
        return "soute payante"
    return "soute souvent incluse"


def filtrer(offres, cfg):
    valides = durees_acceptees(cfg)
    gardees = {}
    ecartees_compagnie = 0

    for offre in offres:
        depart = date_seule(offre.get("departure_at"))
        retour = date_seule(offre.get("return_at"))
        if not depart or not retour:
            continue
        if not (cfg["depart_le_plus_tot"] <= depart <= cfg["depart_le_plus_tard"]):
            continue

        duree = (retour - depart).days
        if duree not in valides:
            continue

        prix = offre.get("price")
        if not prix:
            continue

        compagnie = (offre.get("airline") or "?").upper().strip()

        if compagnie in cfg["compagnies_exclues"]:
            ecartees_compagnie += 1
            continue
        if cfg["exclure_low_cost"] and compagnie in cfg["compagnies_low_cost"]:
            ecartees_compagnie += 1
            continue

        cle = (depart, retour, round(float(prix)), compagnie)
        if cle in gardees:
            continue

        gardees[cle] = {
            "depart": depart,
            "retour": retour,
            "duree": duree,
            "prix": float(prix),
            "compagnie": compagnie,
            "escales": offre.get("transfers"),
            "lien": offre.get("link") or "",
            "bagage": note_bagage(compagnie, cfg),
        }

    if ecartees_compagnie:
        print(f"  ({ecartees_compagnie} offres ecartees : compagnie sans bagage inclus)")

    return sorted(gardees.values(), key=lambda o: o["prix"])


# ------------------------------------------------------------------
# Sorties
# ------------------------------------------------------------------

def lien_complet(chemin):
    if not chemin:
        return ""
    if chemin.startswith("http"):
        return chemin
    return "https://www.aviasales.com" + chemin


def ecrire_resultats(resultats, cfg, horodatage):
    devise = SYMBOLES.get(cfg["devise"], cfg["devise"].upper())
    lignes = []

    lignes.append(f"# Prix les moins chers : {cfg['origine']} vers {cfg['destination']}")
    lignes.append("")
    lignes.append(f"*Mis a jour le {horodatage:%d/%m/%Y a %Hh%M} (UTC).*")
    lignes.append("")
    lignes.append(
        f"Depart entre le **{cfg['depart_le_plus_tot']:%d/%m/%Y}** et le "
        f"**{cfg['depart_le_plus_tard']:%d/%m/%Y}**, sejour de "
        + " ou ".join(f"{d} jours" for d in cfg["durees_jours"])
        + f" (tolerance {cfg['tolerance_jours']} jours)."
    )
    lignes.append("")
    if cfg["exclure_low_cost"]:
        lignes.append(
            "Compagnies low-cost ecartees : ces resultats visent des billets avec "
            "bagage en soute."
        )
    else:
        lignes.append(
            "Toutes les compagnies sont incluses, bagage en soute non garanti."
        )
    lignes.append("")

    if not resultats:
        lignes.append("## Aucun resultat")
        lignes.append("")
        lignes.append(
            "L'API n'a rien renvoye pour ces criteres. Pistes : elargir la fenetre "
            "de depart, augmenter `tolerance_jours`, ou verifier les codes de villes "
            "dans `config.yaml`. Cette route est peut-etre simplement peu recherchee."
        )
    else:
        meilleur = resultats[0]
        lignes.append("## Le moins cher en ce moment")
        lignes.append("")
        lignes.append(
            f"**{meilleur['prix']:.0f} {devise}** - depart le "
            f"{meilleur['depart']:%d/%m/%Y}, retour le {meilleur['retour']:%d/%m/%Y} "
            f"({meilleur['duree']} jours)"
        )
        lignes.append("")
        lignes.append("## Les autres options")
        lignes.append("")
        lignes.append(
            "| Prix | Depart | Retour | Duree | Compagnie | Escales | Bagage | Voir |"
        )
        lignes.append("|---|---|---|---|---|---|---|---|")
        for offre in resultats[: cfg["nombre_de_resultats"]]:
            lien = lien_complet(offre["lien"])
            cellule = f"[lien]({lien})" if lien else "-"
            escales = offre["escales"] if offre["escales"] is not None else "?"
            lignes.append(
                f"| {offre['prix']:.0f} {devise} "
                f"| {offre['depart']:%d/%m} "
                f"| {offre['retour']:%d/%m} "
                f"| {offre['duree']} j "
                f"| {offre['compagnie']} "
                f"| {escales} "
                f"| {offre['bagage']} "
                f"| {cellule} |"
            )

    lignes.append("")
    lignes.append("---")
    lignes.append("")
    lignes.append(
        "Prix indicatifs issus du cache Travelpayouts/Aviasales : ils servent a "
        "reperer les bonnes dates, pas a garantir un tarif. Verifie toujours le "
        "prix reel avant de reserver. L'historique complet est dans `historique.csv`."
    )
    lignes.append("")
    lignes.append(
        "**Colonne Bagage** : indication deduite de la compagnie, pas du billet. "
        "L'API ne transmet aucune donnee bagage. Meme sur une grande compagnie, un "
        "tarif *basic* ou *light* peut exclure la soute. A verifier a la reservation."
    )
    lignes.append("")

    FICHIER_RESULTATS.write_text("\n".join(lignes), encoding="utf-8")


def ajouter_historique(resultats, cfg, horodatage):
    nouveau = not FICHIER_HISTORIQUE.exists()
    meilleur = resultats[0] if resultats else None

    with open(FICHIER_HISTORIQUE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if nouveau:
            writer.writerow(
                ["releve", "origine", "destination", "prix_min",
                 "devise", "depart", "retour", "duree_jours", "nb_offres"]
            )
        writer.writerow([
            horodatage.strftime("%Y-%m-%d %H:%M"),
            cfg["origine"],
            cfg["destination"],
            f"{meilleur['prix']:.0f}" if meilleur else "",
            cfg["devise"],
            meilleur["depart"].isoformat() if meilleur else "",
            meilleur["retour"].isoformat() if meilleur else "",
            meilleur["duree"] if meilleur else "",
            len(resultats),
        ])


# ------------------------------------------------------------------

def diagnostic(brutes, cfg):
    """Affiche ce que l'API a reellement renvoye, quand rien ne colle."""
    if not brutes:
        print("\nL'API n'a renvoye aucune offre du tout.")
        print("Verifie les codes de villes et le marche dans config.yaml.")
        return

    departs, durees = [], []
    for o in brutes:
        d = date_seule(o.get("departure_at"))
        r = date_seule(o.get("return_at"))
        if d:
            departs.append(d)
        if d and r:
            durees.append((r - d).days)

    print("\n--- Diagnostic : contenu reel du cache ---")
    if departs:
        print(f"Departs proposes : du {min(departs)} au {max(departs)}")
    if durees:
        print(f"Durees proposees : de {min(durees)} a {max(durees)} jours")
        courantes = sorted(set(durees))[:20]
        print(f"Durees disponibles : {courantes}")
    print(f"Ta fenetre : {cfg['depart_le_plus_tot']} -> {cfg['depart_le_plus_tard']}")
    print(f"Tes durees : {sorted(durees_acceptees(cfg))}")
    print("-------------------------------------------")


def main():
    cfg = charger_config()
    token = recuperer_token()
    horodatage = dt.datetime.now(dt.timezone.utc)

    print(f"Recherche {cfg['origine']} -> {cfg['destination']} (marche {cfg['marche']})")
    print(f"Depart du {cfg['depart_le_plus_tot']} au {cfg['depart_le_plus_tard']}")
    print(f"Sejours de {cfg['durees_jours']} jours (+/- {cfg['tolerance_jours']})")
    print()

    brutes = []

    # 1) Balayage large : tout ce que le cache connait sur ce mois de depart.
    for mois in sorted({cfg["depart_le_plus_tot"].strftime("%Y-%m"),
                        cfg["depart_le_plus_tard"].strftime("%Y-%m")}):
        print(f"  balayage large du mois {mois}...")
        brutes.extend(interroger_api(cfg, token, mois, None))

    print(f"  -> {len(brutes)} offres apres balayage large")

    # 2) Interrogation ciblee, date par date.
    couples = paires_de_dates(cfg)
    print(f"\n  {len(couples)} combinaisons de dates a tester...")
    trouvees = 0
    for i, (depart, retour) in enumerate(couples, 1):
        offres = interroger_api(cfg, token, depart, retour, silencieux=True)
        trouvees += len(offres)
        brutes.extend(offres)
        if i % 25 == 0:
            print(f"    {i}/{len(couples)} testees, {trouvees} offres trouvees")
        time.sleep(0.25)

    print(f"  -> {trouvees} offres apres interrogation ciblee")

    print(f"\n{len(brutes)} offres recuperees au total.")
    resultats = filtrer(brutes, cfg)
    print(f"{len(resultats)} offres correspondent a tes criteres.")

    if not resultats:
        diagnostic(brutes, cfg)

    ecrire_resultats(resultats, cfg, horodatage)
    ajouter_historique(resultats, cfg, horodatage)

    if resultats:
        devise = SYMBOLES.get(cfg["devise"], cfg["devise"].upper())
        m = resultats[0]
        print(f"\nMeilleur prix : {m['prix']:.0f} {devise} "
              f"({m['depart']:%d/%m} -> {m['retour']:%d/%m}, {m['duree']} jours)")

    print(f"\nEcrit dans {FICHIER_RESULTATS.name} et {FICHIER_HISTORIQUE.name}.")


if __name__ == "__main__":
    main()
