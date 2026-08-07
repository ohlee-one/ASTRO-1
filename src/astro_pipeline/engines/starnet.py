"""Engine StarNet2 pour la separation etoiles / starless.

StarNet2 est un outil IA qui separe une image en deux couches:
  - starless : l'image sans les etoiles (nebuleuse, galaxie, fond)
  - star_mask : les etoiles uniquement

On l'utilise en ligne de commande (CLI) independamment de Siril, car Siril
ne lit pas toujours la configuration starnet_exe en mode CLI.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

STARNET2_BIN = "/usr/local/bin/starnet2"


def is_available() -> bool:
    """Verifie que starnet2 est installe et executable."""
    return shutil.which(STARNET2_BIN) is not None or Path(STARNET2_BIN).exists()


def run_starnet(
    input_path: Path,
    output_dir: Path,
    dry_run: bool = False,
) -> tuple[Path, Path, list[str]]:
    """Separe une image en starless + star_mask via StarNet2.

    Args:
        input_path: image FITS/TIFF/PNG a separer (deja stretchee).
        output_dir: dossier de sortie.
        dry_run: si True, ne lance pas vraiment starnet2.

    Returns:
        (starless_path, starmask_path, commands)
    """
    stem = input_path.stem
    starless_path = output_dir / f"{stem}_starless.tif"
    starmask_path = output_dir / f"{stem}_starmask.tif"

    cmd = [
        STARNET2_BIN,
        "-i", str(input_path),
        "-o", str(starless_path),
        "-m", str(starmask_path),
        "-q",  # quiet
    ]

    commands = [" ".join(cmd)]

    if not dry_run:
        subprocess.run(cmd, check=True, capture_output=True)

    return starless_path, starmask_path, commands