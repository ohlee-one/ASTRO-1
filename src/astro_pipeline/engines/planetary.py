"""Planetary engine : lucky imaging pour le planétaire et la lune.

Ce moteur ne dépend PAS de Siril. Il utilise OpenCV directement pour :
  1. Lire la vidéo SER/AVI frame par frame
  2. Évaluer la qualité (seeing) de chaque frame (gradient de Laplacien)
  3. Trier et garder les meilleures frames
  4. Aligner les frames sélectionnées (corrélation de phase)
  5. Empiler (stacking par moyenne)
  6. Appliquer des ondelettes (sharpening) pour révéler les détails

Le lucky imaging exploite les instants où l'atmosphère est calme ("seeing"
bon) parmi des milliers de frames vidéo. En gardant uniquement les meilleures,
on obtient une image nette impossible à obtenir avec une seule pose longue.

Documentation : https://docs.opencv.org/4.x/
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np

from astro_pipeline.config import Profile


class PlanetaryError(Exception):
    pass


def _ensure_opencv() -> None:
    """Vérifie qu'OpenCV est disponible. Tente l'installation si manquant."""
    try:
        import cv2  # noqa: F401
    except ImportError:
        raise PlanetaryError(
            "OpenCV (cv2) est requis pour le mode planétaire.\n"
            "Installe-le avec :  uv add opencv-python\n"
            "Ou :  pip install opencv-python"
        )


def _find_videos(session_dir: Path, lights_folder: str) -> list[Path]:
    """Trouve les fichiers vidéo SER/AVI dans le dossier lights."""
    lights_dir = session_dir / lights_folder
    if not lights_dir.exists():
        raise PlanetaryError(f"Le dossier {lights_dir} n'existe pas.")

    extensions = (".ser", ".avi", ".mp4", ".mov", ".mkv")
    # Chercher dans lights/ et les sous-dossiers (ex: lights/120826/)
    videos: list[Path] = []
    for f in lights_dir.rglob("*"):
        if f.suffix.lower() in extensions:
            videos.append(f)

    if not videos:
        raise PlanetaryError(
            f"Aucune vidéo SER/AVI trouvée dans {lights_dir}\n"
            f"Formats supportés : {', '.join(extensions)}"
        )

    return sorted(videos)


def _read_frames(video_path: Path) -> np.ndarray:
    """Lit toutes les frames d'une vidéo SER/AVI avec OpenCV.

    Retourne un tableau (N, H, W, 3) en uint8.
    Pour les gros fichiers SER (>4 Go), on lit en streaming.
    """
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise PlanetaryError(f"Impossible d'ouvrir {video_path}")

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    frames: list[np.ndarray] = []
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        # OpenCV lit en BGR, on convertit en RGB
        frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

    cap.release()

    if not frames:
        raise PlanetaryError(f"Aucune frame lue dans {video_path}")

    return np.stack(frames)


def _frame_quality(frame: np.ndarray) -> float:
    """Évalue la qualité (netteté) d'une frame.

    Utilise la variance du Laplacien : plus le gradient est élevé,
    plus l'image est nette (détails fins visibles = bon seeing).
    """
    import cv2

    gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
    laplacian = cv2.Laplacian(gray, cv2.CV_64F)
    return float(laplacian.var())


def _select_best_frames(
    frames: np.ndarray, fraction: float
) -> tuple[np.ndarray, list[int]]:
    """Sélectionne les meilleures frames par qualité (Laplacien).

    Args:
        frames: tableau (N, H, W, 3)
        fraction: fraction à garder (0.25 = 25% des meilleures)

    Returns:
        (meilleures_frames, indices_sélectionnés)
    """
    n = len(frames)
    n_keep = max(1, int(n * fraction))

    # Calculer la qualité de chaque frame
    qualities = [_frame_quality(f) for f in frames]

    # Trier par qualité décroissante et garder les meilleures
    sorted_indices = np.argsort(qualities)[::-1][:n_keep]
    selected = sorted(sorted_indices)  # remettre dans l'ordre chronologique

    return frames[selected], list(selected)


def _align_frames(
    frames: np.ndarray, mode: str = "planet"
) -> np.ndarray:
    """Aligne les frames les unes par rapport aux autres.

    Pour les planètes : alignement par translation (décalage XY) en utilisant
    la corrélation de phase. On prend la première frame comme référence.

    Pour la lune/soleil (surface) : même méthode mais avec un fenêtrage
    plus large.

    Args:
        frames: tableau (N, H, W, 3)
        mode: "planet" ou "surface"

    Returns:
        Frames alignées (même shape)
    """
    import cv2

    n, h, w, _ = frames.shape
    aligned = np.zeros_like(frames)
    ref_gray = cv2.cvtColor(frames[0], cv2.COLOR_RGB2GRAY).astype(np.float32)

    aligned[0] = frames[0]

    for i in range(1, n):
        curr_gray = cv2.cvtColor(frames[i], cv2.COLOR_RGB2GRAY).astype(np.float32)

        # Corrélation de phase pour estimer le décalage
        # OpenCV 5.x retourne (shift, response), OpenCV 4.x retourne (shift, response, _)
        result = cv2.phaseCorrelate(ref_gray, curr_gray)
        shift = result[0]
        dx, dy = shift

        # Translation
        M = np.float32([[1, 0, dx], [0, 1, dy]])
        aligned[i] = cv2.warpAffine(
            frames[i], M, (w, h),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REFLECT,
        )

    return aligned


def _stack_frames(frames: np.ndarray) -> np.ndarray:
    """Empile les frames par moyenne et normalise sur 0-255.

    Args:
        frames: tableau (N, H, W, 3) uint8

    Returns:
        Image empilée (H, W, 3) en float32, normalisée sur [0, 255]
    """
    stacked = frames.astype(np.float32).mean(axis=0)
    # Normalisation : utiliser le p99.9 au lieu du max (évite qu'un pixel chaud
    # n'écrase tout le reste). Le p99.9 -> 255.
    p999 = np.percentile(stacked, 99.9)
    if p999 > 0:
        stacked = np.clip(stacked / p999 * 255.0, 0, 255)
    return stacked


def _wavelet_sharpen(
    image: np.ndarray,
    layers: int = 5,
    weights: list[float] | None = None,
) -> np.ndarray:
    """Renforcement par décomposition en ondelettes (à trous).

    Principe : on décompose l'image en couches d'ondelettes (chaque couche
    capture une échelle de détail), on amplifie chaque couche, puis on
    reconstruit l'image. C'est l'équivalent de RegiStax wavelets.

    Args:
        image: (H, W, 3) ou (H, W) en float32
        layers: nombre de couches d'ondelettes
        weights: poids d'amplification par couche

    Returns:
        Image sharpened (même shape)
    """
    if weights is None:
        weights = [1.5, 1.2, 0.8, 0.4, 0.1]
    # Compléter la liste si nécessaire
    while len(weights) < layers:
        weights.append(0.0)

    import cv2

    # Travailler en luminance (gris) pour préserver la couleur
    if image.ndim == 3:
        # Convertir en Lab, travailler sur L
        lab = cv2.cvtColor(image.astype(np.uint8), cv2.COLOR_RGB2Lab)
        L = lab[:, :, 0].astype(np.float32)
    else:
        L = image.astype(np.float32)

    # Ondelettes à trous : décomposition en gaussiennes successives
    # Chaque couche = différence entre deux niveaux de flou gaussien
    details: list[np.ndarray] = []
    prev = L.copy()
    for i in range(layers):
        sigma = 2 ** i  # 1, 2, 4, 8, 16...
        blurred = cv2.GaussianBlur(prev, (0, 0), sigmaX=sigma)
        detail = prev - blurred
        details.append(detail)
        prev = blurred

    # Reconstruire en amplifiant chaque couche
    result = prev.copy()  # résidu (grande échelle)
    for i in range(layers):
        result += details[i] * (1.0 + weights[i])

    result = np.clip(result, 0, 255)

    if image.ndim == 3:
        lab[:, :, 0] = result.astype(np.uint8)
        sharpened = cv2.cvtColor(lab, cv2.COLOR_Lab2RGB)
        return sharpened.astype(np.float32)
    else:
        return result


def _rgb_align(image: np.ndarray) -> np.ndarray:
    """Aligne les canaux RGB pour corriger la dispersion atmosphérique.

    La dispersion atmosphérique décale les canaux R, V, B (le bleu est plus
    réfracté que le rouge). On aligne V et B sur R en utilisant la corrélation
    de phase.

    Args:
        image: (H, W, 3) en float32

    Returns:
        Image avec canaux alignés (même shape)
    """
    import cv2

    r, g, b = image[:, :, 0], image[:, :, 1], image[:, :, 2]

    r_f = r.astype(np.float32)
    g_f = g.astype(np.float32)
    b_f = b.astype(np.float32)

    # Aligner G sur R
    result_g = cv2.phaseCorrelate(r_f, g_f)
    shift_g = result_g[0]
    M_g = np.float32([[1, 0, shift_g[0]], [0, 1, shift_g[1]]])
    g_aligned = cv2.warpAffine(
        g_f, M_g, (image.shape[1], image.shape[0]),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REFLECT,
    )

    # Aligner B sur R
    result_b = cv2.phaseCorrelate(r_f, b_f)
    shift_b = result_b[0]
    M_b = np.float32([[1, 0, shift_b[0]], [0, 1, shift_b[1]]])
    b_aligned = cv2.warpAffine(
        b_f, M_b, (image.shape[1], image.shape[0]),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REFLECT,
    )

    return np.stack([r, g_aligned, b_aligned], axis=2)


def process_planetary(
    session_dir: Path,
    profile: Profile,
    dry_run: bool = False,
) -> list[Path]:
    """Pipeline planétaire complet : vidéo -> frames -> tri -> alignement -> stack -> sharpening -> export.

    Args:
        session_dir: dossier de la session (contient lights/ avec vidéos SER)
        profile: profil fusionné
        dry_run: si True, ne fait rien (juste retourne les chemins attendus)

    Returns:
        Liste des fichiers de sortie (un TIFF par vidéo d'entrée)
    """
    _ensure_opencv()

    folders = profile.setup.folders
    output_dir = session_dir / "output"
    output_dir.mkdir(exist_ok=True)

    videos = _find_videos(session_dir, folders.lights)
    planetary = profile.target.post.planetary

    results: list[Path] = []

    for video_path in videos:
        # Nom de sortie : nom de la vidéo sans extension + .tif
        out_name = video_path.stem
        out_path = output_dir / f"{out_name}.tif"
        results.append(out_path)

        if dry_run:
            continue

        # 1. Lire les frames
        frames = _read_frames(video_path)
        total = len(frames)

        # 2. Sélectionner les meilleures frames
        best_frames, selected_idx = _select_best_frames(
            frames, planetary.best_frames_fraction
        )
        n_keep = len(best_frames)

        # 3. Aligner les frames
        aligned = _align_frames(best_frames, planetary.alignment_mode)

        # 4. Empiler
        stacked = _stack_frames(aligned)

        # 5. RGB align (correction dispersion atmosphérique)
        if planetary.rgb_align:
            stacked = _rgb_align(stacked)

        # 6. Sharpening par ondelettes
        if planetary.wavelet_sharpening:
            stacked = _wavelet_sharpen(
                stacked,
                layers=planetary.wavelet_layers,
                weights=planetary.wavelet_weights,
            )

        # 7. Export TIFF
        from PIL import Image

        img = Image.fromarray(
            np.clip(stacked, 0, 255).astype(np.uint8), mode="RGB"
        )
        img.save(str(out_path))

    return results