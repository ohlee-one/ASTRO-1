"""Interface en ligne de commande.

Commandes disponibles :
    astro doctor      vérifie que les moteurs sont installés
    astro profiles    liste les profils disponibles
    astro run         lance un traitement
"""

from __future__ import annotations

import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from astro_pipeline import pipeline
from astro_pipeline.config import ProfileNotFoundError, list_profiles, load_profile
from astro_pipeline.engines import graxpert, siril

app = typer.Typer(
    add_completion=False,
    help="Orchestrateur de traitement astrophoto piloté par profils.",
)
console = Console()


# ---------------------------------------------------------------------------
# StarNet : détection de l'exécutable (intégré à Siril mais binaire séparé)
# ---------------------------------------------------------------------------

STARNET_CANDIDATES = [
    Path("/Applications/Siril.app/Contents/MacOS/starnet2"),
    Path("/Applications/Siril.app/Contents/MacOS/starnet++"),
    Path("/opt/homebrew/bin/starnet2"),
    Path("/opt/homebrew/bin/starnet++"),
    Path("/usr/local/bin/starnet2"),
    Path("/usr/local/bin/starnet++"),
]


def find_starnet() -> Path | None:
    """Localise starnet2/starnet++ sur la machine, ou retourne None."""
    for candidate in STARNET_CANDIDATES:
        if candidate.exists():
            return candidate
    for name in ("starnet2", "starnet++"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@app.command()
def doctor(
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Affiche l'aide CLI de GraXpert."
    ),
) -> None:
    """Vérifie que Siril, GraXpert et StarNet++ sont correctement installés."""
    table = Table(title="Diagnostic de l'environnement")
    table.add_column("Moteur")
    table.add_column("État")
    table.add_column("Chemin / version")
    table.add_column("Rôle")

    # --- Siril ---
    siril_binary = siril.find_binary()
    if siril_binary:
        table.add_row(
            "Siril",
            "[green]OK[/green]",
            f"{siril_binary}\n{siril.version()}",
            "Calibration, empilement, stretch, couleur, export",
        )
    else:
        table.add_row(
            "Siril",
            "[red]MANQUANT[/red]",
            "brew install --cask siril",
            "Calibration, empilement, stretch, couleur, export",
        )

    # --- GraXpert ---
    graxpert_binary = graxpert.find_binary()
    if graxpert_binary:
        table.add_row(
            "GraXpert",
            "[green]OK[/green]",
            str(graxpert_binary),
            "Extraction fond de ciel, débruitage IA",
        )
    else:
        table.add_row(
            "GraXpert",
            "[red]MANQUANT[/red]",
            "Place GraXpert.app dans /Applications",
            "Extraction fond de ciel, débruitage IA",
        )

    # --- StarNet++ ---
    starnet_binary = find_starnet()
    if starnet_binary:
        table.add_row(
            "StarNet++",
            "[green]OK[/green]",
            str(starnet_binary),
            "Séparation étoiles / starless",
        )
    else:
        table.add_row(
            "StarNet++",
            "[yellow]MANQUANT[/yellow]",
            "Télécharge starnet2-cli sur starnetastro.com\n"
            "Puis déclare-le dans Siril > Preferences > Miscellaneous",
            "Séparation étoiles / starless (optionnel)",
        )

    console.print(table)

    # Conseils
    if graxpert_binary:
        console.print(
            "\n[yellow]Rappel :[/yellow] les modèles IA de GraXpert doivent avoir été "
            "téléchargés une fois via l'interface graphique avant que le mode CLI "
            "fonctionne."
        )

    if not starnet_binary:
        console.print(
            "\n[yellow]StarNet++ :[/yellow] si tu n'utilises pas la séparation"
            " étoiles/starless, ignore ce message.\n"
            "Sinon, télécharge le binaire CLI sur https://starnetastro.com/cli-tools/\n"
            "et déclare-le dans Siril > Preferences > Miscellaneous > StarNet."
        )

    if verbose and graxpert_binary:
        console.print("\n[bold]Aide CLI de ta version de GraXpert :[/bold]")
        console.print(graxpert.help_text())

    # Siril et GraXpert sont obligatoires. StarNet est optionnel.
    if not (siril_binary and graxpert_binary):
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# profiles
# ---------------------------------------------------------------------------


@app.command()
def profiles() -> None:
    """Liste les profils setup et cible disponibles."""
    available = list_profiles()
    table = Table(title="Profils disponibles")
    table.add_column("Setups (--setup)")
    table.add_column("Cibles (--target)")

    setups = available["setups"]
    targets = available["targets"]
    for index in range(max(len(setups), len(targets))):
        table.add_row(
            setups[index] if index < len(setups) else "",
            targets[index] if index < len(targets) else "",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@app.command()
def run(
    session: Path = typer.Option(
        ..., "--session", help="Dossier de la session (contient lights/, darks/...)"
    ),
    setup: str = typer.Option(..., "--setup", help="Nom du profil setup"),
    target: str = typer.Option(..., "--target", help="Nom du profil cible"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Génère les scripts et affiche les commandes sans rien exécuter.",
    ),
) -> None:
    """Lance le traitement complet d'une session.

    Le pipeline enchaîne trois phases :
      1. Siril   : calibration + empilement (linéaire)
      2. GraXpert : extraction fond de ciel + débruitage (linéaire)
      3. Siril   : stretch, StarNet, couleur, sharpening, export (non-linéaire)

    En mode --dry-run, les scripts .ssf sont générés et affichés mais
    aucun moteur n'est exécuté. Parfait pour vérifier avant de lancer.
    """
    try:
        profile = load_profile(setup, target)
    except (ProfileNotFoundError, ValueError) as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from error

    try:
        result = pipeline.run(session, profile, dry_run=dry_run)
    except Exception as error:  # noqa: BLE001 — message lisible, pas un traceback
        console.print(f"\n[red]{error}[/red]")
        raise typer.Exit(code=1) from error

    if dry_run:
        console.print("\n[bold]Scripts générés :[/bold]")
        for script in result.scripts:
            console.print(f"  {script}")
        console.print(
            "\nRelis-les avant de relancer sans --dry-run.\n"
            "Tu peux les ouvrir dans un éditeur de texte pour vérifier"
            " chaque commande Siril."
        )

    if result.exported:
        console.print(f"\n[bold green]Image finale :[/bold green] {result.exported}")

    if result.log_path:
        console.print(f"[dim]Log complet : {result.log_path}[/dim]")


if __name__ == "__main__":
    app()