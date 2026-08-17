"""Siril engine wrapper.

Siril s'utilise ici via `siril-cli`, sa version sans interface graphique.
On lui passe un script `.ssf` : un simple fichier texte, une commande par ligne.

Pourquoi générer un script plutôt que d'utiliser le module Python `sirilpy` ?
Parce que `sirilpy` doit tourner À L'INTÉRIEUR de Siril (lancé depuis son menu
Scripts). Ici on veut l'inverse : notre programme est le chef d'orchestre et
Siril n'est qu'un exécutant. Le script .ssf est aussi lisible et rejouable à la
main, ce qui aide énormément pour comprendre ce qui se passe.

Documentation des commandes : https://siril.readthedocs.io/
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np

from astro_pipeline.config import Profile

# Emplacements où chercher siril-cli, dans l'ordre.
CANDIDATE_PATHS = [
    Path("/Applications/Siril.app/Contents/MacOS/siril-cli"),
    Path("/opt/homebrew/bin/siril-cli"),
    Path("/usr/local/bin/siril-cli"),
]

# Version minimale exigée. Siril refusera le script si l'installation est
# plus ancienne, plutôt que d'échouer à mi-parcours sur une commande inconnue.
MIN_VERSION = "1.2.0"


class SirilNotFoundError(Exception):
    pass


class SirilExecutionError(Exception):
    pass


def find_binary() -> Path | None:
    """Localise siril-cli sur la machine, ou retourne None."""
    for candidate in CANDIDATE_PATHS:
        if candidate.exists():
            return candidate
    # Dernier recours : chercher dans le PATH du shell
    found = shutil.which("siril-cli")
    return Path(found) if found else None


def is_available() -> bool:
    return find_binary() is not None


def version() -> str:
    binary = find_binary()
    if binary is None:
        raise SirilNotFoundError("siril-cli est introuvable.")
    result = subprocess.run(
        [str(binary), "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or result.stderr.strip()


class MasterNotFoundError(Exception):
    pass


def find_premade_master(session_dir: Path, folder: str) -> Path:
    """Localise un master déjà empilé dans un dossier de calibration.

    L'ASIAIR nomme ses fichiers MasterDark_... / MasterFlat_..., mais on
    accepte n'importe quel FITS unique pour rester tolérant.
    """
    directory = session_dir / folder
    candidates = sorted(directory.glob("*.fit")) + sorted(directory.glob("*.fits"))

    if not candidates:
        raise MasterNotFoundError(
            f"Aucun fichier FITS trouvé dans {folder}/.\n"
            f"En mode use_premade_masters, ce dossier doit contenir le master."
        )

    if len(candidates) > 1:
        # Plusieurs fichiers : on privilégie celui qui s'appelle "Master..."
        masters = [p for p in candidates if p.name.lower().startswith("master")]
        if len(masters) == 1:
            return masters[0]
        names = ", ".join(p.name for p in candidates)
        raise MasterNotFoundError(
            f"Plusieurs fichiers dans {folder}/ : {names}\n"
            f"En mode use_premade_masters, il n'en faut qu'un seul.\n"
            f"Si ce sont des brutes à empiler, mets use_premade_masters: false."
        )

    return candidates[0]


def _dir_has_files(session_dir: Path, folder: str) -> bool:
    """Vérifie si un dossier de calibration contient des fichiers FITS."""
    directory = session_dir / folder
    if not directory.is_dir():
        return False
    return bool(
        list(directory.glob("*.fit")) + list(directory.glob("*.fits"))
        + list(directory.glob("*.FIT")) + list(directory.glob("*.FITS"))
    )


def _rejection_arguments(profile: Profile) -> str:
    """Traduit les réglages de rejet du YAML en arguments Siril."""
    stacking = profile.target.stacking
    if stacking.rejection == "none":
        return "mean"

    # Les codes de rejet de Siril : w=winsorized, l=linear fit, p=percentile
    codes = {"winsorized": "w", "linear": "l", "percentile": "p"}
    code = codes[stacking.rejection]
    return f"rej {code} {stacking.sigma_low} {stacking.sigma_high}"


# ---------------------------------------------------------------------------
# Phase 1 : calibration + empilement (linéaire)
# ---------------------------------------------------------------------------


def build_script(session_dir: Path, profile: Profile) -> str:
    """Construit le script .ssf pour la phase 1 : calibration + empilement.

    Cette phase reste en LINÉAIRE. Elle produit :
      - mode rgb    : result.fit (une image couleur calibrée + empilée)
      - mode haoiii : Ha_result.fit + OIII_result.fit (deux images monochromes)
    """

    folders = profile.setup.folders
    calib = profile.setup.calibration
    sensor = profile.setup.sensor

    lines: list[str] = [
        f"requires {MIN_VERSION}",
        "",
        "# ---- Script généré automatiquement par astro-pipeline ----",
        "# ---- Phase 1 : calibration + empilement (linéaire) --------",
        f"# Setup : {profile.setup.name}",
        f"# Cible : {profile.target.name}",
        "",
    ]

    if calib.use_premade_masters:
        # Les masters existent déjà : on les référence directement, sans rien
        # empiler. Les chemins sont relatifs au dossier process/ où Siril
        # travaille, d'où le "../".
        # On passe le nom AVEC extension (.fit) car les fichiers ASIAIR
        # contiennent des points dans le nom (ex: 180.0s) et Siril ne
        # reconstitue pas l'extension automatiquement pour -dark=/-flat=.
        dark_master = find_premade_master(session_dir, folders.darks)
        flat_master = find_premade_master(session_dir, folders.flats)
        dark_reference = f"../{folders.darks}/{dark_master.name}"
        flat_reference = f"../{folders.flats}/{flat_master.name}"
        bias_reference = None
        lines += [
            "# Masters déjà empilés en amont — aucun traitement nécessaire",
            f"#   dark : {dark_master.name}",
            f"#   flat : {flat_master.name}",
        ]
        if calib.use_biases:
            bias_master = find_premade_master(session_dir, folders.biases)
            bias_reference = f"../{folders.biases}/{bias_master.name}"
            lines.append(f"#   bias : {bias_master.name}")
        lines.append("")
        has_dark = True
        has_flat = True
    else:
        has_dark = _dir_has_files(session_dir, folders.darks)
        has_flat = _dir_has_files(session_dir, folders.flats)

        # --- Master bias -----------------------------------------------------
        bias_reference = None
        if calib.use_biases and has_flat:
            lines += [
                "# Master offset (bias)",
                f"cd {folders.biases}",
                "convert bias -out=../process",
                "cd ../process",
                "stack bias rej w 3 3 -nonorm",
                "cd ..",
                "",
            ]
            bias_reference = "bias_stacked"

        flat_reference = None
        # --- Master flat -----------------------------------------------------
        if has_flat:
            lines += [
                "# Master flat",
                f"cd {folders.flats}",
                "convert flat -out=../process",
                "cd ../process",
            ]
            if calib.use_biases:
                # On soustrait l'offset des flats avant de les empiler
                lines.append("calibrate flat -bias=bias_stacked")
                flat_sequence = "pp_flat"
            else:
                flat_sequence = "flat"
            lines += [
                # Les flats se normalisent en multiplicatif, pas en additif
                f"stack {flat_sequence} rej w 3 3 -norm=mul",
                "cd ..",
                "",
            ]
            flat_reference = f"{flat_sequence}_stacked"

        # --- Master dark -----------------------------------------------------
        dark_reference = None
        if has_dark:
            lines += [
                "# Master dark",
                f"cd {folders.darks}",
                "convert dark -out=../process",
                "cd ../process",
                "stack dark rej w 3 3 -nonorm",
                "cd ..",
                "",
            ]
            dark_reference = "dark_stacked"

    # --- Calibration des lights ---------------------------------------------
    processing = profile.target.processing
    extract_mode = processing.mode == "haoiii"

    # Le seuil minimum d'étoiles pour la registration. Siril n'a pas
    # d'argument direct pour imposer un nombre minimum d'étoiles dans la
    # commande register. Ce réglage est conservé dans le profil pour
    # documentation et usage futur (validation post-registration).

    calibrate_options = []
    if dark_reference:
        calibrate_options.append(f"-dark={dark_reference}")
    if flat_reference:
        calibrate_options.append(f"-flat={flat_reference}")
    if bias_reference:
        calibrate_options.append(f"-bias={bias_reference}")
    if calib.cosmetic_correction and dark_reference:
        # Détection des pixels chauds/froids à partir du master dark
        calibrate_options.append("-cc=dark")
    if sensor.color:
        # -cfa indique un capteur à matrice de Bayer
        calibrate_options.append("-cfa")
        if sensor.equalize_cfa:
            calibrate_options.append("-equalize_cfa")
        # ⚠️ POINT CRUCIAL : en mode haoiii on N'AJOUTE PAS -debayer.
        # Le dématriçage interpolerait les couleurs entre pixels voisins et
        # détruirait la séparation Ha / OIII qu'on veut justement extraire.
        if not extract_mode:
            calibrate_options.append("-debayer")

    lines += [
        "# Calibration des poses",
        f"cd {folders.lights}",
        "convert light -out=../process",
        "cd ../process",
    ]
    if calibrate_options:
        # Siril nomme la séquence light_ (avec underscore final)
        lines.append(f"calibrate light_ {' '.join(calibrate_options)}")
    else:
        lines.append("# Pas de darks/flats — calibration skipée (preview)")
    lines.append("")

    bg_settings = profile.target.post.background_extraction
    # Après calibration, la séquence devient pp_light_ (avec underscore)
    # Sans calibration, c'est light_ directement
    light_prefix = "pp_light_" if calibrate_options else "light_"

    # Extraction de gradient native Siril (seqsubsky) sur les poses calibrées.
    # Plus efficace que GraXpert pour les gradients complexes en ciel pollué.
    use_subsky = bg_settings.enabled and bg_settings.use_siril_subsky
    if use_subsky:
        if bg_settings.subsky_method == "rbf":
            subsky_cmd = (
                f"seqsubsky {light_prefix} -rbf -samples={bg_settings.subsky_samples} "
                f"-tolerance={bg_settings.subsky_tolerance} -smooth={bg_settings.subsky_smooth}"
            )
        else:
            subsky_cmd = (
                f"seqsubsky {light_prefix} {bg_settings.subsky_degree} "
                f"-samples={bg_settings.subsky_samples} "
                f"-tolerance={bg_settings.subsky_tolerance} -smooth={bg_settings.subsky_smooth}"
            )
        lines += [
            f"# Extraction de gradient (seqsubsky Siril {bg_settings.subsky_method} — sur chaque pose calibrée)",
            subsky_cmd,
            "",
        ]
        # Après seqsubsky, la séquence devient bkg_<light_prefix>
        light_prefix = f"bkg_{light_prefix}"

    rejection = _rejection_arguments(profile)
    normalization = profile.target.stacking.normalization

    if extract_mode:
        lines += _haoiii_lines(profile, rejection, normalization, light_prefix)
    else:
        lines += _rgb_lines(rejection, normalization, light_prefix)

    return "\n".join(lines) + "\n"


def _rgb_lines(
    rejection: str, normalization: str, light_prefix: str = "pp_light"
) -> list[str]:
    """Chaîne classique : une seule image couleur en sortie."""
    return [
        "# Alignement sur les étoiles",
        f"register {light_prefix}",
        "",
        "# Empilement final",
        f"stack r_{light_prefix} {rejection} "
        f"-norm={normalization} -output_norm -out=../output/result",
        "",
        "# Rechargement du résultat pour vérification",
        "load ../output/result",
        "stat",
        "close",
    ]


def _haoiii_lines(
    profile: Profile,
    rejection: str,
    normalization: str,
    light_prefix: str = "pp_light",
) -> list[str]:
    """Chaîne bande étroite : deux images monochromes Ha et OIII en sortie."""
    processing = profile.target.processing

    # L'option de rééchantillonnage remet Ha et OIII à la même taille.
    resample = ""
    if processing.resample != "none":
        resample = f" -resample={processing.resample}"

    lines = [
        "# Extraction des couches Ha et OIII depuis la matrice de Bayer",
        f"seqextract_HaOIII {light_prefix}{resample}",
        "",
        "# --- Couche Ha ---",
        f"register Ha_{light_prefix}",
        f"stack r_Ha_{light_prefix} {rejection} "
        f"-norm={normalization} -output_norm -out=../output/Ha_result",
        "",
        "# --- Couche OIII ---",
        f"register OIII_{light_prefix}",
        f"stack r_OIII_{light_prefix} {rejection} "
        f"-norm={normalization} -output_norm -out=../output/OIII_result",
        "",
    ]

    if processing.linear_match:
        lines += [
            "# Alignement des niveaux de l'OIII sur ceux du Ha",
            "load ../output/OIII_result",
            f"linear_match ../output/Ha_result "
            f"{processing.linear_match_low} {processing.linear_match_high}",
            "save ../output/OIII_result",
            "",
        ]

    lines.append("close")
    return lines


# ---------------------------------------------------------------------------
# Phase 2 : post-traitement non-linéaire (stretch, couleur, étoiles, sharpen)
# ---------------------------------------------------------------------------


def _stretch_lines(profile: Profile) -> list[str]:
    """Génère les lignes de commande pour le stretch (linéaire → non-linéaire).

    L'image doit être chargée (load) avant l'appel, et sauvegardée (save) après.
    """
    stretch = profile.target.post.stretch
    if not stretch.enabled:
        return []

    if stretch.method == "autostretch":
        # -linked : mêmes paramètres pour tous les canaux (préserve la balance
        # des blancs). Sans -linked, chaque canal est étiré séparément.
        linked_flag = "-linked" if stretch.linked else ""
        return [
            "# Stretch automatique (linéaire → non-linéaire)",
            f"autostretch {linked_flag} {stretch.shadows_clip} {stretch.target_bg}",
        ]

    if stretch.method == "ght":
        # GHS : on utilise autoghs qui calcule SP automatiquement (plus fiable
        # que ght qui stretch depuis 0 et écrase le background).
        # autoghs: shadowsclip = k (SP = k.sigma de la médiane), D = force.
        linked_flag = "-linked" if stretch.linked else ""
        cmd = (
            f"autoghs {linked_flag} {stretch.shadows_clip} {stretch.ghs_d} "
            f"-b={stretch.ghs_b} -hp={stretch.ghs_hp} -lp={stretch.ghs_lp}"
        )
        return [
            "# Stretch GHS (Generalized Hyperbolic Stretch)",
            cmd,
        ]

    # asinh : stretch arcsinh manuel, plus doux pour les faibles nébulosités.
    human_flag = "-human" if stretch.human else ""
    return [
        "# Stretch arcsinh (douillet pour les faibles signaux)",
        f"asinh {human_flag} {stretch.stretch_factor} {stretch.offset}",
    ]


def _color_lines(profile: Profile) -> list[str]:
    """Génère les lignes pour rmgreen + saturation.

    L'image doit être chargée avant l'appel, sauvegardée après.
    """
    color = profile.target.post.color
    if not color.enabled:
        return []

    lines: list[str] = []

    if color.rmgreen:
        # rmgreen supprime la dominante verte (SCNR).
        # Sans option, Siril utilise le mode "average" par défaut.
        # rmgreen : supprime la dominante verte (SCNR).
        # Siril attend un type numérique : 0 = average neutral, 1 = maximum
        # neutral, 2 = maximum mask, 3 = additive mask. On utilise 0 ou 1.
        scnr_type = "1" if color.rmgreen_type == "maximum" else "0"
        lines.append(f"# Suppression dominante verte (SCNR {color.rmgreen_type})")
        lines.append(f"rmgreen {scnr_type}")

    if color.saturation_boost > 0:
        # Commande `satu` de Siril : vraie saturation couleur, avec un seuil
        # sur le fond de ciel pour ne pas amplifier le bruit.
        #   satu amount [background_factor [hue_range_index]]
        # amount : 0.5 = +50%, 1.0 = +100%
        # background_factor : facteur de (médiane + sigma), seuil sous lequel
        #   les pixels ne sont pas modifiés. 1.0 = doux, 0 = tout saturer.
        # hue_range_index : 6 = toutes les couleurs (défaut).
        lines.append(
            f"# Boost saturation (satu {color.saturation_boost:.2f}, "
            f"seuil={color.saturation_threshold})"
        )
        lines.append(
            f"satu {color.saturation_boost:.2f} "
            f"{color.saturation_threshold:.1f} {color.hue_range}"
        )

    if color.target_hue_boost > 0:
        # Saturation ciblée sur une plage de teintes précise (ex: magenta-rose
        # pour booster le Ha rouge → rose flashy).
        lines.append(
            f"# Saturation ciblée (teinte {color.target_hue_range}, "
            f"+{color.target_hue_boost:.2f})"
        )
        lines.append(
            f"satu {color.target_hue_boost:.2f} "
            f"{color.target_hue_threshold:.1f} {color.target_hue_range}"
        )

    if color.blue_shift > 0:
        # Décaler le rouge vers le rose/magenta : on éclaircit le canal bleu
        # via un MTF ciblé sur B. En Siril, mid < 0.5 ÉCLAIRIT les midtons,
        # mid > 0.5 les ASSOMBRT. Pour éclaircir le bleu (et tirer le rouge
        # vers le rose = rouge + bleu), il faut mid < 0.5.
        # mid = 0.5 / (1 + blue_shift) : 0.2 → mid≈0.42 (léger), 0.5 → mid≈0.33.
        mid = 0.5 / (1.0 + color.blue_shift)
        lines.append(
            f"# Décalage rouge → rose (éclaircit canal B, mid={mid:.3f})"
        )
        lines.append(f"mtf 0.0 {mid:.3f} 1.0 B")

    if color.background_clip > 0:
        # Assombrir le fond de ciel : MTF avec un point noir relevé.
        # mtf low mid high : low = point noir, mid = 0.5 = neutre (pas de
        # re-stretch des midtons), high = 1.0 = pas de changement des hautes
        # lumières. Seuls les pixels proches du noir (fond sans gaz) sont
        # assombris. Le signal de la nébuleuse n'est pas affecté car il est
        # déjà au-dessus du point noir.
        lines.append(
            f"# Assombrissement du fond (clip {color.background_clip:.3f})"
        )
        lines.append(
            f"mtf {color.background_clip:.3f} 0.5 1.0"
        )

    return lines


def _starnet_lines(profile: Profile) -> list[str]:
    """Génère les commandes StarNet pour séparer étoiles et starless.

    StarNet est intégré à Siril via la commande `starnet`. Il faut que
    l'exécutable starnet2/starnet++ soit installé et déclaré dans les
    préférences de Siril.

    L'image doit être chargée avant l'appel. Après starnet :
      - l'image starless est chargée comme image courante
      - le star_mask est sauvegardé dans le dossier de travail
    """
    starnet = profile.target.post.starnet
    if not starnet.enabled:
        return []

    options: list[str] = []
    if starnet.upscale:
        options.append("-upscale")
    if starnet.stretch_linear:
        # -stretch : pré-étire l'image avant StarNet, puis restitue en linéaire.
        # Indispensable sur les images linéaires (non encore étirées).
        options.append("-stretch")

    return [
        "# StarNet++ : séparation étoiles / starless",
        f"starnet {' '.join(options)}".rstrip(),
    ]


def _sharpening_lines(profile: Profile) -> list[str]:
    """Génère les commandes de sharpening (unsharp ou wavelet).

    L'image doit être chargée avant l'appel, sauvegardée après.
    """
    sharp = profile.target.post.sharpening
    if not sharp.enabled:
        return []

    if sharp.method == "unsharp":
        return [
            "# Sharpening : masque flou gaussien (unsharp mask)",
            f"unsharp {sharp.sigma} {sharp.amount}",
        ]

    # wavelet : transformée à trous + reconstruction pondérée.
    # type : 1 = linéaire, 2 = B-spline
    wavelet_type = "1" if sharp.wavelet_type == "linear" else "2"
    # Les poids doivent couvrir toutes les couches ; on complète avec 0.0
    weights = sharp.weights[: sharp.layers]
    while len(weights) < sharp.layers:
        weights.append(0.0)
    weights_str = " ".join(str(w) for w in weights)

    return [
        "# Sharpening : reconstruction par ondelettes",
        f"wavelet {sharp.layers} {wavelet_type}",
        f"wrecons {weights_str}",
    ]


def build_post_script(
    session_dir: Path,
    profile: Profile,
    stacked: list[Path],
) -> str:
    """Construit le script .ssf pour la phase finale : post-traitement non-linéaire.

    En mode haoiii, `stacked` contient l'image recomposée et débruitée
    (composed_linear_denoised.fit), pas les couches Ha/OIII séparées.
    On stretch directement cette image RGB.

    En mode rgb, `stacked` contient l'image empilée (+ éventuellement
    traitée par GraXpert).
    """
    output_dir = session_dir / "output"

    lines: list[str] = [
        f"requires {MIN_VERSION}",
        "",
        "# ---- Script généré automatiquement par astro-pipeline ----",
        "# ---- Phase finale : post-traitement (non-linéaire) ---------",
        f"# Setup : {profile.setup.name}",
        f"# Cible : {profile.target.name}",
        "",
        "# On travaille dans output/ où se trouvent les fichiers.",
        "cd output",
        "",
    ]

    for img in stacked:
        # Dans tous les modes, on sauvegarde le résultat final sous "final".
        # Ça évite les doubles suffixes (final_final) et garantit que
        # run_post trouve bien le fichier attendu (final.tif/png/jpg).
        lines += _single_post_lines(profile, img, output_dir, prefix="final")

    return "\n".join(lines) + "\n"


def _single_post_lines(
    profile: Profile,
    img: Path,
    output_dir: Path,
    prefix: str,
) -> list[str]:
    """Post-traitement d'une seule image (mode RGB ou HaOIII sans recomposition).

    On travaille dans output/ (le cd est fait en haut du script).
    Les fichiers d'entrée s'y trouvent déjà (produits par Siril phase 1 ou
    GraXpert phase 2).

    Si StarNet est activé, on utilise un traitement en 3 couches:
    1. Stretch + StarNet -> starless (galaxie+fond) + starmask (étoiles)
    2. Traitement du starless: denoise agressif, couleur, sharpening
    3. Recombinaison: starless + starmask -> image finale
    """
    starnet = profile.target.post.starnet

    if not starnet.enabled:
        # --- Mode classique (sans séparation de couches) ---
        lines: list[str] = [
            f"# --- Post-traitement : {img.name} ---",
            f"load {img.stem}",
        ]

        # Subsky post-empilement (si GraXpert pas activé)
        bg = profile.target.post.background_extraction
        if bg.enabled and bg.use_siril_subsky and not bg.use_graxpert_post_stack:
            method = bg.post_subsky_method
            lines.append(f"# Extraction gradient résiduel (subsky {method})")
            if method == "rbf":
                lines.append(
                    f"subsky -rbf -samples={bg.post_subsky_samples} "
                    f"-tolerance={bg.post_subsky_tolerance} -smooth={bg.post_subsky_smooth}"
                )
            else:
                lines.append(
                    f"subsky {bg.post_subsky_degree} -samples={bg.post_subsky_samples} "
                    f"-tolerance={bg.post_subsky_tolerance} -smooth={bg.post_subsky_smooth}"
                )

        # PCC
        color = profile.target.post.color
        if color.enabled and color.photometric_cc:
            focal = profile.setup.optics.focal_length_mm
            pixelsize = profile.setup.optics.pixel_size_um
            ra = profile.target.ra
            dec = profile.target.dec
            lines.append("# Plate-solving + calibration photométrique (PCC)")
            if ra and dec:
                lines.append(f"platesolve {ra} {dec} -focal={focal} -pixelsize={pixelsize} -noflip")
                lines.append("pcc")
            else:
                lines.append(f"pcc -focal={focal} -pixelsize={pixelsize}")

        # Denoise linéaire
        denoise = profile.target.post.denoise
        if denoise.siril_denoise:
            lines.append(f"# Débruitage NL-Bayes (mod={denoise.siril_mod}, linéaire)")
            lines.append(f"denoise -mod={denoise.siril_mod}")

        # Stretch
        lines += _stretch_lines(profile)

        # Subsky post-stretch : retire le gradient résiduel (rouge diffus)
        # sur l'image non-linéaire. Crucial pour les filtres tri-bande souples
        # (L-eNhance) qui laissent passer plus de pollution lumineuse.
        bg = profile.target.post.background_extraction
        if bg.enabled and bg.use_graxpert_post_stack:
            method = bg.post_subsky_method
            lines.append(f"# Extraction gradient résiduel post-stretch (subsky {method})")
            if method == "rbf":
                lines.append(
                    f"subsky -rbf -samples={bg.post_subsky_samples} "
                    f"-tolerance={bg.post_subsky_tolerance} -smooth={bg.post_subsky_smooth}"
                )
            else:
                lines.append(
                    f"subsky {bg.post_subsky_degree} -samples={bg.post_subsky_samples} "
                    f"-tolerance={bg.post_subsky_tolerance} -smooth={bg.post_subsky_smooth}"
                )

        # Couleur + sharpening
        lines += _color_lines(profile)
        lines += _sharpening_lines(profile)
        lines.append(f"save {prefix}")
        lines.append("")
        lines += _export_lines(profile, output_dir / prefix)
        return lines

    # --- Mode 4 couches (avec StarNet + GraXpert sur starless) ---
    # Phase 1: stretch + StarNet -> starless (galaxie+fond) + starmask (étoiles)
    # Phase 2 (Python/GraXpert): GraXpert extrait le fond du starless
    # Phase 3 (Python): débruitage fond + recombinaison
    lines: list[str] = [
        f"# --- Post-traitement 4 couches : {img.name} ---",
        f"# Phase 1 : Stretch + StarNet",
        f"load {img.stem}",
    ]

    # Pas de débruitage linéaire : il modifie la distribution des données
    # et rend autoghs instable (image noire). Le débruitage se fait après
    # le stretch (comme dans la version "nette amélioration").

    # Stretch GHS (autoghs) : EXACTEMENT les paramètres du "fond parfait".
    # D=3.0, HP=0.75, LP=0.02, shadows_clip=-2.0, B=13.0.
    lines += _stretch_lines(profile)

    # Débruitage NL-Bayes après le stretch (comme dans la version qui marchait).
    denoise = profile.target.post.denoise
    if denoise.siril_denoise:
        lines.append(f"# Débruitage NL-Bayes + DA3D (mod={denoise.siril_mod})")
        lines.append(f"denoise -mod={denoise.siril_mod} -da3d")

    # StarNet : sépare l'image étirée en starless + star_mask
    lines.append("# Configuration StarNet (chemin de l'exécutable)")
    lines.append("set core.starnet_exe=/usr/local/bin/starnet2")
    lines.append("# StarNet : séparation étoiles / starless")
    starnet_opts: list[str] = []
    if starnet.upscale:
        starnet_opts.append("-upscale")
    if starnet.stretch_linear:
        starnet_opts.append("-stretch")
    lines.append(f"starnet {' '.join(starnet_opts)}".rstrip())
    starmask_name = f"starmask_{img.stem}"
    lines.append(f"# Sauvegarde du starless (image courante après starnet)")
    lines.append(f"save {prefix}_starless")
    lines.append(f"# Chargement et sauvegarde du star_mask sous un nom fixe")
    lines.append(f"load {starmask_name}")
    lines.append(f"save {prefix}_starmask")
    lines.append("")
    lines.append(f"# Phase 2 et 3 : GraXpert + recombinaison (faite en Python)")
    lines.append(f"# GraXpert extrait le fond du starless, puis recombinaison Python")
    lines.append("")
    return lines


def _export_lines(profile: Profile, fits_path: Path) -> list[str]:
    """Génère les commandes d'export (FITS → TIFF/PNG/JPG).

    On travaille dans output/ ; l'export se fait avec des noms simples.
    """
    export = profile.target.post.export
    if not export.enabled:
        return []

    stem = fits_path.stem

    lines: list[str] = ["# --- Export final ---"]

    if export.format == "tiff":
        deflate_flag = "-deflate" if export.deflate else ""
        lines.append(f"load {stem}")
        lines.append(f"savetif {stem} {deflate_flag}".rstrip())
    elif export.format == "png":
        lines.append(f"load {stem}")
        lines.append(f"savepng {stem}")
    elif export.format == "jpg":
        lines.append(f"load {stem}")
        lines.append(f"savejpg {stem}")

    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Exécution
# ---------------------------------------------------------------------------


def expected_outputs(session_dir: Path, profile: Profile) -> list[Path]:
    """Fichiers que Siril doit produire en phase 1, selon le mode de traitement."""
    output_dir = session_dir / "output"
    if profile.target.processing.mode == "haoiii":
        return [output_dir / "Ha_result.fit", output_dir / "OIII_result.fit"]
    return [output_dir / "result.fit"]


def build_startrails_script(session_dir: Path, profile: Profile) -> str:
    """Construit le script .ssf pour le mode star trails.

    Le mode star trails est fondamentalement différent du pipeline classique :
      - Pas de calibration (pas de darks/flats/biases)
      - Pas de registration (les étoiles DOIVENT tracer leurs trajectoires)
      - Empilement par maximum pixel-wise (lighten blend)
      - Le reste (stretch, couleur, export) est identique au mode rgb

    Le script produit result.fit dans output/.
    """
    folders = profile.setup.folders
    sensor = profile.setup.sensor

    lines: list[str] = [
        f"requires {MIN_VERSION}",
        "",
        "# ---- Script généré automatiquement par astro-pipeline ----",
        "# ---- Mode star trails : conversion + empilement par max ---",
        f"# Setup : {profile.setup.name}",
        f"# Cible : {profile.target.name}",
        "",
        "# Conversion des RAW en séquence FITS",
        f"cd {folders.lights}",
        "convert light -out=../process",
        "cd ../process",
        "",
    ]

    # Dématriçage si capteur couleur
    calibrate_options = []
    if sensor.color:
        calibrate_options.append("-cfa")
        if sensor.equalize_cfa:
            calibrate_options.append("-equalize_cfa")
        calibrate_options.append("-debayer")

    if calibrate_options:
        lines += [
            "# Dématriçage (capteur couleur)",
            f"calibrate light_ {' '.join(calibrate_options)}",
            "",
        ]
        light_prefix = "pp_light_"
    else:
        light_prefix = "light_"

    # Empilement par MAXIMUM (pas de registration, pas de normalisation)
    # Chaque pixel = la valeur la plus élevée de toutes les frames.
    # C'est ce qui crée les traînées d'étoiles.
    lines += [
        "# Empilement par maximum (lighten blend) — pas de registration",
        f"stack {light_prefix} max -out=../output/result",
        "",
        "# Rechargement du résultat pour vérification",
        "load ../output/result",
        "stat",
        "close",
    ]

    return "\n".join(lines) + "\n"


def run_startrails(
    session_dir: Path, profile: Profile, dry_run: bool = False
) -> list[Path]:
    """Exécute le mode star trails : conversion + empilement par max.

    Retourne la liste des fichiers empilés (un seul : result.fit).
    """
    binary = find_binary()
    if binary is None:
        raise SirilNotFoundError(
            "siril-cli est introuvable.\n"
            "Installe-le avec :  brew install --cask siril\n"
        )

    process_dir = session_dir / "process"
    output_dir = session_dir / "output"
    process_dir.mkdir(exist_ok=True)
    output_dir.mkdir(exist_ok=True)

    script_path = process_dir / "generated.ssf"
    script_path.write_text(build_startrails_script(session_dir, profile), encoding="utf-8")

    results = [output_dir / "result.fit"]

    if dry_run:
        return results

    command = [str(binary), "-d", str(session_dir), "-s", str(script_path)]
    process = subprocess.run(command, capture_output=True, text=True, check=False)

    if process.returncode != 0:
        raise SirilExecutionError(
            f"Siril s'est arrêté avec le code {process.returncode} "
            f"(star trails : conversion + empilement).\n"
            f"Script exécuté : {script_path}\n"
            f"--- Sortie Siril ---\n{process.stdout[-3000:]}\n{process.stderr[-2000:]}"
        )

    missing = [path for path in results if not path.exists()]
    if missing:
        names = ", ".join(path.name for path in missing)
        raise SirilExecutionError(
            f"Siril s'est terminé sans erreur mais ces fichiers manquent : {names}\n"
            f"{process.stdout[-3000:]}"
        )

    return results


def build_meteors_script(session_dir: Path, profile: Profile) -> str:
    """Construit le script .ssf pour le mode meteors (pluies de météores).

    Le mode meteors utilise une approche par soustraction pour isoler les
    météores du fond de ciel et des star trails :

      1. Conversion des RAW + dématriçage
      2. Stack MAXIMUM : contient tout (étoiles trailed + météores + fond)
      3. Stack MÉDIAN : contient le fond stable (les météores, rares,
         sont éliminés par le médian)

    La différence (max - médian) contient les météores + du bruit résiduel
    des star trails. Un seuillage statistique (p99.9) isole les météores.

    Pas de registration : sur trépied fixe en pose longue, les étoiles sont
    déjà trailed dans chaque frame et la registration Siril échoue sur la
    plupart des frames. L'approche par soustraction rend la registration
    inutile : les star trails sont similaires entre le max et le médian,
    donc s'annulent dans la différence.

    Le script produit stack_max.fit et stack_med.fit dans output/.
    """
    folders = profile.setup.folders
    sensor = profile.setup.sensor

    lines: list[str] = [
        f"requires {MIN_VERSION}",
        "",
        "# ---- Script généré automatiquement par astro-pipeline ----",
        "# ---- Mode meteors : stack max + stack médian (soustraction) ---",
        f"# Setup : {profile.setup.name}",
        f"# Cible : {profile.target.name}",
        "",
        "# Conversion des RAW en séquence FITS",
        f"cd {folders.lights}",
        "convert light -out=../process",
        "cd ../process",
        "",
    ]

    # Dématriçage si capteur couleur
    calibrate_options = []
    if sensor.color:
        calibrate_options.append("-cfa")
        if sensor.equalize_cfa:
            calibrate_options.append("-equalize_cfa")
        calibrate_options.append("-debayer")

    if calibrate_options:
        lines += [
            "# Dématriçage (capteur couleur)",
            f"calibrate light_ {' '.join(calibrate_options)}",
            "",
        ]
        light_prefix = "pp_light_"
    else:
        light_prefix = "light_"

    # Deux stacks : max (tout) et médian (fond seul, sans météores)
    lines += [
        "# Stack MAXIMUM : contient tout (étoiles trailed + météores + fond)",
        f"stack {light_prefix} max -out=../output/stack_max",
        "",
        "# Stack MÉDIAN : fond stable (météores éliminés par le médian)",
        f"stack {light_prefix} med -out=../output/stack_med",
        "",
        "close",
    ]

    return "\n".join(lines) + "\n"


def run_meteors(
    session_dir: Path, profile: Profile, dry_run: bool = False
) -> list[Path]:
    """Exécute le mode meteors : conversion + stack max + stack médian.

    Retourne la liste des fichiers empilés (stack_max.fit et stack_med.fit).
    La soustraction et l'isolation des météores se fait dans le pipeline
    (Python), pas ici.
    """
    binary = find_binary()
    if binary is None:
        raise SirilNotFoundError(
            "siril-cli est introuvable.\n"
            "Installe-le avec :  brew install --cask siril\n"
        )

    process_dir = session_dir / "process"
    output_dir = session_dir / "output"
    process_dir.mkdir(exist_ok=True)
    output_dir.mkdir(exist_ok=True)

    script_path = process_dir / "generated.ssf"
    script_path.write_text(build_meteors_script(session_dir, profile), encoding="utf-8")

    results = [output_dir / "stack_max.fit", output_dir / "stack_med.fit"]

    if dry_run:
        return results

    command = [str(binary), "-d", str(session_dir), "-s", str(script_path)]
    process = subprocess.run(command, capture_output=True, text=True, check=False)

    if process.returncode != 0:
        raise SirilExecutionError(
            f"Siril s'est arrêté avec le code {process.returncode} "
            f"(meteors : conversion + stacks).\n"
            f"Script exécuté : {script_path}\n"
            f"--- Sortie Siril ---\n{process.stdout[-3000:]}\n{process.stderr[-2000:]}"
        )

    missing = [path for path in results if not path.exists()]
    if missing:
        names = ", ".join(path.name for path in missing)
        raise SirilExecutionError(
            f"Siril s'est terminé sans erreur mais ces fichiers manquent : {names}\n"
            f"{process.stdout[-3000:]}"
        )

    return results


def final_output_name(profile: Profile) -> str:
    """Nom de base du fichier final produit par la phase finale."""
    if profile.target.processing.mode == "haoiii":
        return "final"
    # En mode rgb, le prefix est le stem de l'image d'entrée (result, result_denoised...)
    # Le save se fait sous ce même stem, donc le nom final = le stem.
    # On retourne "final" pour cohérence — le pipeline passe le bon prefix.
    return "final"


def export_extension(profile: Profile) -> str:
    """Extension du fichier d'export final, selon le format configuré."""
    fmt = profile.target.post.export.format
    return {"tiff": "tif", "png": "png", "jpg": "jpg"}[fmt]


def run(session_dir: Path, profile: Profile, dry_run: bool = False) -> list[Path]:
    """Génère le script de phase 1, l'exécute, et retourne les fichiers empilés.

    Args:
        session_dir: dossier de la session (contient lights/, darks/...)
        profile: profil fusionné setup + cible
        dry_run: si True, écrit le script mais ne lance pas Siril

    Returns:
        Liste des fichiers empilés : un en mode "rgb", deux en "haoiii".
    """
    binary = find_binary()
    if binary is None:
        raise SirilNotFoundError(
            "siril-cli est introuvable.\n"
            "Installe-le avec :  brew install --cask siril\n"
            "Puis vérifie :      ls /Applications/Siril.app/Contents/MacOS/siril-cli"
        )

    process_dir = session_dir / "process"
    output_dir = session_dir / "output"
    process_dir.mkdir(exist_ok=True)
    output_dir.mkdir(exist_ok=True)

    script_path = process_dir / "generated.ssf"
    script_path.write_text(build_script(session_dir, profile), encoding="utf-8")

    results = expected_outputs(session_dir, profile)

    if dry_run:
        # En simulation, on génère aussi le script de post-traitement pour
        # que l'utilisateur puisse le relire avant de lancer pour de vrai.
        post_script_path = process_dir / "post_processing.ssf"
        post_script_path.write_text(
            build_post_script(session_dir, profile, results), encoding="utf-8"
        )
        return results

    # -d : définit le répertoire de travail
    # -s : exécute le script indiqué
    command = [str(binary), "-d", str(session_dir), "-s", str(script_path)]

    process = subprocess.run(command, capture_output=True, text=True, check=False)

    if process.returncode != 0:
        raise SirilExecutionError(
            f"Siril s'est arrêté avec le code {process.returncode} "
            f"(phase 1 : calibration + empilement).\n"
            f"Script exécuté : {script_path}\n"
            f"--- Sortie Siril ---\n{process.stdout[-3000:]}\n{process.stderr[-2000:]}"
        )

    missing = [path for path in results if not path.exists()]
    if missing:
        names = ", ".join(path.name for path in missing)
        raise SirilExecutionError(
            f"Siril s'est terminé sans erreur mais ces fichiers manquent : {names}\n"
            f"Regarde la fin du log ci-dessous pour comprendre :\n"
            f"{process.stdout[-3000:]}"
        )

    return results


def run_post(
    session_dir: Path, profile: Profile, stacked: list[Path], dry_run: bool = False
) -> Path | None:
    """Exécute la phase 2 : post-traitement non-linéaire.

    Args:
        session_dir: dossier de la session
        profile: profil fusionné
        stacked: fichiers empilés produits par la phase 1
        dry_run: si True, écrit le script mais ne lance pas Siril

    Returns:
        Chemin du fichier exporté (TIFF/PNG/JPG), ou None si l'export
        est désactivé. En mode simulation, retourne le chemin attendu.
    """
    binary = find_binary()
    if binary is None:
        raise SirilNotFoundError("siril-cli est introuvable.")

    process_dir = session_dir / "process"
    output_dir = session_dir / "output"

    post_script_path = process_dir / "post_processing.ssf"
    post_script_path.write_text(
        build_post_script(session_dir, profile, stacked), encoding="utf-8"
    )

    # Calculer le chemin d'export attendu
    export_name = final_output_name(profile)
    export_ext = export_extension(profile)
    export_path = output_dir / f"{export_name}.{export_ext}"

    if dry_run:
        return export_path if profile.target.post.export.enabled else None

    command = [str(binary), "-d", str(session_dir), "-s", str(post_script_path)]
    process = subprocess.run(command, capture_output=True, text=True, check=False)

    if process.returncode != 0:
        raise SirilExecutionError(
            f"Siril s'est arrêté avec le code {process.returncode} "
            f"(phase 2 : post-traitement).\n"
            f"Script exécuté : {post_script_path}\n"
            f"--- Sortie Siril ---\n{process.stdout[-3000:]}\n{process.stderr[-2000:]}"
        )

    # Si StarNet est activé, la recombinaison se fait en Python (addmax non scriptable).
    starnet_enabled = profile.target.post.starnet.enabled
    if starnet_enabled:
        starless = output_dir / "final_starless.fit"
        starmask = output_dir / "final_starmask.fit"
        for f in [starless, starmask]:
            if not f.exists():
                raise SirilExecutionError(
                    f"Recombinaison 4 couches : fichier manquant : {f}\n"
                    f"--- Sortie Siril ---\n{process.stdout[-2000:]}"
                )

        from astropy.io import fits
        from scipy.ndimage import median_filter
        from astro_pipeline.engines import graxpert

        # Étape 1: GraXpert sur le starless étiré pour extraire le fond.
        starless_bg, bg_commands = graxpert.run_background_only(
            starless, output_dir, profile, dry_run=dry_run
        )
        if not starless_bg.exists():
            raise SirilExecutionError(
                f"GraXpert n'a pas produit le starless sans fond : {starless_bg}\n"
            )

        # Étape 2: Script Siril pour traiter séparément galaxie et étoiles.
        # - Galaxie (starless_bg): rmgreen pour corriger le vert + background_clip.
        # - Étoiles (starmask): rmgreen pour corriger le vert + léger flou pour rondeur.
        treat_script = process_dir / "treat_layers.ssf"
        color = profile.target.post.color
        treat_lines = [
            f"requires {MIN_VERSION}",
            "",
            "cd output",
            "",
            "# --- Traitement de la galaxie (starless sans fond) ---",
            f"load final_starless_bg",
        ]
        # rmgreen sur la galaxie
        if color.rmgreen:
            scnr_type = "1" if color.rmgreen_type == "maximum" else "0"
            treat_lines.append(f"# Correction du vert sur la galaxie (SCNR {color.rmgreen_type})")
            treat_lines.append(f"rmgreen {scnr_type}")
        # Saturation sur la galaxie (les gaz, sans toucher le fond ni les etoiles)
        if color.saturation_boost > 0:
            treat_lines.append(f"# Saturation des gaz ({color.saturation_boost:.1f})")
            treat_lines.append(f"satu {color.saturation_boost:.2f} {color.saturation_threshold:.1f} {color.hue_range}")
        # Blue shift sur la galaxie : eclaircit le canal bleu uniquement (channel=2)
        # pour faire virer le rouge Ha vers le rose/magenta.
        if color.blue_shift > 0:
            treat_lines.append(f"# Blue shift (rouge -> rose, {color.blue_shift:.2f})")
            treat_lines.append(f"mtf 0.0 {1.0 - color.blue_shift:.2f} 1.0 2")
        # Background clip sur la galaxie
        if color.background_clip > 0:
            treat_lines.append(f"# Assombrissement du fond (clip {color.background_clip:.3f})")
            treat_lines.append(f"mtf {color.background_clip:.3f} 0.5 1.0")
        treat_lines.append("save final_galaxy_proc")
        treat_lines.append("")

        # Sharpening sur la galaxie (révèle les détails des bras spiraux)
        sharp = profile.target.post.sharpening
        if sharp.enabled:
            treat_lines.append(f"# Recharger la galaxie pour sharpening")
            treat_lines.append(f"load final_galaxy_proc")
            if sharp.method == "unsharp":
                treat_lines.append(f"# Sharpening galaxie (unsharp)")
                treat_lines.append(f"unsharp {sharp.sigma} {sharp.amount}")
            treat_lines.append("save final_galaxy_proc")
            treat_lines.append("")

        # --- Traitement des étoiles ---
        treat_lines.append("# --- Traitement des étoiles ---")
        treat_lines.append("load final_starmask")
        # rmgreen sur les étoiles (corrige le vert)
        if color.rmgreen:
            treat_lines.append(f"# Correction du vert sur les étoiles (SCNR)")
            treat_lines.append(f"rmgreen {scnr_type}")
        # Pas de flou gaussien : il fait disparaître les petites étoiles.
        # Les étoiles sont laissées telles quelles pour préserver toutes les
        # étoiles faibles (le starmask de StarNet est déjà propre).
        treat_lines.append("save final_starmask_proc")
        treat_lines.append("")

        treat_script.write_text("\n".join(treat_lines) + "\n", encoding="utf-8")
        cmd_treat = [str(binary), "-d", str(session_dir), "-s", str(treat_script)]
        proc_treat = subprocess.run(cmd_treat, capture_output=True, text=True, check=False)
        if proc_treat.returncode != 0:
            raise SirilExecutionError(
                f"Siril traitement des couches a échoué (code {proc_treat.returncode}).\n"
                f"--- Sortie Siril ---\n{proc_treat.stdout[-2000:]}"
            )

        # Étape 3: Recombinaison Python.
        galaxy_proc = output_dir / "final_galaxy_proc.fit"
        starmask_proc = output_dir / "final_starmask_proc.fit"
        for f in [galaxy_proc, starmask_proc]:
            if not f.exists():
                raise SirilExecutionError(
                    f"Recombinaison : fichier manquant : {f}\n"
                    f"--- Sortie Siril ---\n{proc_treat.stdout[-1000:]}"
                )

        with fits.open(starless) as hdul_sl, fits.open(starless_bg) as hdul_sbg, fits.open(starmask_proc) as hdul_sm:
            data_starless = hdul_sl[0].data.astype("float32")
            data_starless_bg = hdul_sbg[0].data.astype("float32")
            data_starmask = hdul_sm[0].data.astype("float32")

        with fits.open(galaxy_proc) as hdul_gp:
            data_galaxy_proc = hdul_gp[0].data.astype("float32")

        # Le fond = starless - starless_bg (ce que GraXpert a retiré)
        data_bg = data_starless - data_starless_bg
        data_bg = np.clip(data_bg, 0, None)

        # Débruitage du fond: filtre médian large
        data_bg_denoised = np.zeros_like(data_bg)
        for i in range(data_bg.shape[0] if data_bg.ndim == 3 else 1):
            ch = data_bg[i] if data_bg.ndim == 3 else data_bg
            ch_denoised = median_filter(ch, size=25)
            if data_bg.ndim == 3:
                data_bg_denoised[i] = ch_denoised
            else:
                data_bg_denoised = ch_denoised

        # Sauvegarder les 3 couches en FITS separees pour export TIFF.
        hdr = hdul_sl[0].header
        fits.writeto(output_dir / "background.fit", data_bg_denoised.astype(">f4"), hdr, overwrite=True)
        fits.writeto(output_dir / "nebula.fit", data_galaxy_proc.astype(">f4"), hdr, overwrite=True)
        fits.writeto(output_dir / "stars.fit", data_starmask.astype(">f4"), hdr, overwrite=True)

        # Recombinaison: fond lissé + galaxie traitée + étoiles traitées
        combined = data_bg_denoised + data_galaxy_proc
        combined = np.maximum(combined, data_starmask)

        combined = combined.astype(">f4")
        fits.writeto(output_dir / "final.fit", combined, hdr, overwrite=True)

        # Export TIFF des 3 couches + image finale combinee via Siril.
        if profile.target.post.export.enabled:
            export_script = process_dir / "export.ssf"
            if export_ext == "tif":
                export_cmd = "savetif {name} -deflate"
            elif export_ext == "png":
                export_cmd = "savepng {name}"
            else:
                export_cmd = "savejpg {name}"
            export_lines = [
                f"requires {MIN_VERSION}",
                "",
                "cd output",
                "load final",
                export_cmd.format(name=export_name),
                "load background",
                export_cmd.format(name="background"),
                "load nebula",
                export_cmd.format(name="nebula"),
                "load stars",
                export_cmd.format(name="stars"),
            ]
            export_script.write_text("\n".join(export_lines) + "\n", encoding="utf-8")
            cmd_export = [str(binary), "-d", str(session_dir), "-s", str(export_script)]
            proc_export = subprocess.run(cmd_export, capture_output=True, text=True, check=False)
            if proc_export.returncode != 0:
                raise SirilExecutionError(
                    f"Siril export a échoué (code {proc_export.returncode}).\n"
                    f"--- Sortie Siril ---\n{proc_export.stdout[-2000:]}"
                )

    if profile.target.post.export.enabled and not export_path.exists():
        raise SirilExecutionError(
            f"Le fichier d'export final n'a pas été créé : {export_path}\n"
            f"Regarde la fin du log ci-dessous pour comprendre :\n"
            f"{process.stdout[-3000:]}"
        )

    return export_path if profile.target.post.export.enabled else None


# ---------------------------------------------------------------------------
# Recomposition linéaire (entre GraXpert fond de ciel et GraXpert denoise)
# ---------------------------------------------------------------------------


def build_compose_linear_script(
    session_dir: Path,
    profile: Profile,
    processed: list[Path],
) -> str:
    """Construit un script .ssf qui recompose Ha+OIII en RGB LINÉAIRE.

    Cette étape est insérée entre l'extraction du fond de ciel (GraXpert) et
    le débruitage (GraXpert), car le modèle de débruitage IA de GraXpert
    nécessite une image RGB 3 canaux. En mode HaOIII, Ha et OIII sont
    monochromes — il faut donc les recomposer en RGB avant de débruiter.

    Le résultat est un fichier `composed_linear.fit` dans output/.
    """
    output_dir = session_dir / "output"

    # Identifier Ha et OIII parmi les fichiers traités
    ha_path = next((p for p in processed if "Ha" in p.stem), processed[0])
    oiii_path = next((p for p in processed if "OIII" in p.stem), processed[1])

    comp = profile.target.post.haoiii_composition

    lines: list[str] = [
        f"requires {MIN_VERSION}",
        "",
        "# ---- Recomposition Ha+OIII en RGB linéaire ------------------",
        "# Cette étape est nécessaire avant le débruitage IA de GraXpert",
        "# qui requiert 3 canaux. Les couches Ha et OIII sont monochromes.",
        "",
        f"cd output",
        "",
    ]

    if comp.use_ha_as_luminance:
        lines.append(
            f"rgbcomp -lum={ha_path.stem} "
            f"{ha_path.stem} {oiii_path.stem} {oiii_path.stem} "
            f"-out=composed_linear"
        )
    else:
        lines.append(
            f"rgbcomp {ha_path.stem} {oiii_path.stem} {oiii_path.stem} "
            f"-out=composed_linear"
        )

    lines.append("")
    return "\n".join(lines) + "\n"


def run_compose_linear(
    session_dir: Path,
    profile: Profile,
    processed: list[Path],
    dry_run: bool = False,
) -> Path:
    """Exécute la recomposition linéaire Ha+OIII → RGB.

    Returns:
        Chemin du fichier composed_linear.fit dans output/.
    """
    binary = find_binary()
    if binary is None:
        raise SirilNotFoundError("siril-cli est introuvable.")

    process_dir = session_dir / "process"
    output_dir = session_dir / "output"

    script_path = process_dir / "compose_linear.ssf"
    script_path.write_text(
        build_compose_linear_script(session_dir, profile, processed),
        encoding="utf-8",
    )

    composed_path = output_dir / "composed_linear.fit"

    if dry_run:
        return composed_path

    command = [str(binary), "-d", str(session_dir), "-s", str(script_path)]
    process = subprocess.run(command, capture_output=True, text=True, check=False)

    if process.returncode != 0:
        raise SirilExecutionError(
            f"Siril s'est arrêté avec le code {process.returncode} "
            f"(recomposition linéaire).\n"
            f"Script exécuté : {script_path}\n"
            f"--- Sortie Siril ---\n{process.stdout[-3000:]}\n{process.stderr[-2000:]}"
        )

    if not composed_path.exists():
        raise SirilExecutionError(
            f"Le fichier recomposé n'a pas été créé : {composed_path}\n"
            f"Regarde la fin du log ci-dessous pour comprendre :\n"
            f"{process.stdout[-3000:]}"
        )

    return composed_path