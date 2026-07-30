# Astro-1

Pipeline automatique de traitement d'astrophotos : de vos fichiers FITS bruts
jusqu'à une image finale étirée, colorisée et prête à partager.

Astro-1 est un **orchestrateur**. Il ne réimplémente aucun algorithme de traitement
d'image — il pilote des logiciels open source existants via leur ligne de commande,
en fonction de profils de configuration YAML simples.

| Étape | Moteur utilisé | Rôle |
|---|---|---|
| Calibration, registration, empilement | [Siril](https://siril.org) | Moteur mature, scriptable, GPL |
| Extraction de fond de ciel | [GraXpert](https://github.com/Steffenhir/GraXpert) | Modèle IA pré-entraîné |
| Stretch, couleur, sharpening, export | [Siril](https://siril.org) | Commandes CLI scriptées |

---

## Installation

### Prérequis

1. **Siril** — [téléchargement](https://siril.org/download/) ou `brew install --cask siril`
2. **GraXpert** — [téléchargement](https://github.com/Steffenhir/GraXpert/releases)
3. **Python 3.11+** et [uv](https://github.com/astral-sh/uv) (`brew install uv`)

Vérifiez que tout est en place :

```bash
uv run astro doctor
```

### Mise en route

```bash
git clone https://github.com/ohlee-one/astro-1.git
cd astro-1
uv sync
```

### Premier lancement de GraXpert (important)

Lancez GraXpert **en mode graphique une fois** et appliquez un traitement sur
n'importe quelle image. C'est la seule façon de télécharger les modèles IA
(le mode CLI ne sait pas les récupérer). Une fois les modèles en cache, le mode
CLI fonctionne sans réseau.

---

## Organiser une session

Le pipeline attend cette structure de dossiers pour chaque session :

```
ma-session/
├── lights/     ← vos poses sur la cible (FITS)
├── darks/      ← vos darks (ou MasterDark_*.fit si pré-empilés)
├── flats/      ← vos flats (ou MasterFlat_*.fit si pré-empilés)
└── biases/     ← vos offsets (optionnel)
```

Les dossiers `process/`, `output/` et `logs/` sont créés automatiquement.

---

## Lancer un traitement

```bash
uv run astro run \
  --session ~/Astro/2026-08-12_M42 \
  --setup redcat51-asi294mc \
  --target nebula-narrowband
```

Le pipeline enchaîne automatiquement :

1. **Siril** — calibration, alignement, empilement → FITS linéaires
2. **GraXpert** — extraction du fond de ciel (sur le linéaire)
3. **Siril** — recomposition Ha+OIII en RGB (mode bande étroite uniquement)
4. **GraXpert** — débruitage IA (optionnel, sur le RGB linéaire)
5. **Siril** — stretch, couleur, sharpening, export → TIFF final

L'image finale est dans `output/final.tif`.

### Aperçu sans exécuter

```bash
uv run astro run --session ... --setup ... --target ... --dry-run
```

Affiche les scripts Siril et commandes GraXpert générés **sans rien lancer**.

---

## Profils de configuration

Astro-1 fonctionne avec deux types de profils YAML :

- **Setup** (`profiles/setups/`) — décrit votre matériel. Change rarement.
  - Télescope, caméra, pattern de Bayer, dossiers de calibration
- **Target** (`profiles/targets/`) — décrit ce que vous photographiez. Change à chaque sortie.
  - Mode de traitement, paramètres d'empilement, post-traitement (stretch, couleur, etc.)

### Templates prêts à copier

Le dossier `templates/` contient des profils de départ optimisés par type
d'objet et de matériel :

**Setups (matériel) :**

| Template | Usage |
|---|---|
| `setup-color-dualband.yaml` | Caméra couleur + filtre dual-band (L-eXtreme, L-Ultimate) |
| `setup-color-lp.yaml` | Caméra couleur + filtre LP/UV-IR (large bande) |
| `setup-mono-narrowband.yaml` | Caméra mono + filtres bande étroite (Ha, OIII, SII) |
| `setup-mono-lrgb.yaml` | Caméra mono + filtres LRGB |
| `setup-dslr.yaml` | DSLR / appareil photo |

**Targets (cibles) :**

| Template | Usage |
|---|---|
| `nebula-narrowband.yaml` | Nébuleuse en bande étroite (Ha/OIII) |
| `nebula-rgb.yaml` | Nébuleuse en RGB large bande |
| `galaxy-rgb.yaml` | Galaxie |
| `cluster-rgb.yaml` | Amas d'étoiles (ouvert ou globulaire) |
| `comet-rgb.yaml` | Comète |
| `snr-narrowband.yaml` | Reste de supernova (Veil, Cygnus Loop…) |

### Assistant interactif (wizard)

Pour démarrer encore plus vite, utilisez l'assistant interactif :

```bash
uv run astro wizard
```

Le wizard vous guide étape par étape, **sans aucune connaissance technique requise** :

1. **Caméra** — choisissez votre modèle dans une liste (ASI294MC, ASI533MC, Canon 600D…) ou entrez un nom. La taille des photosites est automatiquement déduite.
2. **Filtre** — sélectionnez votre filtre (L-eXtreme, L-Pro, Ha 7nm…). Le type de traitement est déduit automatiquement.
3. **Fichiers pré-empilés** — indiquez si vos darks/flats sont déjà empilés par votre logiciel d'acquisition (ASIAIR, NINA) et si vous shootez des bias séparés.
4. **Cible** — choisissez le type d'objet (nébuleuse, galaxie, amas, comète…) et donnez-lui un nom (M42, IC1805…).

Les profils YAML sont générés automatiquement dans `profiles/`. Plus besoin de copier et éditer les templates à la main.

---

## Itérer avec une IA (Cursor, Claude, ChatGPT…)

Le pipeline est conçu pour fonctionner avec un assistant IA qui ajuste les profils
à votre place. Deux approches possibles :

### Approche simple (sans MCP)

1. Ouvrez le projet dans [Cursor](https://cursor.sh) ou votre éditeur avec un agent IA
2. Lancez le pipeline : `uv run astro run --session ... --setup ... --target ...`
3. Si le rendu ne vous plaît pas, décrivez ce que vous voulez à l'IA :
   - *"L'image est trop sombre"* → l'IA ajuste `stretch.shadows_clip` ou `stretch.target_bg`
   - *"Les couleurs de la nébuleuse ne sont pas assez vives"* → l'IA ajuste `color.saturation_boost`
   - *"Le fond de ciel est trop clair"* → l'IA ajuste `color.background_clip`
   - *"Il y a trop de vert résiduel"* → l'IA change `color.rmgreen_type` de `"average"` à `"maximum"`
4. Relancez le pipeline — itérez jusqu'au résultat souhaité

L'IA lit les profils YAML, comprend la structure, et modifie les bons paramètres.
Aucune connaissance de Siril ou de GraXpert nécessaire.

### Approche MCP (avancée — itération automatisée)

Astro-1 inclut un serveur MCP (Model Context Protocol) qui expose les outils du
pipeline directement à votre IA. L'IA peut lancer le pipeline, lire les logs,
et ajuster les profils — le tout sans que vous touchiez au terminal.

**Installation :**

```bash
uv sync --extra mcp
```

**Configuration pour Claude Desktop :**

Ajoutez ce bloc à `~/Library/Application Support/Claude/claude_desktop_config.json` :

```json
{
  "mcpServers": {
    "astro-1": {
      "command": "uv",
      "args": ["run", "python", "-m", "astro_pipeline.mcp_server"],
      "cwd": "/chemin/vers/astro-1"
    }
  }
}
```

**Configuration pour Cursor :**

Créez un fichier `.mcp.json` à la racine du projet :

```json
{
  "mcpServers": {
    "astro-1": {
      "command": "uv",
      "args": ["run", "python", "-m", "astro_pipeline.mcp_server"]
    }
  }
}
```

**Outils exposés :**

| Outil | Rôle |
|---|---|
| `list_profiles_tool` | Liste les profils setup et target disponibles |
| `doctor_tool` | Vérifie que Siril et GraXpert sont installés |
| `run_pipeline_tool` | Lance le pipeline complet sur une session |
| `read_log_tool` | Lit le log de la dernière exécution |
| `get_profile_tool` | Récupère le contenu d'un profil YAML |
| `adjust_profile_tool` | Modifie un paramètre dans un profil YAML |

Une fois connecté, vous pouvez dire à Claude : *"Lance le pipeline sur ma session
M42 avec le setup redcat51 et le target nebula-narrowband, puis si l'image est
trop sombre ajuste le stretch."* — Claude le fait tout seul.

### Paramètres clés pour le rendu

| Paramètre | Fichier | Effet |
|---|---|---|
| `stretch.target_bg` | target | Luminosité du fond (0.15 = sombre, 0.35 = clair) |
| `stretch.shadows_clip` | target | Conservation des ombres (-2.8 = standard, -1.0 = plus de contraste) |
| `color.saturation_boost` | target | Saturation globale (0.5 = +50%, 1.0 = +100%) |
| `color.saturation_threshold` | target | Seuil de bruit (0 = tout saturer, 1.5 = seulement les zones lumineuses) |
| `color.target_hue_boost` | target | Saturation ciblée sur une teinte (5 = magenta-rose) |
| `color.background_clip` | target | Assombrir le fond (0.02 = subtil, 0.06 = prononcé) |
| `color.rmgreen_type` | target | Suppression du vert ("average" = doux, "maximum" = agressif) |
| `sharpening.amount` | target | Force du sharpening (0.5 = modéré, 1.0 = fort) |

---

## Commandes disponibles

```bash
uv run astro doctor          # Vérifie que Siril et GraXpert sont installés
uv run astro run ...          # Lance le pipeline complet
uv run astro run ... --dry-run  # Affiche les scripts sans exécuter
```

---

## Architecture

```
src/astro_pipeline/
├── cli.py          → point d'entrée (Typer CLI)
├── pipeline.py     → enchaîne les phases, gère les chemins et le logging
├── config.py       → modèles Pydantic + chargement/fusion des profils YAML
├── log.py          → logger persistant par session (fichier + console Rich)
└── engines/
    ├── siril.py    → génère les scripts .ssf et lance siril-cli
    └── graxpert.py → construit et lance les commandes GraXpert
```

Règle de dépendance : `cli` → `pipeline` → `engines` → `config`. Jamais dans l'autre sens.

---

## Limitations connues

- **Débruitage GraXpert sur Apple Silicon** : peut crasher avec un bug `onnxruntime`/`CoreML`
  (`KernelChannels != InputChannels`). Désactivé par défaut dans les templates.
- **StarNet++** : la séparation étoiles/starless fonctionne, mais la recombinaison
  (starless traité + starmask) n'est pas encore implémentée. Désactivé par défaut.
- **macOS uniquement** pour l'instant. Le pipeline devrait fonctionner sur Linux
  avec des chemins d'installation adaptés, mais c'est non testé.

---

## Licence

MIT — voir [LICENSE](LICENSE).

Astro-1 utilise et pilote des logiciels open source (Siril : GPL, GraXpert : MIT)
qui conservent leurs propres licences.