"""Orchestration : enchaîne les moteurs dans le bon ordre.

Ce module ne sait rien des détails de Siril ou de GraXpert. Il se contente de
les appeler et de gérer les chemins, les vérifications, le logging et l'affichage.

Le pipeline complet :

Mode RGB :
  1. Siril    : calibration, registration, empilement → FITS linéaire RGB
  2. GraXpert : extraction fond de ciel + débruitage IA (sur le linéaire)
  3. Siril    : stretch, StarNet, couleur, sharpening, export

Mode HaOIII (bande étroite) :
  1. Siril    : calibration, extraction Ha/OIII, empilement → 2 FITS monochromes
  2. GraXpert : extraction fond de ciel (sur chaque couche monochrome)
  3. Siril    : recomposition Ha+OIII → RGB linéaire
  4. GraXpert : débruitage IA (sur l'RGB recomposé — nécessite 3 canaux)
  5. Siril    : stretch, StarNet, couleur, sharpening, export

La différence clé : en mode haoiii, le débruitage IA de GraXpert ne peut pas
fonctionner sur les couches monochromes (1 canal). Il faut recomposer en RGB
(3 canaux) avant de débruiter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

from astro_pipeline.config import Profile
from astro_pipeline.engines import graxpert, siril
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

    Le flow diffère selon le mode (rgb vs haoiii) — voir la docstring du module.
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
    mode_label = mode + (" (extraction bande étroite Ha + OIII)" if is_haoiii else "")
    logger.info(f"Mode    : {mode_label}")
    logger.info(f"Log     : {logger.log_path}")

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