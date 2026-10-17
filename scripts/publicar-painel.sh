#!/bin/zsh
# Envia o painel.json (só agregados) para o repositório privado que o site lê.
# Chamado pelo sync-diario.sh depois de um sync com sucesso; também roda na mão.
# Sem clone em $DADOS (ou sem painel.json), não faz nada.
set -uo pipefail

FIN="${FIN_DATA_DIR:-/Volumes/FinVault/fin}"
DADOS="${PAINEL_DADOS_DIR:-$FIN/painel-dados}"
GH="${GH:-/usr/local/bin/gh}"

[[ -d "$DADOS/.git" && -f "$FIN/painel.json" ]] || exit 0
cp "$FIN/painel.json" "$DADOS/painel.json"
cd "$DADOS" || exit 1
git add painel.json
git diff --cached --quiet && exit 0   # nada mudou
git -c user.name=themacedo -c user.email=88012938+themacedo@users.noreply.github.com \
  commit -q -m "Atualiza painel ($(date '+%d/%m %H:%M'))"
git -c credential.helper="!$GH auth git-credential" push -q origin HEAD:main
