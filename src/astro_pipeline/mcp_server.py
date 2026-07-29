"""Serveur MCP (Model Context Protocol) pour Astro-1.

Expose les outils du pipeline à un assistant IA (Claude Desktop, Cursor,
ChatGPT…) via stdio. L'IA peut alors :
  - lister les profils disponibles
  - vérifier l'environnement (Siril, GraXpert installés ?)
  - lancer le pipeline sur une session
  - lire le log de la dernière exécution
  - ajuster un paramètre dans un profil YAML
  - voir le profil complet actuellement chargé

Configuration côté client (Claude Desktop, Cursor…) :

    {
      "mcpServers": {
        "astro-1": {
          "command": "uv",
          "args": ["run", "python", "-m", "astro_pipeline.mcp_server"],
          "cwd": "/chemin/vers/astro-1"
        }
      }
    }

Aucun serveur distant : tout tourne en local sur la machine de l'utilisateur.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from astro_pipeline.config import (
    PROFILES_DIR,
    ProfileNotFoundError,
    list_profiles,
    load_profile,
)
from astro_pipeline.engines import graxpert, siril
from astro_pipeline.pipeline import SessionError, run as run_pipeline

# ---------------------------------------------------------------------------
# Serveur MCP
# ---------------------------------------------------------------------------

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    FastMCP = None  # type: ignore[assignment,misc]


if FastMCP is not None:

    mcp = FastMCP(
        "astro-1",
        instructions=(
            "Astro-1 : pipeline automatique de traitement d'astrophotos. "
            "Outils disponibles : list_profiles, doctor, run_pipeline, "
            "read_log, get_profile, adjust_profile."
        ),
    )

    # -- Outils --------------------------------------------------------------

    @mcp.tool()
    def list_profiles_tool() -> str:
        """Liste les profils setup et target disponibles."""
        profiles = list_profiles()
        return json.dumps(profiles, indent=2, ensure_ascii=False)

    @mcp.tool()
    def doctor_tool() -> str:
        """Vérifie que Siril et GraXpert sont installés et trouvables."""
        results: dict[str, dict] = {}

        siril_bin = siril.find_binary()
        if siril_bin:
            results["siril"] = {
                "status": "OK",
                "path": str(siril_bin),
                "version": siril.version(),
            }
        else:
            results["siril"] = {
                "status": "MANQUANT",
                "fix": "brew install --cask siril",
            }

        graxpert_bin = graxpert.find_binary()
        if graxpert_bin:
            results["graxpert"] = {
                "status": "OK",
                "path": str(graxpert_bin),
            }
        else:
            results["graxpert"] = {
                "status": "MANQUANT",
                "fix": "Place GraXpert.app dans /Applications",
            }

        return json.dumps(results, indent=2, ensure_ascii=False)

    @mcp.tool()
    def run_pipeline_tool(
        session: str,
        setup: str,
        target: str,
        dry_run: bool = False,
    ) -> str:
        """Lance le pipeline complet sur une session.

        Args:
            session: Chemin du dossier de session (contient lights/, darks/...).
            setup: Nom du profil setup (sans .yaml).
            target: Nom du profil target (sans .yaml).
            dry_run: Si True, génère les scripts sans exécuter les moteurs.

        Returns:
            JSON avec le statut, les fichiers produits et le chemin du log.
        """
        try:
            profile = load_profile(setup, target)
        except (ProfileNotFoundError, ValueError) as err:
            return json.dumps(
                {"status": "error", "error": str(err)},
                indent=2,
                ensure_ascii=False,
            )

        try:
            result = run_pipeline(
                Path(session).expanduser().resolve(),
                profile,
                dry_run=dry_run,
            )
        except (SessionError, Exception) as err:  # noqa: BLE001
            return json.dumps(
                {"status": "error", "error": str(err)},
                indent=2,
                ensure_ascii=False,
            )

        return json.dumps(
            {
                "status": "ok",
                "stacked": [str(p) for p in result.stacked],
                "exported": str(result.exported) if result.exported else None,
                "scripts": [str(p) for p in result.scripts],
                "log_path": str(result.log_path) if result.log_path else None,
            },
            indent=2,
            ensure_ascii=False,
        )

    @mcp.tool()
    def read_log_tool(session: str) -> str:
        """Lit le dernier fichier de log d'une session.

        Args:
            session: Chemin du dossier de session.

        Returns:
            Le contenu du log le plus récent, ou un message d'erreur.
        """
        logs_dir = Path(session).expanduser().resolve() / "logs"
        if not logs_dir.exists():
            return json.dumps(
                {"error": f"Pas de dossier logs/ dans {session}"},
                ensure_ascii=False,
            )

        logs = sorted(logs_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
        if not logs:
            return json.dumps(
                {"error": "Aucun fichier de log trouvé"},
                ensure_ascii=False,
            )

        latest = logs[-1]
        content = latest.read_text(encoding="utf-8", errors="replace")
        # On limite à 8000 caractères pour ne pas surcharger l'IA
        if len(content) > 8000:
            content = content[-8000:]
        return content

    @mcp.tool()
    def get_profile_tool(target: str) -> str:
        """Récupère le contenu complet d'un profil target YAML.

        Args:
            target: Nom du profil target (sans .yaml).

        Returns:
            Le contenu YAML du profil, tel qu'il est sur disque.
        """
        path = PROFILES_DIR / "targets" / f"{target}.yaml"
        if not path.exists():
            available = sorted(p.stem for p in (PROFILES_DIR / "targets").glob("*.yaml"))
            return json.dumps(
                {
                    "error": f"Profil introuvable : {target}",
                    "available": available,
                },
                ensure_ascii=False,
            )
        return path.read_text(encoding="utf-8")

    @mcp.tool()
    def adjust_profile_tool(
        target: str,
        key_path: str,
        value: str,
    ) -> str:
        """Modifie un paramètre dans un profil target YAML.

        Args:
            target: Nom du profil target (sans .yaml).
            key_path: Chemin du paramètre, avec des points. Exemples :
                "post.stretch.target_bg"
                "post.color.saturation_boost"
                "post.color.rmgreen_type"
            value: Nouvelle valeur. Les booléens sont "true"/"false",
                les nombres sont passés en string ("0.9").

        Returns:
            JSON confirmant la modification ou décrivant l'erreur.
        """
        path = PROFILES_DIR / "targets" / f"{target}.yaml"
        if not path.exists():
            return json.dumps(
                {"error": f"Profil introuvable : {target}"},
                ensure_ascii=False,
            )

        data = yaml.safe_load(path.read_text(encoding="utf-8"))

        # Convertir la valeur string en le bon type
        if value.lower() in ("true", "false"):
            typed_value: object = value.lower() == "true"
        else:
            try:
                typed_value = float(value)
                if typed_value.is_integer():
                    typed_value = int(typed_value)
            except ValueError:
                typed_value = value

        # Naviguer dans le dict et modifier la clé finale
        keys = key_path.split(".")
        current = data
        for key in keys[:-1]:
            if key not in current:
                return json.dumps(
                    {"error": f"Clé introuvable : {key_path} (arrêt à '{key}')"},
                    ensure_ascii=False,
                )
            current = current[key]

        final_key = keys[-1]
        if final_key not in current:
            return json.dumps(
                {"error": f"Clé introuvable : {final_key} dans {key_path}"},
                ensure_ascii=False,
            )

        old_value = current[final_key]
        current[final_key] = typed_value

        # Réécrire le fichier en préservant les commentaires
        path.write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        return json.dumps(
            {
                "status": "ok",
                "profile": target,
                "key": key_path,
                "old_value": old_value,
                "new_value": typed_value,
            },
            indent=2,
            ensure_ascii=False,
        )

    # -- Point d'entrée -------------------------------------------------------

    def main() -> None:
        mcp.run()


else:

    def main() -> None:
        import sys

        print(
            "Le paquet 'mcp' n'est pas installé.\n"
            "Installe-le avec : uv pip install mcp\n"
            "ou : uv sync --extra mcp",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()