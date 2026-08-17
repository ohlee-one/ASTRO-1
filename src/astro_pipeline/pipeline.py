"""Orchestration : enchaîne les moteurs dans le bon ordre.

Ce module ne sait rien des détails de Siril ou de GraXpert. Il se contente de
les appeler et de gérer les chemins, les vérifications, le logging et l'affichage.

Le pipeline complet :

Mode RGB :
  1. Siril    : calibration, registration, empilement -> FITS linéaire RGB
  2. GraXpert : extraction fond de ciel + débruitage IA (sur le linéaire)
  3. Siril    : stretch, StarNet, couleur, sharpening, export

Mode HaOIII (bande étroite) :
  1. Siril    : calibration, extraction Ha/OIII, empilement -> 2 FITS monochromes
  2. GraXpert : extraction fond de ciel (sur chaque couche monochrome)
  3. Siril    : recomposition Ha+OIII -> RGB linéaire
  4. GraXpert : débruitage IA (sur l'RGB recomposé, nécessite 3 canaux)
  5. Siril    : stretch, StarNet, couleur, sharpening, export

Mode Star trails :
  1. Siril    : conversion RAW + dématriçage + empilement par maximum
  2. Siril    : stretch, couleur, export
  Pas de calibration, pas d'alignement, pas de GraXpert, pas de StarNet.

Mode Météores (Perséides, Géminides...) :
  1. Siril    : conversion RAW + dématriçage + stack max + stack médian
  2. Python   : soustraction (max - médian), seuillage p99.9, combinaison
  3. Siril    : stretch, couleur, export
  Pas de calibration, pas de registration, pas de GraXpert, pas de StarNet.
  Les star trails s'annulent dans la différence (similaires entre max et médian).
  Les météores restent car ils n'apparaissent qu'une fois (absents du médian).

Mode Planétaire (lucky imaging) :
  1. OpenCV   : lecture vidéo SER/AVI + tri par qualité + alignement + stack
  2. Python   : RGB align (dispersion) + sharpening (ondelettes) + export TIFF
  Pas de Siril, pas de GraXpert, pas de StarNet, pas de calibration.

La différence clé : en mode haoiii, le débruitage IA de GraXpert ne peut pas
fonctionner sur les couches monochromes (1 canal). Il faut recomposer en RGB
(3 canaux) avant de débruiter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import astropy.io.fits as fits
import numpy as np
from rich.console import Console

from astro_pipeline.config import Profile
from astro_pipeline.engines import graxpert, planetary, siril
from astro_pipeline.log import SessionLogger

console = Console()


@dataclass
class PipelineResult:
    """Résultat d'une exécution complète du pipeline.

    Attributes:
        stacked: fichiers FITS empilés produits par la phase 1 (linéaires)
        processed: fichiers FITS après GraXpert (fond de ciel + débruitage)
        exported: fichier final exporté (TIFF/PNG/JPG), ou None si désactivé
        scripts: chemins des scripts .ssf générés
        commands: commandes GraXpert exécutées (pour rejouage manuel)
        log_path: chemin du fichier de log de la session
    """
    stacked: list[Path]
    processed: list[Path]
    exported: Path | None
    scripts: list[Path]
    commands: list[list[str]] = field(default_factory=list)
    log_path: Path | None = None


class SessionError(Exception):
    pass


def _isolate_meteors(
    stacked: list[Path], output_dir: Path, dry_run: bool = False
) -> Path | None:
    """Isole les météores par soustraction (max - médian) et seuillage.

    Le stack max contient tout (étoiles trailed + météores + fond).
    Le stack médian contient le fond stable (météores éliminés par le médian
    car ils n'apparaissent qu'une fois sur N frames).

    La différence (max - médian) contient les météores + du bruit résiduel
    des star trails. Un seuillage au p99.9 isole les météores (signaux les
    plus intenses) en éliminant le bruit de fond des trails.

    Le résultat est combiné avec le stack médian (fond de ciel + étoiles)
    pour produire une image naturelle où les météores sont visibles
    par-dessus le ciel étoilé.
    """
    if dry_run or len(stacked) < 2:
        return None

    max_path = stacked[0]  # stack_max.fit
    med_path = stacked[1]  # stack_med.fit

    if not max_path.exists() or not med_path.exists():
        return None

    max_data = fits.getdata(str(max_path)).astype(np.float32)
    med_data = fits.getdata(str(med_path)).astype(np.float32)

    # Différence = max - médian (signal positif uniquement)
    diff = np.maximum(max_data - med_data, 0)

    # Seuillage au p99.9 : garde uniquement les 0.1% les plus brillants
    # Les météores sont des outliers (1 frame sur N), les trails sont diffus
    diff_gray = np.max(diff, axis=0)
    nonzero = diff_gray[diff_gray > 1e-6]
    if len(nonzero) < 100:
        return None
    p999 = np.percentile(nonzero, 99.9)

    # Masquer tout ce qui est en dessous du seuil (bruit de trails)
    meteor_signal = np.where(diff > p999, diff, 0).astype(np.float32)

    # Combiner : fond de ciel (médian) + météores amplifiés
    # ×3 pour rendre les météores bien visibles par-dessus le fond
    combined = med_data + meteor_signal * 3.0

    # Sauver le FITS combiné pour que Siril puisse le stretcher
    result_path = output_dir / "meteors_combined.fit"
    fits.writeto(str(result_path), combined.astype(np.float32), overwrite=True)

    return result_path


def validate_session(session_dir: Path, profile: Profile) -> None:
    """Vérifie que la session a la bonne structure AVANT de lancer quoi que ce soit.

    C'est délibérément fait en premier : mieux vaut échouer en deux secondes
    qu'après quarante minutes d'empilement.

    Les dossiers darks/flats peuvent être vides (preview sans calibration),
    mais le dossier lights doit contenir au moins une image.
    """
    if not session_dir.exists():
        raise SessionError(f"Le dossier de session n'existe pas : {session_dir}")

    folders = profile.setup.folders
    # lights est toujours obligatoire. darks/flats sont créés s'ils manquent.
    required = [folders.lights]
    optional = [folders.darks, folders.flats]
    if profile.setup.calibration.use_biases and not profile.setup.calibration.use_premade_masters:
        required.append(folders.biases)

    missing = [name for name in required if not (session_dir / name).is_dir()]
    if missing:
        raise SessionError(
            f"Dossiers manquants dans {session_dir} : {', '.join(missing)}\n"
            f"Structure attendue : lights (darks/flats optionnels)"
        )

    # Crée les dossiers darks/flats s'ils n'existent pas (Siril les attend)
    for name in optional:
        (session_dir / name).mkdir(exist_ok=True)

    # Vérifie que les dossiers obligatoires ne sont pas vides
    for name in required:
        if not any((session_dir / name).iterdir()):
            raise SessionError(f"Le dossier {name}/ est vide.")


def run(
    session_dir: Path,
    profile: Profile,
    dry_run: bool = False,
) -> PipelineResult:
    """Exécute le pipeline complet sur une session.

    Le flow diffère selon le mode (rgb vs haoiii vs startrails) — voir la docstring du module.
    """
    session_dir = session_dir.expanduser().resolve()
    validate_session(session_dir, profile)

    output_dir = session_dir / "output"
    output_dir.mkdir(exist_ok=True)

    logger = SessionLogger(session_dir, console)
    logger.rule(profile.label)
    logger.info(f"Session : {session_dir}")
    if dry_run:
        logger.warning("Mode simulation — rien ne sera exécuté.")

    mode = profile.target.processing.mode
    is_haoiii = mode == "haoiii"
    is_startrails = mode == "startrails"
    is_meteors = mode == "meteors"
    is_planetary = mode == "planetary"
    mode_label = mode
    if is_haoiii:
        mode_label += " (extraction bande étroite Ha + OIII)"
    elif is_startrails:
        mode_label += " (empilement par maximum, pas de registration)"
    elif is_meteors:
        mode_label += " (soustraction max - médian, météores isolés)"
    elif is_planetary:
        mode_label += " (lucky imaging : tri, alignement, stack, ondelettes)"
    logger.info(f"Mode    : {mode_label}")
    logger.info(f"Log     : {logger.log_path}")

    # === Mode star trails : pipeline simplifié ================================
    # Pas de calibration, pas de GraXpert, pas de StarNet.
    # 1. Siril : conversion + empilement par max
    # 2. Siril : stretch + couleur + export
    if is_startrails:
        total_steps = 2
        step = 0
        scripts: list[Path] = []
        commands: list[list[str]] = []

        # Étape 1 : Siril — conversion + empilement par max
        step += 1
        logger.step(step, total_steps, "Siril", "conversion RAW + empilement par maximum")
        stacked = siril.run_startrails(session_dir, profile, dry_run=dry_run)
        script1 = session_dir / "process" / "generated.ssf"
        scripts.append(script1)
        logger.info(f"      Script : {script1}")
        for path in stacked:
            if dry_run:
                logger.info(f"      → {path.name}")
            else:
                logger.success(path.name)

        # Étape 2 : Siril — stretch + couleur + export
        step += 1
        logger.step(step, total_steps, "Siril", "stretch, couleur, export")
        exported = siril.run_post(session_dir, profile, stacked, dry_run=dry_run)
        script_post = session_dir / "process" / "post_processing.ssf"
        scripts.append(script_post)
        logger.info(f"      Script : {script_post}")

        if exported:
            if dry_run:
                logger.info(f"      → {exported.name}")
            else:
                logger.success(exported.name)
        else:
            logger.warning("Export désactivé dans le profil")

        logger.rule("Terminé" if not dry_run else "Simulation terminée")
        logger.info(f"Log complet : {logger.log_path}")
        logger.close()

        return PipelineResult(
            stacked=stacked,
            processed=stacked,
            exported=exported,
            scripts=scripts,
            commands=commands,
            log_path=logger.log_path,
        )

    # === Mode meteors : soustraction max - médian pour isoler les météores =====
    # Pas de calibration, pas de GraXpert, pas de StarNet, pas de registration.
    # 1. Siril : conversion + dématriçage + stack max + stack médian
    # 2. Python : soustraction (max - médian), seuillage p99.9, combinaison
    # 3. Siril : stretch + couleur + export
    #
    # Les star trails sont similaires entre le max et le médian, donc
    # s'annulent dans la différence. Les météores n'apparaissent qu'une fois,
    # donc sont dans le max mais pas dans le médian : ils restent dans la diff.
    if is_meteors:
        total_steps = 3
        step = 0
        scripts: list[Path] = []
        commands: list[list[str]] = []

        # Étape 1 : Siril — conversion + stack max + stack médian
        step += 1
        logger.step(step, total_steps, "Siril",
                    "conversion RAW + stack max + stack médian")
        stacked = siril.run_meteors(session_dir, profile, dry_run=dry_run)
        script1 = session_dir / "process" / "generated.ssf"
        scripts.append(script1)
        logger.info(f"      Script : {script1}")
        for path in stacked:
            if dry_run:
                logger.info(f"      → {path.name}")
            else:
                logger.success(path.name)

        # Étape 2 : Python — soustraction + isolation des météores
        step += 1
        logger.step(step, total_steps, "Python",
                    "soustraction max - médian, isolation des météores")
        meteor_path = _isolate_meteors(stacked, output_dir, dry_run=dry_run)
        if meteor_path:
            if dry_run:
                logger.info(f"      → {meteor_path.name}")
            else:
                logger.success(meteor_path.name)
        processed = [meteor_path] if meteor_path else stacked

        # Étape 3 : Siril — stretch + couleur + export
        step += 1
        logger.step(step, total_steps, "Siril", "stretch, couleur, export")
        exported = siril.run_post(session_dir, profile, processed, dry_run=dry_run)
        script_post = session_dir / "process" / "post_processing.ssf"
        scripts.append(script_post)
        logger.info(f"      Script : {script_post}")

        if exported:
            if dry_run:
                logger.info(f"      → {exported.name}")
            else:
                logger.success(exported.name)
        else:
            logger.warning("Export désactivé dans le profil")

        logger.rule("Terminé" if not dry_run else "Simulation terminée")
        logger.info(f"Log complet : {logger.log_path}")
        logger.close()

        return PipelineResult(
            stacked=stacked,
            processed=processed,
            exported=exported,
            scripts=scripts,
            commands=commands,
            log_path=logger.log_path,
        )

    # === Mode planetary : lucky imaging (planétaire, lune, soleil) ============
    # Pas de Siril, pas de GraXpert, pas de StarNet. Tout en Python + OpenCV.
    # 1. Lecture vidéo SER/AVI
    # 2. Tri par qualité (Laplacien)
    # 3. Alignement (corrélation de phase)
    # 4. Empilement (moyenne)
    # 5. RGB align (dispersion atmosphérique)
    # 6. Sharpening (ondelettes)
    # 7. Export TIFF
    if is_planetary:
        total_steps = 1
        step = 0
        scripts: list[Path] = []
        commands: list[list[str]] = []

        step += 1
        logger.step(step, total_steps, "OpenCV",
                    "lucky imaging : tri frames + alignement + stack + ondelettes + export")
        exported_files = planetary.process_planetary(
            session_dir, profile, dry_run=dry_run
        )

        for path in exported_files:
            if dry_run:
                logger.info(f"      → {path.name}")
            else:
                logger.success(path.name)

        exported = exported_files[0] if exported_files else None
        if not exported:
            logger.warning("Aucune vidéo trouvée")

        logger.rule("Terminé" if not dry_run else "Simulation terminée")
        logger.info(f"Log complet : {logger.log_path}")
        logger.close()

        return PipelineResult(
            stacked=exported_files,
            processed=exported_files,
            exported=exported,
            scripts=scripts,
            commands=commands,
            log_path=logger.log_path,
        )

    # === Pipeline classique (rgb / haoiii) ====================================
    # Le nombre d'étapes dépend du mode (haoiii a 2 étapes de plus)
    total_steps = 5 if is_haoiii else 3
    step = 0
    scripts: list[Path] = []
    commands: list[list[str]] = []

    # === Étape 1 : Siril — calibration + empilement ==========================
    step += 1
    logger.step(step, total_steps, "Siril", "calibration, alignement, empilement")
    stacked = siril.run(session_dir, profile, dry_run=dry_run)
    script1 = session_dir / "process" / "generated.ssf"
    scripts.append(script1)
    logger.info(f"      Script : {script1}")
    for path in stacked:
        if dry_run:
            logger.info(f"      → {path.name}")
        else:
            logger.success(path.name)

    # === Étape 2 : GraXpert — extraction du fond de ciel =====================
    # En mode haoiii, on ne fait QUE l'extraction du fond de ciel ici
    # (pas le débruitage, car les couches sont monochromes).
    # Le débruitage se fera après la recomposition RGB (étape 4).
    #
    # Si use_siril_subsky est activé, le gradient a été partiellement retiré
    # par Siril (seqsubsky) sur chaque pose individuelle. Mais un gradient
    # résiduel réapparaît après empilement. Si use_graxpert_post_stack est
    # activé, GraXpert (modèle IA) est utilisé pour corriger ce gradient
    # résiduel post-empilement (meilleur que le subsky Siril).
    bg_settings = profile.target.post.background_extraction
    use_graxpert_bg = bg_settings.enabled and bg_settings.use_graxpert_post_stack
    skip_graxpert_bg = bg_settings.enabled and bg_settings.use_siril_subsky and not use_graxpert_bg

    bg_processed: list[Path] = stacked  # par défaut, on garde les fichiers tels quels

    # GraXpert est nécessaire si :
    # - on n'utilise pas seqsubsky (extraction de fond par GraXpert), OU
    # - use_graxpert_post_stack (GraXpert IA post-empilement), OU
    # - le débruitage GraXpert est activé.
    denoise_enabled = profile.target.post.denoise.enabled and not is_haoiii
    needs_graxpert = is_haoiii or not skip_graxpert_bg or denoise_enabled

    if needs_graxpert:
        step += 1
        bg_label = "fond de ciel" if is_haoiii else "fond de ciel, débruitage"
        if use_graxpert_bg and not is_haoiii:
            bg_label = "fond de ciel IA post-empilement (GraXpert)"
        elif skip_graxpert_bg and not is_haoiii:
            bg_label = "débruitage (gradient déjà retiré par Siril)"
        logger.step(step, total_steps, "GraXpert", bg_label)

        bg_processed = []
        for path in stacked:
            if skip_graxpert_bg:
                # GraXpert seulement pour le débruitage si activé, pas le background
                if profile.target.post.denoise.enabled:
                    result, step_commands = graxpert.run_denoise_only(
                        path, output_dir, profile, dry_run=dry_run
                    )
                    bg_processed.append(result)
                    commands.extend(step_commands)
                    for command in step_commands:
                        logger.command(command)
                    if dry_run:
                        logger.info(f"      → {result.name}")
                    else:
                        logger.success(result.name)
                else:
                    bg_processed.append(path)
            else:
                result, step_commands = graxpert.run_background_only(
                    path, output_dir, profile, dry_run=dry_run
                )
                bg_processed.append(result)
                commands.extend(step_commands)
                for command in step_commands:
                    logger.command(command)
                    if dry_run:
                        logger.info(f"      → {result.name}")
                    else:
                        logger.success(result.name)
    else:
        # RGB mode avec subsky : GraXpert complètement skippé
        logger.info("  GraXpert skippé (gradient retiré par seqsubsky Siril)")

    # === Étapes 3-4 spécifiques au mode HaOIII ===============================
    denoise_input: list[Path] = bg_processed

    if is_haoiii:
        # --- Étape 3 : Siril — recomposition Ha+OIII en RGB linéaire ----------
        step += 1
        logger.step(step, total_steps, "Siril", "recomposition Ha+OIII → RGB linéaire")
        composed = siril.run_compose_linear(session_dir, profile, bg_processed, dry_run=dry_run)
        script_compose = session_dir / "process" / "compose_linear.ssf"
        scripts.append(script_compose)
        logger.info(f"      Script : {script_compose}")
        if dry_run:
            logger.info(f"      → {composed.name}")
        else:
            logger.success(composed.name)

        # --- Étape 4 : GraXpert — débruitage sur l'RGB recomposé -------------
        step += 1
        logger.step(step, total_steps, "GraXpert", "débruitage IA (RGB recomposé)")
        denoised, denoise_commands = graxpert.run_denoise_only(
            composed, output_dir, profile, dry_run=dry_run
        )
        commands.extend(denoise_commands)
        for command in denoise_commands:
            logger.command(command)
        if dry_run:
            logger.info(f"      → {denoised.name}")
        else:
            logger.success(denoised.name)

        denoise_input = [denoised]

    # === Étape finale : Siril — post-traitement non-linéaire ==================
    step += 1
    logger.step(step, total_steps, "Siril",
                "stretch, StarNet, couleur, sharpening, export")

    exported = siril.run_post(session_dir, profile, denoise_input, dry_run=dry_run)
    script_post = session_dir / "process" / "post_processing.ssf"
    scripts.append(script_post)
    logger.info(f"      Script : {script_post}")

    if exported:
        if dry_run:
            logger.info(f"      → {exported.name}")
        else:
            logger.success(exported.name)
    else:
        logger.warning("Export désactivé dans le profil (post.export.enabled: false)")

    # === Fin ==================================================================
    logger.rule("Terminé" if not dry_run else "Simulation terminée")
    logger.info(f"Log complet : {logger.log_path}")
    logger.close()

    return PipelineResult(
        stacked=stacked,
        processed=denoise_input,
        exported=exported,
        scripts=scripts,
        commands=commands,
        log_path=logger.log_path,
    )