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
    """
    if not session_dir.exists():
        raise SessionError(f"Le dossier de session n'existe pas : {session_dir}")

    folders = profile.setup.folders
    required = [folders.lights, folders.darks, folders.flats]
    if profile.setup.calibration.use_biases and not profile.setup.calibration.use_premade_masters:
        required.append(folders.biases)

    missing = [name for name in required if not (session_dir / name).is_dir()]
    if missing:
        raise SessionError(
            f"Dossiers manquants dans {session_dir} : {', '.join(missing)}\n"
            f"Structure attendue : {', '.join(required)}\n"
            f"(Si tu n'utilises pas d'offsets, mets use_biases: false "
            f"dans ton profil setup.)"
        )

    # Vérifie que les dossiers ne sont pas vides
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
    step += 1
    bg_label = "fond de ciel" if is_haoiii else "fond de ciel, débruitage"
    logger.step(step, total_steps, "GraXpert", bg_label)

    bg_processed: list[Path] = []
    for path in stacked:
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