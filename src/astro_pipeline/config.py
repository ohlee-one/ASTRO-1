"""Loading and validation of YAML profiles.

Deux fichiers YAML sont chargés puis fusionnés en un seul objet `Profile` :
  - un profil "setup"  : le matériel  (profiles/setups/<nom>.yaml)
  - un profil "target" : la cible     (profiles/targets/<nom>.yaml)

Pydantic valide les valeurs au chargement. Si une clé est absente du YAML, la
valeur par défaut définie ici est utilisée. Si une valeur est aberrante
(exemple : strength=5.0 alors que le maximum est 1.0), l'erreur est levée
immédiatement, avant de lancer le moindre traitement.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

# Racine du projet = deux niveaux au-dessus de ce fichier
# (src/astro_pipeline/config.py -> src/astro_pipeline -> src -> racine)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILES_DIR = PROJECT_ROOT / "profiles"


class ProfileNotFoundError(Exception):
    """Levée quand un fichier de profil n'existe pas."""


# ---------------------------------------------------------------------------
# Modèles du profil SETUP
# ---------------------------------------------------------------------------


class Sensor(BaseModel):
    color: bool = True
    bayer_pattern: str = "auto"
    equalize_cfa: bool = True


class Optics(BaseModel):
    focal_length_mm: float | None = None
    aperture_mm: float | None = None
    pixel_size_um: float | None = None


class Folders(BaseModel):
    lights: str = "lights"
    darks: str = "darks"
    flats: str = "flats"
    biases: str = "biases"


class Calibration(BaseModel):
    use_biases: bool = True
    cosmetic_correction: bool = True

    # true  = les dossiers darks/ et flats/ contiennent DÉJÀ des masters
    #         empilés (cas de l'ASIAIR, de NINA avec master builder, etc.).
    #         On les utilise directement, sans les réempiler.
    # false = les dossiers contiennent les brutes individuelles, à empiler.
    use_premade_masters: bool = False


class SetupProfile(BaseModel):
    name: str
    sensor: Sensor = Field(default_factory=Sensor)
    optics: Optics = Field(default_factory=Optics)
    folders: Folders = Field(default_factory=Folders)
    calibration: Calibration = Field(default_factory=Calibration)


# ---------------------------------------------------------------------------
# Modèles du profil TARGET
# ---------------------------------------------------------------------------


class Stacking(BaseModel):
    rejection: Literal["winsorized", "linear", "percentile", "none"] = "winsorized"
    # ge / le = bornes minimum et maximum acceptées
    sigma_low: float = Field(default=3.0, ge=0.1, le=10.0)
    sigma_high: float = Field(default=3.0, ge=0.1, le=10.0)
    normalization: Literal["addscale", "additive", "mul", "none"] = "addscale"


class Registration(BaseModel):
    min_stars: int = Field(default=10, ge=3)


class Processing(BaseModel):
    """Mode de traitement : couleur classique, bande étroite, star trails, ou meteors.

    - "rgb"       : dématriçage classique -> une seule image couleur.
    - "haoiii"    : PAS de dématriçage. Les couches Ha (pixels rouges) et OIII
                    (pixels verts/bleus) sont extraites de la matrice de Bayer,
                    puis empilées séparément. C'est le traitement adapté aux
                    filtres dual-band type L-eXtreme, L-Ultimate, Duo-Band.
    - "startrails": Pas de calibration, pas d'alignement. Les photos sont
                    converties en FITS puis empilées par maximum pixel-wise
                    (chaque pixel = la valeur la plus élevée de toutes les
                    frames). Les étoiles tracent leurs trajectoires.
    - "meteors"   : Pour les pluies de météores (Perséides, Géminides...).
                    Registration (alignement sur les étoiles) puis empilement
                    par maximum. Les étoiles sont ponctuelles (pas de star
                    trails) et les météores restent visibles car ils
                    n'apparaissent qu'une seule fois, à des positions
                    aléatoires non alignees. Pas de calibration.
    """

    mode: Literal["rgb", "haoiii", "startrails", "meteors"] = "rgb"

    # Le Ha ne provient que d'1 pixel sur 4 : il sort en demi-résolution,
    # alors que l'OIII sort en pleine résolution. Il faut les remettre à la
    # même taille, sinon l'alignement entre les deux échouera ensuite.
    #   "ha"   : agrandit le Ha x2 (garde la résolution maximale)
    #   "oiii" : réduit l'OIII de moitié (plus rapide, perd en résolution)
    #   "none" : ne touche à rien (les deux images auront des tailles différentes)
    resample: Literal["ha", "oiii", "none"] = "ha"

    # Après empilement, ajuste l'échelle de l'OIII sur celle du Ha pour que
    # les deux couches soient combinables sans dominante.
    linear_match: bool = True
    linear_match_low: float = 0.0
    linear_match_high: float = 0.92


class BackgroundExtraction(BaseModel):
    enabled: bool = True
    smoothing: float = Field(default=0.1, ge=0.0, le=1.0)
    correction: Literal["subtraction", "division"] = "subtraction"

    # Utiliser l'extraction de gradient native de Siril (seqsubsky) au lieu
    # de GraXpert. Plus efficace pour les gradients complexes en ciel pollué.
    # Applique le retrait de gradient sur chaque pose calibrée AVANT empilement.
    # GraXpert est alors désactivé (le retrait post-empilement n'est plus nécessaire).
    use_siril_subsky: bool = False
    subsky_method: Literal["rbf", "poly"] = "poly"
    subsky_degree: int = Field(default=1, ge=0, le=4)
    subsky_samples: int = Field(default=20, ge=5, le=100)
    subsky_tolerance: float = Field(default=1.0, ge=0.0, le=10.0)
    subsky_smooth: float = Field(default=0.5, ge=0.0, le=1.0)

    # Subsky post-empilement (sur l'image empilée, avant stretch).
    # Paramètres séparés car le gradient résiduel après empilement peut
    # nécessiter une méthode différente (RBF vs poly).
    post_subsky_method: Literal["rbf", "poly"] = "rbf"
    post_subsky_degree: int = Field(default=2, ge=0, le=4)
    post_subsky_samples: int = Field(default=30, ge=5, le=100)
    post_subsky_tolerance: float = Field(default=1.0, ge=0.0, le=10.0)
    post_subsky_smooth: float = Field(default=0.3, ge=0.0, le=1.0)

    # Utiliser GraXpert (modèle IA) pour l'extraction de fond post-empilement
    # au lieu du subsky Siril. Le modèle IA distingue mieux la galaxie du
    # gradient, ce qui est crucial pour les gradients complexes en ciel pollué.
    # Nécessite que le modèle bge de GraXpert soit téléchargé (lancer le GUI
    # une fois). Le seqsubsky sur les poses individuelles reste actif.
    use_graxpert_post_stack: bool = False


class Denoise(BaseModel):
    enabled: bool = True
    strength: float = Field(default=0.5, ge=0.0, le=1.0)
    batch_size: int = Field(default=4, ge=1, le=32)

    # Débruitage natif Siril (algorithme NL-Bayes, non-local Bayesian).
    # Ne nécessite pas de modèle IA. Appliqué après le stretch.
    # -mod : modulation (0 = tout débruité, 1 = inchangé). 0.7 = doux.
    siril_denoise: bool = False
    siril_mod: float = Field(default=0.7, ge=0.0, le=1.0)


class StarNet(BaseModel):
    """Séparation étoiles / starless via StarNet++ (intégré à Siril).

    Le principe du « starless processing » :
      1. On sépare l'image en deux : starless (sans étoiles) + star_mask.
      2. On traite le starless (stretch, couleur, sharpen...) sans abîmer les étoiles.
      3. On réinjecte les étoiles à la fin, éventuellement réduites.

    Ça évite que les étoiles « bavent » pendant le stretch et qu'elles
    saturent pendant les ajustements de couleur.
    """

    enabled: bool = True

    # Upscale 2× avant StarNet pour les images aux étoiles très fines
    # (utile avec un capteur à petits photosites). Plus lent.
    upscale: bool = False

    # Pour les images LINEAR (pas encore étirées), Siril peut faire un
    # pré-stretch interne avant StarNet puis restituer en linéaire.
    # Dans notre pipeline, le stretch est fait AVANT StarNet, donc on
    # laisse à False par défaut. Mettre à True uniquement si tu utilises
    # StarNet sur une image encore linéaire.
    stretch_linear: bool = False


class StarReduction(BaseModel):
    """Réduction de la taille des étoiles sur la couche starless.

    La méthode Moran : on dilate légèrement le masque d'étoiles, puis on
    le soustrait du starless de façon contrôlée. Les étoiles rapetissent
    sans disparaître. Le résultat est recombiné avec le starless traité.
    """

    enabled: bool = True
    # Force de la réduction, entre 0 (aucune) et 1 (agressif).
    # 0.5 est un bon point de départ pour des étoiles déjà propres.
    amount: float = Field(default=0.5, ge=0.0, le=1.0)


class Stretch(BaseModel):
    """Passage du linéaire au non-linéaire (l'image devient visible).

    Trois méthodes disponibles :
      - "autostretch" : détection automatique des paramètres (simple).
      - "asinh"       : stretch arcsinh manuel, plus doux pour les faibles
                        nébulosités. Préserve mieux la luminosité L*a*b*.
      - "ght"         : Generalized Hyperbolic Stretch (GHS). Permet de
                        contrôler précisément où le stretch agit (symmetry
                        point), de protéger les étoiles (HP) et les ombres
                        (LP). C'est l'outil préféré de la communauté astro
                        pour les images difficiles (ciel pollué, galaxies).
    """

    enabled: bool = True
    method: Literal["autostretch", "asinh", "ght"] = "autostretch"

    # --- Paramètres autostretch ---
    # Point de coupe des ombres, en écarts-types du pic principal.
    # Plus négatif = on garde plus de détails dans les ombres.
    shadows_clip: float = Field(default=-2.8, ge=-10.0, le=0.0)
    # Luminosité cible du fond, dans [0, 1]. 0.25 = rendu équilibré.
    target_bg: float = Field(default=0.25, ge=0.0, le=1.0)
    # Stretch lié : mêmes paramètres pour tous les canaux (préserve la balance
    # des blancs). Non-lié = chaque canal est étiré séparément (plus agressif).
    linked: bool = True

    # --- Paramètres asinh ---
    # Force du stretch, typiquement entre 1 et 1000.
    stretch_factor: float = Field(default=100.0, ge=1.0, le=1000.0)
    # Offset du point noir, dans [0, 1].
    offset: float = Field(default=0.0, ge=0.0, le=1.0)
    # Utilise les poids de luminance de l'œil humain (préserve la clarté).
    human: bool = True

    # --- Paramètres GHS (ght) ---
    # Force du stretch, entre 0 et 10. Plus élevé = stretch plus agressif.
    ghs_d: float = Field(default=3.0, ge=0.0, le=10.0)
    # Symmetry point (SP), entre 0 et 1. Point où le stretch est le plus intense.
    # Pour les galaxies : 0.2-0.3 (mid-tones). Pour les nébuleuses : 0.1-0.2.
    ghs_sp: float = Field(default=0.2, ge=0.0, le=1.0)
    # Highlight protection (HP), entre 0 et 1. Protège les étoiles du bloat.
    # 0.7 = protection modérée, 0.85 = protection forte.
    ghs_hp: float = Field(default=0.75, ge=0.0, le=1.0)
    # Local protection (LP), entre 0 et SP. Zone linéaire préservant les ombres.
    # Évite de remonter le bruit du background.
    ghs_lp: float = Field(default=0.0, ge=0.0, le=1.0)
    # B (focal), entre -5 et 15. Contrôle la largeur du stretch autour de SP.
    # 13 = très focalisé (défaut Siril). Valeurs plus basses = stretch plus large.
    ghs_b: float = Field(default=13.0, ge=-5.0, le=15.0)


class HaOIIIComposition(BaseModel):
    """Recombinaison des couches Ha et OIII en une image couleur.

    Le Ha (hydrogène) va dans le canal rouge.
    L'OIII (oxygène) va dans le vert ET le bleu (couleur cyan).
    On peut aussi ajouter le Ha comme luminance pour renforcer les détails.

    Cette étape n'a de sens qu'en mode haoiii. En mode rgb, Siril produit
    déjà une image couleur directement.
    """

    enabled: bool = True
    # Injecte le Ha comme couche de luminance (LRGB au lieu de RGB simple).
    # Le Ha contient généralement les détails les plus fins des nébuleuses.
    use_ha_as_luminance: bool = True


class Sharpening(BaseModel):
    """Renforcement des détails (sharpening) après empilement.

    Deux méthodes :
      - "unsharp"  : masque flou gaussien, simple et efficace.
      - "wavelet"  : reconstruction par ondelettes à trous, plus fin et
                     contrôlable couche par couche.
    """

    enabled: bool = True
    method: Literal["unsharp", "wavelet"] = "unsharp"

    # --- Paramètres unsharp ---
    # Sigma du gaussien (taille du flou). 0.5 à 2.0 typiquement.
    sigma: float = Field(default=1.0, ge=0.1, le=10.0)
    # Quantité de mélange. 0.5 = modéré, 1.0 = fort.
    amount: float = Field(default=0.5, ge=0.0, le=3.0)

    # --- Paramètres wavelet ---
    # Nombre de couches d'ondelettes (1 à 7).
    layers: int = Field(default=4, ge=1, le=7)
    # Type de fonction : 1 = linéaire, 2 = B-spline. B-spline = plus doux.
    wavelet_type: Literal["linear", "bspline"] = "bspline"
    # Poids de reconstruction pour chaque couche. Les premières couches
    # contiennent les détails fins, les dernières les grands structures.
    # Une liste typique : [1.0, 0.7, 0.3, 0.1] pour renforcer surtout
    # les détails fins. Siril complète automatiquement avec 0.0 pour les
    # couches manquantes.
    weights: list[float] = Field(default_factory=lambda: [1.0, 0.7, 0.3, 0.1])


class Color(BaseModel):
    """Ajustements de couleur appliqués après le stretch.

    - rmgreen : supprime la dominante verte (SCNR, comme PixInsight/HLVG).
    - saturation : booste la saturation des couleurs via la commande `satu`
      de Siril, qui permet de ne saturer que les pixels au-dessus d'un seuil
      (pour ne pas amplifier le bruit du fond de ciel).
    """

    enabled: bool = True

    # Calibration photométrique de couleur (PCC) : compare les couleurs des
    # étoiles avec un catalogue pour calibrer automatiquement la balance des
    # blancs. Essentiel en ciel pollué où les canaux ont des niveaux différents.
    # Nécessite un plate-solve (fait automatiquement par Siril avec focal/pixel).
    # Désactivé par défaut (nécessite une connexion internet pour les catalogues).
    photometric_cc: bool = False

    # Suppression de la dominante verte. Très utile en ciel profond,
    # surtout après stretch où le vert résiduel ressort.
    rmgreen: bool = True
    # Type de SCNR : "average" ou "maximum". "average" est plus doux.
    rmgreen_type: Literal["average", "maximum"] = "average"

    # Boost de saturation via la commande `satu` de Siril.
    # 0.0 = inchangé, 0.5 = +50%, 1.0 = +100% (x2).
    saturation_boost: float = Field(default=0.8, ge=0.0, le=3.0)
    # Seuil de fond de ciel : facteur de (médiane + sigma). Seuls les pixels
    # au-dessus de ce seuil sont saturés, pour ne pas amplifier le bruit.
    # 1.0 = défaut Siril (doux), 0.0 = désactivé (sature tout).
    saturation_threshold: float = Field(default=1.0, ge=0.0, le=10.0)
    # Plage de teintes à saturer : 6 = toutes (défaut). Voir doc Siril `satu`.
    hue_range: int = Field(default=6, ge=0, le=6)

    # Saturation supplémentaire ciblée sur une plage de teintes précise.
    # Permet de booster une couleur spécifique (ex: le rouge/rose de la
    # nébuleuse Ha) sans toucher au reste. 0.0 = désactivé.
    # Plages : 0=rose-orange, 1=orange-jaune, 2=jaune-cyan, 3=cyan,
    # 4=cyan-magenta, 5=magenta-rose, 6=toutes.
    target_hue_boost: float = Field(default=0.0, ge=0.0, le=3.0)
    target_hue_range: int = Field(default=5, ge=0, le=6)
    target_hue_threshold: float = Field(default=0.5, ge=0.0, le=10.0)

    # Décaler la teinte du rouge vers le rose/magenta en boostant le canal
    # bleu. Le rose = rouge + bleu, donc en éclaircissant le canal B dans les
    # zones rouges, le Ha tire vers le magenta/rose sans augmenter la
    # saturation. 0.0 = désactivé, 0.1-0.3 = décalage subtil.
    # Appliqué via mtf sur le canal B uniquement.
    blue_shift: float = Field(default=0.0, ge=0.0, le=1.0)

    # Assombrir le fond de ciel sans toucher à la nébuleuse.
    # Applique un MTF avec un point noir (low) légèrement relevé après le
    # stretch. Les pixels sombres (fond sans gaz) sont assombris, les pixels
    # lumineux (nébuleuse) ne sont pas affectés.
    # 0.0 = désactivé, 0.01-0.05 = assombrissement subtil du fond.
    background_clip: float = Field(default=0.0, ge=0.0, le=0.3)


class Export(BaseModel):
    """Format et options de l'image finale livrée.

    Le pipeline garde toujours les FITS intermédiaires. Cette section
    contrôle uniquement le format du livrable final, celui qu'on ouvre
    dans un visualiseur ou qu'on partage.
    """

    enabled: bool = True
    # Format de sortie. TIFF 16-bit = qualité maximale pour post-traitement.
    # PNG 16-bit = bon compromis. JPG = pour partage rapide seulement.
    format: Literal["tiff", "png", "jpg"] = "tiff"
    # Compression TIFF (LZW). Réduit la taille sans perte.
    deflate: bool = True


class Post(BaseModel):
    """Toutes les étapes de post-traitement, dans l'ordre d'exécution.

    Ordre des étapes (du linéaire vers le non-linéaire) :
      1. background_extraction  (sur le linéaire — GraXpert)
      2. denoise                (sur le linéaire — GraXpert IA)
      3. starnet                (séparation avant le stretch)
      4. stretch                (linéaire → non-linéaire)
      5. color                  (rmgreen, saturation — sur le non-linéaire)
      6. sharpening             (sur le starless non-linéaire)
      7. star_reduction         (réduit les étoiles puis recompose)
      8. export                 (FITS → TIFF/PNG)

    En mode haoiii, la recomposition Ha+OIII se fait après le stretch
    individuel de chaque couche.
    """

    background_extraction: BackgroundExtraction = Field(
        default_factory=BackgroundExtraction
    )
    denoise: Denoise = Field(default_factory=Denoise)
    starnet: StarNet = Field(default_factory=StarNet)
    stretch: Stretch = Field(default_factory=Stretch)
    haoiii_composition: HaOIIIComposition = Field(
        default_factory=HaOIIIComposition
    )
    color: Color = Field(default_factory=Color)
    sharpening: Sharpening = Field(default_factory=Sharpening)
    star_reduction: StarReduction = Field(default_factory=StarReduction)
    export: Export = Field(default_factory=Export)


class TargetProfile(BaseModel):
    name: str

    # Coordonnées approximatives de la cible (J2000) pour le plate-solving.
    # Format: "HH:MM:SS" pour RA, "DD:MM:SS" pour Dec.
    # Utilisé par la calibration photométrique (PCC) de Siril.
    # Si vide, la PCC nécessitera que l'image soit déjà plate-solved.
    ra: str = ""
    dec: str = ""

    processing: Processing = Field(default_factory=Processing)
    stacking: Stacking = Field(default_factory=Stacking)
    registration: Registration = Field(default_factory=Registration)
    post: Post = Field(default_factory=Post)


# ---------------------------------------------------------------------------
# Profil fusionné
# ---------------------------------------------------------------------------


class Profile(BaseModel):
    """Le profil complet passé au pipeline : setup + cible réunis."""

    setup: SetupProfile
    target: TargetProfile

    @property
    def label(self) -> str:
        return f"{self.setup.name} / {self.target.name}"


# ---------------------------------------------------------------------------
# Chargement
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        # On liste les profils disponibles : bien plus utile qu'un simple
        # "fichier introuvable" quand on a un doute sur le nom exact.
        available = sorted(p.stem for p in path.parent.glob("*.yaml"))
        raise ProfileNotFoundError(
            f"Profil introuvable : {path}\n"
            f"Profils disponibles dans {path.parent.name}/ : "
            f"{', '.join(available) if available else '(aucun)'}"
        )
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return data or {}


def load_profile(setup_name: str, target_name: str) -> Profile:
    """Charge et valide les deux profils YAML.

    Args:
        setup_name: nom du fichier setup, sans l'extension .yaml
        target_name: nom du fichier target, sans l'extension .yaml

    Raises:
        ProfileNotFoundError: si l'un des fichiers n'existe pas
        ValidationError: si une valeur du YAML est invalide
    """
    setup_data = _read_yaml(PROFILES_DIR / "setups" / f"{setup_name}.yaml")
    target_data = _read_yaml(PROFILES_DIR / "targets" / f"{target_name}.yaml")

    try:
        return Profile(
            setup=SetupProfile(**setup_data),
            target=TargetProfile(**target_data),
        )
    except ValidationError as error:
        # On reformule l'erreur Pydantic, dont le format brut est peu lisible.
        details = "\n".join(
            f"  - {'.'.join(str(part) for part in err['loc'])} : {err['msg']}"
            for err in error.errors()
        )
        raise ValueError(
            f"Un réglage est invalide dans tes profils YAML :\n{details}"
        ) from error


def list_profiles() -> dict[str, list[str]]:
    """Retourne les profils disponibles, pour la commande `astro profiles`."""
    return {
        "setups": sorted(p.stem for p in (PROFILES_DIR / "setups").glob("*.yaml")),
        "targets": sorted(p.stem for p in (PROFILES_DIR / "targets").glob("*.yaml")),
    }
