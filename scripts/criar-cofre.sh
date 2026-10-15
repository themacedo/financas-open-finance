#!/bin/zsh
# Cria o cofre APFS criptografado (AES-256, sparse bundle) para o ledger. Você roda; a senha é pedida
# pelo próprio macOS (não passa pela linha de comando nem fica em arquivo).
set -euo pipefail
VAULT="${VAULT:-$HOME/Vaults/FinVault.sparsebundle}"
SIZE="${SIZE:-4g}"
[[ -e "$VAULT" ]] && { echo "já existe: $VAULT"; exit 1; }
mkdir -p "${VAULT:h}"
echo "Criando $VAULT ($SIZE). Escolha uma senha forte e guarde no seu gerenciador de senhas."
hdiutil create -size "$SIZE" -type SPARSEBUNDLE -fs APFS -encryption AES-256 -volname FinVault "$VAULT"
echo "No diálogo da senha, NÃO marque 'Lembrar senha nas Chaves': com login automático o cofre abriria sozinho no boot."
hdiutil attach -nobrowse "$VAULT"
mkdir -p /Volumes/FinVault/fin && chmod 700 /Volumes/FinVault/fin
echo
echo "Pronto. Montado em /Volumes/FinVault (dados em /Volumes/FinVault/fin)."
echo "Tire o volume do Spotlight: Ajustes > Siri e Spotlight > Privacidade do Spotlight > + /Volumes/FinVault"
echo "Conferir: mdutil -s /Volumes/FinVault"
echo "Depois de cada reboot: scripts/montar-cofre.sh"
