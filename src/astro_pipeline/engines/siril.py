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
        lines += [
            "# Masters déjà empilés en amont — aucun traitement nécessaire",
            f"#   dark : {dark_master.name}",
            f"#   flat : {flat_master.name}",
            "",
        ]
    else:
        # --- Master bias -----------------------------------------------------
        if calib.use_biases:
            lines += [
                "# Master offset (bias)",
                f"cd {folders.biases}",
                "convert bias -out=../process",
                "cd ../process",
                "stack bias rej w 3 3 -nonorm",
                "cd ..",
                "",
            ]

        # --- Master flat -----------------------------------------------------
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

        # --- Master dark -----------------------------------------------------
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
        flat_reference = f"{flat_sequence}_stacked"

    # --- Calibration des lights ---------------------------------------------
    processing = profile.target.processing
    extract_mode = processing.mode == "haoiii"

    # Le seuil minimum d'étoiles pour la registration. Siril n'a pas
    # d'argument direct pour imposer un nombre minimum d'étoiles dans la
    # commande register. Ce réglage est conservé dans le profil pour
    # documentation et usage futur (validation post-registration).

    calibrate_options = [
        f"-dark={dark_reference}",
        f"-flat={flat_reference}",
    ]
    if calib.cosmetic_correction:
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
        f"calibrate light {' '.join(calibrate_options)}",
        "",
    ]

    rejection = _rejection_arguments(profile)
    normalization = profile.target.stacking.normalization

    if extract_mode:
        lines += _haoiii_lines(profile, rejection, normalization)
    else:
        lines += _rgb_lines(rejection, normalization)

    return "\n".join(lines) + "\n"


def _rgb_lines(rejection: str, normalization: str) -> list[str]:
    """Chaîne classique : une seule image couleur en sortie."""
    return [
        "# Alignement sur les étoiles",
        "register pp_light",
        "",
        "# Empilement final",
        f"stack r_pp_light {rejection} "
        f"-norm={normalization} -output_norm -out=../output/result",
        "",
        "# Rechargement du résultat pour vérification",
        "load ../output/result",
        "stat",
        "close",
    ]


def _haoiii_lines(
    profile: Profile, rejection: str, normalization: str
) -> list[str]:
    """Chaîne bande étroite : deux images monochromes Ha et OIII en sortie."""
    processing = profile.target.processing

    # L'option de rééchantillonnage remet Ha et OIII à la même taille.
    resample = ""
    if processing.resample != "none":
        resample = f" -resample={processing.resample}"

    lines = [
        "# Extraction des couches Ha et OIII depuis la matrice de Bayer",
        f"seqextract_HaOIII pp_light{resample}",
        "",
        "# --- Couche Ha ---",
        "register Ha_pp_light",
        f"stack r_Ha_pp_light {rejection} "
        f"-norm={normalization} -output_norm -out=../output/Ha_result",
        "",
        "# --- Couche OIII ---",
        "register OIII_pp_light",
        f"stack r_OIII_pp_light {rejection} "
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
    """
    lines: list[str] = [
        f"# --- Post-traitement : {img.name} ---",
        f"load {img.stem}",
    ]

    # Ordre : stretch d'abord (linéaire → non-linéaire), puis StarNet sur
    # l'image non-linéaire, puis couleur et sharpening sur le starless.
    lines += _stretch_lines(profile)
    lines += _starnet_lines(profile)
    lines += _color_lines(profile)
    lines += _sharpening_lines(profile)
    lines.append(f"save {prefix}")
    lines.append("")

    lines += _export_lines(profile, output_dir / prefix)

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