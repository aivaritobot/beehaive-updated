#!/usr/bin/env bash
# Crea un repositorio NUEVO en tu cuenta de GitHub y sube este proyecto (rama main).
# No usa un repo ya existente: gh repo create ... --push
#
# Uso:
#   chmod +x scripts/publish-new-github-repo.sh
#   ./scripts/publish-new-github-repo.sh nombre-del-repo-nuevo
#
# Antes: gh auth login   (una vez en esta máquina)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
NAME="${1:-}"
if [[ -z "$NAME" ]]; then
  echo "Uso: $0 <nombre-repo-nuevo-en-github>" >&2
  echo "Ejemplo: $0 beehAIve-updated" >&2
  exit 1
fi
if git remote get-url origin &>/dev/null; then
  echo "Ya existe remote 'origin'. Si quieres otro repo nuevo, borra el remote o usa otro directorio." >&2
  git remote -v
  exit 1
fi
exec gh repo create "$NAME" --public --source=. --remote=origin --push
