#!/bin/zsh
# Descarrega e remove os 2 LaunchAgents. Não toca no cofre, no ledger nem no Keychain.
set -uo pipefail
GUI="gui/$(id -u)"
for label in local.fin.bot local.fin.sync; do
  launchctl bootout "$GUI/$label" 2>/dev/null && echo "descarregado: $label"
  [[ -f "$HOME/Library/LaunchAgents/$label.plist" ]] && rm "$HOME/Library/LaunchAgents/$label.plist" && echo "removido: $label.plist"
done
echo "Logs ficam em ~/Library/Logs/fin (apague à mão se quiser)."
echo "Segredos e cofre não foram tocados. Para remover tudo, veja 'Desfazer tudo' no README."
