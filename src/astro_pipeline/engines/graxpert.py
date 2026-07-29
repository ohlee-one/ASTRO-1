"""GraXpert engine wrapper.

GraXpert applique des modèles IA pré-entraînés : extraction du fond de ciel,
débruitage, et (selon les versions) déconvolution.

⚠️ POINT IMPORTANT
Les modèles IA ne sont pas embarqués dans le téléchargement. Ils sont récupérés
depuis un stockage distant par l'application graphique uniquement. Il faut donc
avoir lancé GraXpert en mode fenêtré AU MOINS UNE FOIS et appliqué chaque
traitement, pour que les modèles soient mis en cache localement. Ensuite le
mode CLI fonctionne.

⚠️ SECOND POINT
La syntaxe de la ligne de commande a évolué entre les versions 2.x et 3.x.
Tous les arguments sont construits dans ce fichier et nulle part ailleurs :
s'il faut les corriger pour ta version, c'est le seul endroit à toucher.
Lance `astro doctor --verbose` pour afficher l'aide de ta version installée.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from astro_pipeline.config import Profile

CANDIDATE_PATHS = [
    Path("/Applications/GraXpert.app/Contents/MacOS/GraXpert"),
    Path("/opt/homebrew/bin/graxpert"),
]


class GraXpertNotFoundError(Exception):
    pass


class GraXpertExecutionError(Exception):
    pass


def find_binary() -> Path | None:
    for candidate in CANDIDATE_PATHS:
        if candidate.exists():
            return candidate
    found = shutil.which("graxpert")
    return Path(found) if found else None


def is_available() -> bool:
    return find_binary() is not None


def help_text() -> str:
    """Retourne l'aide de la version installée (utile pour vérifier la syntaxe)."""
    binary = find_binary()
    if binary is None:
        raise GraXpertNotFoundError("GraXpert est introuvable.")
    result = subprocess.run(
        [str(binary), "-h"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() or result.stderr.strip()


def _run_command(command: list[str], step_name: str, dry_run: bool) -> None:
    if dry_run:
        return
    process = subprocess.run(command, capture_output=True, text=True, check=False)
    if process.returncode != 0:
        raise GraXpertExecutionError(
            f"GraXpert a échoué à l'étape « {step_name} » "
            f"(code {process.returncode}).\n"
            f"Commande : {' '.join(command)}\n"
            f"--- Sortie ---\n{process.stdout[-2000:]}\n{process.stderr[-2000:]}\n\n"
            f"Piste la plus fréquente : le modèle IA n'a jamais été téléchargé. "
            f"Ouvre GraXpert en mode graphique et applique ce traitement une fois."
        )


def build_background_command(
    input_path: Path, output_path: Path, profile: Profile
) -> list[str]:
    binary = find_binary()
    settings = profile.target.post.background_extraction
    # GraXpert attend "Subtraction" / "Division" avec une majuscule
    correction = settings.correction.capitalize()
    return [
        str(binary),
        "-cli",
        str(input_path),
        "-cmd", "background-extraction",
        "-correction", correction,
        "-smoothing", str(settings.smoothing),
        "-output", str(output_path.with_suffix("")),
    ]


def build_denoise_command(
    input_path: Path, output_path: Path, profile: Profile
) -> list[str]:
    binary = find_binary()
    settings = profile.target.post.denoise
    return [
        str(binary),
        "-cli",
        str(input_path),
        "-cmd", "denoising",
        "-strength", str(settings.strength),
        "-batch_size", str(settings.batch_size),
        "-output", str(output_path.with_suffix("")),
    ]


def run(
    input_path: Path,
    output_dir: Path,
    profile: Profile,
    dry_run: bool = False,
) -> tuple[Path, list[list[str]]]:
    """Applique les étapes de post-traitement activées dans le profil cible.

    Chaque étape prend en entrée la sortie de la précédente. Les fichiers
    intermédiaires sont conservés : ils sont précieux pour comparer et ajuster.

    Returns:
        (chemin du fichier final, liste des commandes construites)
    """
    if find_binary() is None:
        raise GraXpertNotFoundError(
            "GraXpert est introuvable.\n"
            "Télécharge le build macOS et place GraXpert.app dans /Applications."
        )

    # Le préfixe reprend le nom du fichier empilé (result, Ha_result, OIII_result)
    # pour que les sorties Ha et OIII ne s'écrasent pas mutuellement.
    prefix = input_path.stem

    current = input_path
    commands: list[list[str]] = []

    if profile.target.post.background_extraction.enabled:
        target = output_dir / f"{prefix}_bg.fits"
        command = build_background_command(current, target, profile)
        commands.append(command)
        _run_command(command, f"extraction du fond de ciel ({prefix})", dry_run)
        current = target

    if profile.target.post.denoise.enabled:
        target = output_dir / f"{prefix}_denoised.fits"
        command = build_denoise_command(current, target, profile)
        commands.append(command)
        _run_command(command, f"débruitage ({prefix})", dry_run)
        current = target

    return current, commands


def run_background_only(
    input_path: Path,
    output_dir: Path,
    profile: Profile,
    dry_run: bool = False,
) -> tuple[Path, list[list[str]]]:
    """Applique uniquement l'extraction du fond de ciel (pas de débruitage).

    Utilisé en mode haoiii où le débruitage doit attendre la recomposition RGB
    (le modèle IA de débruitage nécessite 3 canaux, pas 1).

    Returns:
        (chemin du fichier traité, liste des commandes)
    """
    if find_binary() is None:
        raise GraXpertNotFoundError(
            "GraXpert est introuvable.\n"
            "Télécharge le build macOS et place GraXpert.app dans /Applications."
        )

    prefix = input_path.stem
    commands: list[list[str]] = []

    if profile.target.post.background_extraction.enabled:
        target = output_dir / f"{prefix}_bg.fits"
        command = build_background_command(input_path, target, profile)
        commands.append(command)
        _run_command(command, f"extraction du fond de ciel ({prefix})", dry_run)
        return target, commands

    # Si l'extraction est désactivée, retourner l'entrée telle quelle
    return input_path, commands


def run_denoise_only(
    input_path: Path,
    output_dir: Path,
    profile: Profile,
    dry_run: bool = False,
) -> tuple[Path, list[list[str]]]:
    """Applique uniquement le débruitage IA (pas d'extraction du fond de ciel).

    Utilisé en mode haoiii après la recomposition RGB, quand le fond de ciel
    a déjà été extrait sur les couches monochromes séparément.

    Returns:
        (chemin du fichier débruité, liste des commandes)
    """
    if find_binary() is None:
        raise GraXpertNotFoundError(
            "GraXpert est introuvable.\n"
            "Télécharge le build macOS et place GraXpert.app dans /Applications."
        )

    prefix = input_path.stem
    commands: list[list[str]] = []

    if profile.target.post.denoise.enabled:
        target = output_dir / f"{prefix}_denoised.fits"
        command = build_denoise_command(input_path, target, profile)
        commands.append(command)
        _run_command(command, f"débruitage ({prefix})", dry_run)
        return target, commands

    # Si le débruitage est désactivé, retourner l'entrée telle quelle
    return input_path, commands
