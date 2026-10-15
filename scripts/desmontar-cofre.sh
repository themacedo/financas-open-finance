#!/bin/zsh
# Para o bot e desmonta o cofre com segurança (sem -force, para não corromper o SQLite).
set -uo pipefail
launchctl bootout "gui/$(id -u)/local.fin.bot" 2>/dev/null && echo "bot parado (rode instalar-launchd.sh para voltar)"
hdiutil detach /Volumes/FinVault
