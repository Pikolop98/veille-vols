# Veille de prix de billets d'avion

Cherche automatiquement les billets les moins chers sur une **fenetre de dates
souple** ("n'importe quel jour apres le 17 aout, pour un sejour d'un ou deux
mois") au lieu de tester les dates une par une a la main.

Le script tourne tout seul chez GitHub, une fois par jour. Il n'y a rien a
installer sur ton ordinateur : tu ouvres simplement le fichier
**[RESULTATS.md](RESULTATS.md)** pour voir le tableau des prix.

---

## Ce qu'il y a dans le depot

| Fichier | A quoi ca sert |
|---|---|
| `config.yaml` | **Le seul fichier a modifier.** Villes, dates, durees. |
| `RESULTATS.md` | Le tableau des prix, regenere a chaque passage. |
| `historique.csv` | Une ligne par jour, pour voir si les prix montent ou descendent. |
| `chercher_vols.py` | Le programme. Pas besoin d'y toucher. |
| `.github/workflows/veille.yml` | La minuterie qui lance la recherche chaque matin. |

---

## Installation, une seule fois

### 1. Obtenir un jeton d'acces (gratuit)

1. Cree un compte sur <https://www.travelpayouts.com> (c'est un reseau
   d'affiliation, l'inscription est gratuite et sans carte bancaire).
2. Dans ton profil, va dans la section **API token** et copie le jeton.

### 2. Donner le jeton a GitHub

Dans ton depot GitHub : **Settings** > **Secrets and variables** > **Actions**
> bouton **New repository secret**.

- Name : `TRAVELPAYOUTS_TOKEN`
- Secret : colle ton jeton
- **Add secret**

Le jeton reste cache, il n'apparait jamais dans le code.

### 3. Autoriser le robot a ecrire

**Settings** > **Actions** > **General** > section *Workflow permissions* >
coche **Read and write permissions** > **Save**.

### 4. Premier essai

Onglet **Actions** > *Veille prix vols* > bouton **Run workflow**.
Au bout d'une minute, `RESULTATS.md` apparait avec le tableau.

---

## Changer la recherche

Ouvre `config.yaml` sur GitHub, clique sur le crayon, modifie, puis
**Commit changes**. La prochaine recherche utilisera les nouveaux criteres.

```yaml
origine: PAR              # Paris (tous les aeroports)
destination: BKK          # Bangkok
depart_le_plus_tot: 2026-08-18
depart_le_plus_tard: 2026-08-31
durees_jours: [30, 60]    # 1 mois ou 2 mois
tolerance_jours: 3        # accepte 27 a 33 jours, et 57 a 63 jours
devise: eur
exclure_low_cost: true    # ecarte les compagnies a bagage payant
```

---

## Les bagages : ce que fait vraiment le filtre

L'API de donnees Travelpayouts **ne transmet aucune information sur les
bagages**. Elle renvoie le prix, la compagnie, les dates, les escales, et
c'est tout. Il est donc impossible de filtrer sur "billet avec soute
incluse" de facon exacte.

Le reglage `exclure_low_cost` contourne le probleme par la compagnie : il
ecarte Ryanair, Wizz, Transavia, Vueling, Scoot, AirAsia X, Spirit, etc.,
ou la soute est presque toujours un supplement. En pratique, cela elimine
la grande majorite des billets "sac a dos uniquement".

Deux limites a garder en tete :

- Sur une grande compagnie, un tarif *basic* / *light* peut quand meme
  exclure la soute. La colonne **Bagage** du tableau est une indication,
  pas une garantie.
- Si tu veux au contraire les billets les moins chers coute que coute,
  bagage cabine seulement, mets `exclure_low_cost: false`.

Tu peux editer librement la liste `compagnies_low_cost` dans
`config.yaml`, et ajouter des codes dans `compagnies_exclues` pour bannir
une compagnie dans tous les cas.

---

## A savoir

Les prix viennent du cache de Travelpayouts / Aviasales : ce sont de **vrais
prix trouves recemment par de vrais voyageurs**, mais ils peuvent avoir bouge
depuis. Sers-t'en pour reperer les meilleures dates, puis verifie le tarif
reel avant de reserver.

Si le tableau est vide, c'est en general que la route est peu recherchee ou
que la fenetre est trop etroite : elargis les dates ou augmente
`tolerance_jours`.
