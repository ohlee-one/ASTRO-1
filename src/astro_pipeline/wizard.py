"""Commande `astro wizard` : assistant de création de profils.

Pose des questions à l'utilisateur (caméra, filtre, cible) et génère
automatiquement les profils YAML optimisés dans profiles/.

Le wizard est volontairement simple : aucune connaissance technique requise.
On demande la caméra (pas la taille des photosites — on l'a en base),
le filtre utilisé (pas le mode de traitement — on le déduit), et quels
fichiers sont déjà empilés par le logiciel d'acquisition.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from astro_pipeline.config import PROFILES_DIR

console = Console()

# ---------------------------------------------------------------------------
# Base de caméras : nom → (color, pixel_size_um)
# Étendue au fur et à mesure. L'utilisateur peut aussi entrer "autre".
# ---------------------------------------------------------------------------

CAMERAS: dict[str, tuple[bool, float]] = {
    # ZWO couleur
    "ASI294MC": (True, 4.63),
    "ASI533MC": (True, 3.76),
    "ASI2600MC": (True, 3.76),
    "ASI585MC": (True, 2.315),
    "ASI678MC": (True, 2.0),
    "ASI485MC": (True, 5.8),
    # ZWO monochrome
    "ASI294MM": (False, 4.63),
    "ASI533MM": (False, 3.76),
    "ASI2600MM": (False, 3.76),
    "ASI585MM": (False, 2.315),
    "ASI6200MM": (False, 3.76),
    # Canon / Nikon DSLR
    "Canon 600D": (True, 4.3),
    "Canon 6D": (True, 6.55),
    "Canon R5": (True, 4.39),
    "Canon R6": (True, 4.39),
    "Nikon D5300": (True, 3.92),
    "Nikon D750": (True, 5.97),
    # QHY
    "QHY268C": (True, 3.76),
    "QHY600": (False, 3.76),
    # Player One
    "Poseidon-M": (False, 2.4),
}

# ---------------------------------------------------------------------------
# Filtres : nom → template setup
# ---------------------------------------------------------------------------

FILTERS: dict[str, str] = {
    "L-eXtreme (dual-band Ha+OIII)": "color-dualband",
    "L-Ultimate (dual-band Ha+OIII)": "color-dualband",
    "L-Pro (large bande)": "color-lp",
    "Filtre LP / UV-IR cut": "color-lp",
    "Pas de filtre (DSLR)": "dslr",
    "Ha 7nm": "mono-narrowband",
    "OIII 7nm": "mono-narrowband",
    "SII 7nm": "mono-narrowband",
    "L (luminance)": "mono-lrgb",
    "R (rouge)": "mono-lrgb",
    "G (vert)": "mono-lrgb",
    "B (bleu)": "mono-lrgb",
}

# ---------------------------------------------------------------------------
# Cibles : nom → template target
# ---------------------------------------------------------------------------

TARGETS: dict[str, str] = {
    "Nébuleuse (bande étroite Ha/OIII)": "nebula-narrowband",
    "Nébuleuse (RGB large bande)": "nebula-rgb",
    "Galaxie": "galaxy-rgb",
    "Amas d'étoiles": "cluster-rgb",
    "Comète": "comet-rgb",
    "Reste de supernova (Veil, etc.)": "snr-narrowband",
    "Star trails (traînées d'étoiles)": "star-trails",
}

TEMPLATES_DIR = PROFILES_DIR.parent / "templates"


def wizard_command() -> None:
    """Assistant interactif : crée vos profils setup et target en 2 minutes."""
    console.print(Panel(
        "[bold cyan]Astro-1 Wizard[/bold cyan]\n"
        "Répondez à quelques questions, on génère vos profils automatiquement.\n"
        "[dim]Appuyez sur Entrée pour accepter la valeur par défaut.[/dim]",
        title="Bienvenue",
        border_style="cyan",
    ))

    # =====================================================================
    # 1. Matériel
    # =====================================================================
    console.print("\n[bold]1. Votre caméra[/bold]\n")

    # Afficher les caméras par groupes
    cam_table = Table(title="Caméras connues", show_header=True, border_style="dim")
    cam_table.add_column("N°", style="cyan", width=4)
    cam_table.add_column("Caméra", style="white")
    cam_table.add_column("Type", style="dim", width=10)
    cam_table.add_column("Photosites", style="dim", width=10)

    cam_list = list(CAMERAS.keys())
    for i, name in enumerate(cam_list, 1):
        is_color, px = CAMERAS[name]
        cam_table.add_row(str(i), name, "couleur" if is_color else "mono", f"{px} µm")
    console.print(cam_table)

    cam_choice = Prompt.ask(
        f"\nVotre caméra (1-{len(cam_list)} ou tapez le nom exact)",
        default="1",
    )

    # Déterminer la caméra
    if cam_choice.isdigit() and 1 <= int(cam_choice) <= len(cam_list):
        camera_name = cam_list[int(cam_choice) - 1]
        is_color, pixel_size = CAMERAS[camera_name]
    elif cam_choice in CAMERAS:
        camera_name = cam_choice
        is_color, pixel_size = CAMERAS[camera_name]
    else:
        # Caméra personnalisée
        camera_name = cam_choice
        is_color = Confirm.ask("C'est une caméra couleur (matrice de Bayer) ?", default=True)
        pixel_size_str = Prompt.ask("Taille des photosites (µm)", default="3.76")
        pixel_size = float(pixel_size_str)

    # Focale et diamètre
    console.print("\n[bold]Votre optique[/bold]\n")
    focal_length = Prompt.ask("Focale du télescope/lunette (mm)", default="250")
    aperture = Prompt.ask("Diamètre (mm)", default="51")

    # =====================================================================
    # 2. Filtre
    # =====================================================================
    console.print("\n[bold]2. Votre filtre[/bold]\n")

    # Filtrer les filtres pertinents selon le type de caméra
    if is_color:
        relevant_filters = {
            k: v for k, v in FILTERS.items()
            if v in ("color-dualband", "color-lp", "dslr")
        }
    else:
        relevant_filters = {
            k: v for k, v in FILTERS.items()
            if v in ("mono-narrowband", "mono-lrgb")
        }

    filt_table = Table(title="Filtres disponibles", show_header=True, border_style="dim")
    filt_table.add_column("N°", style="cyan", width=4)
    filt_table.add_column("Filtre")
    filt_list = list(relevant_filters.keys())
    for i, name in enumerate(filt_list, 1):
        filt_table.add_row(str(i), name)
    console.print(filt_table)

    filt_choice = Prompt.ask(
        f"\nVotre filtre (1-{len(filt_list)})",
        default="1",
    )
    filt_idx = int(filt_choice) - 1 if filt_choice.isdigit() else 0
    filt_idx = max(0, min(filt_idx, len(filt_list) - 1))
    filter_name = filt_list[filt_idx]
    setup_key = relevant_filters[filter_name]

    # =====================================================================
    # 3. Fichiers pré-empilés
    # =====================================================================
    console.print("\n[bold]3. Fichiers pré-empilés[/bold]")
    console.print("[dim]Certains logiciels (ASIAIR, NINA) empilent automatiquement[/dim]")
    console.print("[dim]les darks et flats. Dites-nous lesquels.[/dim]\n")

    premade_darks = Confirm.ask("Vos darks sont déjà empilés (MasterDark_*.fit) ?", default=False)
    premade_flats = Confirm.ask("Vos flats sont déjà empilés (MasterFlat_*.fit) ?", default=False)
    use_biases = Confirm.ask("Shootez-vous des bias/offsets séparés ?", default=False)

    premade_masters = premade_darks and premade_flats

    # =====================================================================
    # 4. Cible
    # =====================================================================
    console.print("\n[bold]4. Votre cible[/bold]\n")

    target_table = Table(title="Types de cible", show_header=True, border_style="dim")
    target_table.add_column("N°", style="cyan", width=4)
    target_table.add_column("Type")
    target_list = list(TARGETS.keys())
    for i, name in enumerate(target_list, 1):
        target_table.add_row(str(i), name)
    console.print(target_table)

    target_choice = Prompt.ask(
        f"\nVotre type de cible (1-{len(target_list)})",
        default="1",
    )
    target_idx = int(target_choice) - 1 if target_choice.isdigit() else 0
    target_idx = max(0, min(target_idx, len(target_list) - 1))
    target_label = target_list[target_idx]
    target_key = TARGETS[target_label]

    target_name = Prompt.ask("Nom de cette cible (ex: M42, IC1805, Veil)", default=target_key)

    # =====================================================================
    # 5. Nom du setup
    # =====================================================================
    default_setup_name = f"{camera_name.lower().replace(' ', '-')}-{focal_length}mm"
    setup_name = Prompt.ask("\nNom du profil setup", default=default_setup_name)

    # =====================================================================
    # Génération
    # =====================================================================
    console.print("\n[bold]Génération des profils...[/bold]\n")

    setup_src = TEMPLATES_DIR / f"setup-{setup_key}.yaml"
    target_src = TEMPLATES_DIR / f"{target_key}.yaml"

    setups_dir = PROFILES_DIR / "setups"
    targets_dir = PROFILES_DIR / "targets"
    setups_dir.mkdir(parents=True, exist_ok=True)
    targets_dir.mkdir(parents=True, exist_ok=True)

    setup_dst = setups_dir / f"{setup_name}.yaml"
    target_dst = targets_dir / f"{target_name}.yaml"

    if setup_dst.exists() and not Confirm.ask(
        f"Le profil setup '{setup_name}' existe. Écraser ?", default=False
    ):
        console.print("[yellow]Annulé.[/yellow]")
        return

    if target_dst.exists() and not Confirm.ask(
        f"Le profil target '{target_name}' existe. Écraser ?", default=False
    ):
        console.print("[yellow]Annulé.[/yellow]")
        return

    # Générer le setup
    if not setup_src.exists():
        console.print(f"[red]Template setup introuvable : {setup_src}[/red]")
        return

    content = setup_src.read_text(encoding="utf-8")

    # Remplacer les valeurs matériel
    content = _replace_value(content, "focal_length_mm", focal_length)
    content = _replace_value(content, "aperture_mm", aperture)
    content = _replace_value(content, "pixel_size_um", str(pixel_size))
    content = _replace_value(content, "use_premade_masters", "true" if premade_masters else "false")
    content = _replace_value(content, "use_biases", "true" if use_biases else "false")

    # Remplacer le nom
    for old_name in [
        "Caméra couleur + dual-band (template)",
        "Caméra couleur + LP/UV-IR (template)",
        "Caméra mono + filtres étroits (template)",
        "Caméra mono + LRGB (template)",
        "DSLR/Hybride (template)",
    ]:
        content = content.replace(f'name: "{old_name}"', f'name: "{camera_name} + {filter_name}"')

    setup_dst.write_text(content, encoding="utf-8")
    console.print(f"[green]✓ Setup :[/green] {setup_dst}")

    # Générer le target
    if not target_src.exists():
        console.print(f"[red]Template target introuvable : {target_src}[/red]")
        return

    content = target_src.read_text(encoding="utf-8")
    # Remplacer le nom template par le nom de la cible
    for old_name in [
        "Nébuleuse bande étroite (template)",
        "Nébuleuse RGB large bande (template)",
        "Galaxie RGB (template)",
        "Amas d'étoiles RGB (template)",
        "Comète (template)",
        "Reste de supernova bande étroite (template)",
    ]:
        content = content.replace(f'name: "{old_name}"', f'name: "{target_name}"')

    target_dst.write_text(content, encoding="utf-8")
    console.print(f"[green]✓ Target :[/green] {target_dst}")

    # Résumé
    console.print(Panel(
        f"[bold green]Profils créés ![/bold green]\n\n"
        f"Caméra  : {camera_name} ({'couleur' if is_color else 'mono'}, {pixel_size} µm)\n"
        f"Filtre  : {filter_name}\n"
        f"Focale  : {focal_length} mm / {aperture} mm\n"
        f"Pré-empilés : darks={'oui' if premade_darks else 'non'} "
        f"flats={'oui' if premade_flats else 'non'}\n\n"
        f"Setup   : [cyan]{setup_name}[/cyan]\n"
        f"Target  : [cyan]{target_name}[/cyan]\n\n"
        f"[bold]Lancer le pipeline :[/bold]\n"
        f"[dim]uv run astro run --session ~/Astro/ma-session \\\n"
        f"  --setup {setup_name} --target {target_name}[/dim]",
        title="Terminé",
        border_style="green",
    ))


def _replace_value(content: str, key: str, value: str) -> str:
    """Remplace la valeur d'une clé YAML, en gérant l'indentation et les commentaires."""
    import re

    pattern = rf"^(\s*{key}:\s*)\S+"
    replacement = rf"\g<1>{value}"
    return re.sub(pattern, replacement, content, flags=re.MULTILINE)