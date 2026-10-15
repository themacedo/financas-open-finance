#!/bin/zsh
# Monta o cofre (pede a senha). Use depois de cada reboot enquanto o login automático estiver ligado.
set -euo pipefail
VAULT="${VAULT:-$HOME/Vaults/FinVault.sparsebundle}"
if mount | grep -q " /Volumes/FinVault "; then echo "já montado"; exit 0; fi
echo "No diálogo da senha, NÃO marque 'Lembrar senha nas Chaves': com login automático o cofre abriria sozinho no boot."
hdiutil attach -nobrowse "$VAULT"
