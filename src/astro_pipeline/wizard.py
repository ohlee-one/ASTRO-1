"""Commande `astro wizard` : assistant de création de profils.

Pose des questions à l'utilisateur (matériel + type de cible) et génère
automatiquement les profils YAML optimisés dans profiles/.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from astro_pipeline.config import PROFILES_DIR

console = Console()

# Templates disponibles, mappés par type
SETUP_TEMPLATES = {
    "color-dualband": "Caméra couleur + filtre dual-band (L-eXtreme, L-Ultimate)",
    "color-lp": "Caméra couleur + filtre LP/UV-IR (large bande)",
    "mono-narrowband": "Caméra mono + filtres bande étroite (Ha, OIII, SII)",
    "mono-lrgb": "Caméra mono + filtres LRGB",
    "dslr": "DSLR / appareil photo",
}

TARGET_TEMPLATES = {
    "nebula-narrowband": "Nébuleuse en bande étroite (Ha/OIII)",
    "nebula-rgb": "Nébuleuse en RGB large bande",
    "galaxy-rgb": "Galaxie",
    "cluster-rgb": "Amas d'étoiles",
    "comet-rgb": "Comète",
    "snr-narrowband": "Reste de supernova (SNR)",
}

TEMPLATES_DIR = PROFILES_DIR.parent / "templates"


def wizard_command() -> None:
    """Assistant interactif : crée vos profils setup et target en 2 minutes."""
    console.print(Panel(
        "[bold cyan]Astro-1 Wizard[/bold cyan]\n"
        "Création de vos profils de traitement en répondant à quelques questions.",
        title="Bienvenue",
        border_style="cyan",
    ))

    # -- Setup --
    console.print("\n[bold]1. Votre matériel[/bold]\n")

    setup_table = Table(title="Types de setup disponibles", show_header=True)
    setup_table.add_column("N°", style="cyan", width=4)
    setup_table.add_column("Type")
    for i, (key, label) in enumerate(SETUP_TEMPLATES.items(), 1):
        setup_table.add_row(str(i), label)
    console.print(setup_table)

    setup_choice = Prompt.ask(
        "\nVotre type de setup",
        choices=[str(i) for i in range(1, len(SETUP_TEMPLATES) + 1)],
        default="1",
    )
    setup_key = list(SETUP_TEMPLATES.keys())[int(setup_choice) - 1]

    # Questions matériel
    console.print("\n[bold]Détails de votre matériel[/bold]\n")
    setup_name = Prompt.ask("Nom du setup", default=f"mon-setup-{setup_key}")
    focal_length = Prompt.ask("Focale du télescope/lunette (mm)", default="250")
    aperture = Prompt.ask("Diamètre (mm)", default="51")
    pixel_size = Prompt.ask("Taille des photosites (µm)", default="4.63")

    premade = Confirm.ask(
        "Votre logiciel d'acquisition (ASIAIR, NINA...) empile-t-il déjà vos darks/flats ?",
        default=False,
    )

    # -- Target --
    console.print("\n[bold]2. Votre cible[/bold]\n")

    target_table = Table(title="Types de cible disponibles", show_header=True)
    target_table.add_column("N°", style="cyan", width=4)
    target_table.add_column("Type")
    for i, (key, label) in enumerate(TARGET_TEMPLATES.items(), 1):
        target_table.add_row(str(i), label)
    console.print(target_table)

    target_choice = Prompt.ask(
        "\nVotre type de cible",
        choices=[str(i) for i in range(1, len(TARGET_TEMPLATES) + 1)],
        default="1",
    )
    target_key = list(TARGET_TEMPLATES.keys())[int(target_choice) - 1]

    target_name = Prompt.ask("Nom de la cible", default=f"ma-cible-{target_key}")

    # -- Génération --
    console.print("\n[bold]3. Génération des profils[/bold]\n")

    setup_src = TEMPLATES_DIR / f"setup-{setup_key}.yaml"
    target_src = TEMPLATES_DIR / f"{target_key}.yaml"

    setups_dir = PROFILES_DIR / "setups"
    targets_dir = PROFILES_DIR / "targets"
    setups_dir.mkdir(parents=True, exist_ok=True)
    targets_dir.mkdir(parents=True, exist_ok=True)

    setup_dst = setups_dir / f"{setup_name}.yaml"
    target_dst = targets_dir / f"{target_name}.yaml"

    # Vérifier les overwrite
    if setup_dst.exists():
        if not Confirm.ask(f"Le profil setup '{setup_name}' existe déjà. Écraser ?", default=False):
            console.print("[yellow]Setup annulé.[/yellow]")
            return

    if target_dst.exists():
        if not Confirm.ask(f"Le profil target '{target_name}' existe déjà. Écraser ?", default=False):
            console.print("[yellow]Target annulé.[/yellow]")
            return

    # Lire et personnaliser le setup
    if setup_src.exists():
        content = setup_src.read_text(encoding="utf-8")
        # Remplacer les valeurs par défaut par celles de l'utilisateur
        content = content.replace("focal_length_mm: 250", f"focal_length_mm: {focal_length}")
        content = content.replace("aperture_mm: 51", f"aperture_mm: {aperture}")
        content = content.replace("pixel_size_um: 4.63", f"pixel_size_um: {pixel_size}")
        content = content.replace(
            "use_premade_masters: false",
            f"use_premade_masters: {'true' if premade else 'false'}",
        )
        content = content.replace(
            'name: "Caméra couleur + dual-band (template)"',
            f'name: "{setup_name}"',
        )
        content = content.replace(
            'name: "Caméra couleur + LP/UV-IR (template)"',
            f'name: "{setup_name}"',
        )
        content = content.replace(
            'name: "Caméra mono + bande étroite (template)"',
            f'name: "{setup_name}"',
        )
        content = content.replace(
            'name: "Caméra mono + LRGB (template)"',
            f'name: "{setup_name}"',
        )
        content = content.replace(
            'name: "DSLR / appareil photo (template)"',
            f'name: "{setup_name}"',
        )
        setup_dst.write_text(content, encoding="utf-8")
        console.print(f"[green]✓ Setup créé :[/green] {setup_dst}")
    else:
        console.print(f"[red]Template setup introuvable : {setup_src}[/red]")
        return

    # Copier le target (sans modification, l'utilisateur ajustera après)
    if target_src.exists():
        content = target_src.read_text(encoding="utf-8")
        # Remplacer le nom
        original_names = [
            "Nébuleuse bande étroite (template)",
            "Nébuleuse RGB large bande (template)",
            "Galaxie RGB (template)",
            "Amas d'étoiles RGB (template)",
            "Comète (template)",
            "Reste de supernova bande étroite (template)",
        ]
        for orig in original_names:
            content = content.replace(
                f'name: "{orig}"',
                f'name: "{target_name}"',
            )
        target_dst.write_text(content, encoding="utf-8")
        console.print(f"[green]✓ Target créé :[/green] {target_dst}")
    else:
        console.print(f"[red]Template target introuvable : {target_src}[/red]")
        return

    # -- Résumé --
    console.print(Panel(
        f"[bold green]Profils créés ![/bold green]\n\n"
        f"Setup  : [cyan]{setup_name}[/cyan]\n"
        f"Target : [cyan]{target_name}[/cyan]\n\n"
        f"[bold]Pour lancer le pipeline :[/bold]\n"
        f"[dim]uv run astro run --session ~/Astro/ma-session \\\n"
        f"  --setup {setup_name} --target {target_name}[/dim]\n\n"
        f"[bold]Pour itérer avec une IA :[/bold]\n"
        f"[dim]Ouvrez le projet dans Cursor et demandez à l'IA d'ajuster le rendu.[/dim]",
        title="Terminé",
        border_style="green",
    ))