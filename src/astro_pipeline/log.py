"""Logging persistant par session.

Chaque exécution du pipeline crée un fichier de log horodaté dans le dossier
de la session : ``logs/astro_YYYY-MM-DD_HHMMSS.log``. Les logs contiennent
toutes les commandes exécutées, leur sortie, et les erreurs rencontrées.

Le module expose un `SessionLogger` qui encapsule à la fois l'écriture fichier
et l'affichage console (via Rich). Comme ça on a un seul appel à faire, et le
log est garanti synchro avec ce que voit l'utilisateur dans le terminal.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from rich.console import Console


class SessionLogger:
    """Logger qui écrit à la fois dans un fichier et dans la console Rich.

    Le fichier de log est créé à la racine de la session, dans un sous-dossier
    ``logs/``. Le nom contient la date et l'heure pour retrouver facilement
    l'exécution qu'on cherche.
    """

    def __init__(self, session_dir: Path, console: Console | None = None) -> None:
        self.console = console or Console()
        self.session_dir = session_dir
        self.log_dir = session_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.log_path = self.log_dir / f"astro_{timestamp}.log"

        # Logger Python standard → fichier
        self._logger = logging.getLogger(f"astro.{session_dir.name}.{timestamp}")
        self._logger.setLevel(logging.DEBUG)
        self._logger.handlers.clear()

        file_handler = logging.FileHandler(self.log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
            )
        )
        self._logger.addHandler(file_handler)

        self._logger.info(f"=== Session : {session_dir} ===")

    def info(self, message: str) -> None:
        """Log INFO + affichage normal dans la console."""
        self._logger.info(message)
        self.console.print(message)

    def step(self, number: int, total: int, name: str, detail: str = "") -> None:
        """Affiche une étape numérotée du pipeline."""
        label = f"[bold cyan]{number}/{total} — {name}[/bold cyan]"
        if detail:
            label += f" [dim]({detail})[/dim]"
        line = f"{number}/{total} — {name}" + (f" ({detail})" if detail else "")
        self._logger.info(line)
        self.console.print(f"\n{label}")

    def success(self, message: str) -> None:
        """Log + affichage vert (succès)."""
        self._logger.info(f"[OK] {message}")
        self.console.print(f"[green]✓[/green] {message}")

    def command(self, cmd: str | list[str]) -> None:
        """Log une commande exécutée (pour rejouage manuel)."""
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        self._logger.info(f"$ {cmd_str}")
        self.console.print(f"      [dim]{cmd_str}[/dim]")

    def warning(self, message: str) -> None:
        """Log WARNING + affichage jaune."""
        self._logger.warning(message)
        self.console.print(f"[yellow]⚠ {message}[/yellow]")

    def error(self, message: str) -> None:
        """Log ERROR + affichage rouge."""
        self._logger.error(message)
        self.console.print(f"[red]{message}[/red]")

    def subprocess_output(self, stdout: str, stderr: str) -> None:
        """Persiste la sortie d'un subprocess dans le fichier de log uniquement.

        On ne l'affiche pas dans la console (ça peut faire des milliers de lignes),
        mais c'est précieux pour le debug post-mortem.
        """
        if stdout:
            self._logger.debug(f"--- stdout ---\n{stdout}")
        if stderr:
            self._logger.debug(f"--- stderr ---\n{stderr}")

    def rule(self, title: str) -> None:
        """Affiche une ligne de séparation avec un titre."""
        self._logger.info(f"--- {title} ---")
        self.console.rule(f"[bold]{title}")

    def close(self) -> None:
        """Ferme proprement le handler de fichier."""
        self._logger.info("=== Fin de session ===")
        for handler in self._logger.handlers:
            handler.close()
            self._logger.removeHandler(handler)