#!/bin/zsh
# Sync diário com recuperação: o launchd chama este script às 11:30, ao fazer login e a cada hora.
# Ele só roda o `fin sync` se já passou das 11:30 e ainda não houve sync com sucesso hoje.
# Cofre fechado (depois de um reboot): pede a senha numa janela do macOS; a senha vai direto
# para o hdiutil pela entrada padrão e não é gravada em lugar nenhum. Cancelou: pergunta de novo na hora seguinte.
set -uo pipefail

PROJECT="${0:A:h:h}"
UV="${FIN_UV:-$HOME/.local/bin/uv}"
VAULT="${VAULT:-$HOME/Vaults/FinVault.sparsebundle}"
MOUNT="/Volumes/FinVault"
LOG="${FIN_LOG_FILE:-$HOME/Library/Logs/fin/sync.log}"
HORA_MINIMA="${FIN_SYNC_APOS:-1130}"
LOCK="${TMPDIR:-/tmp}/fin-sync-diario.lock"

avisar() {  # notificação do macOS (título, texto)
  /usr/bin/osascript -e "display notification \"$2\" with title \"$1\"" >/dev/null 2>&1 || true
}

# 1. Cedo demais ou já sincronizado hoje: nada a fazer.
[[ "$(date +%H%M)" -lt "$HORA_MINIMA" ]] && exit 0
grep -q "^$(date +%F) .*sync success" "$LOG" 2>/dev/null && exit 0

# 2. Uma execução por vez (login e o intervalo de 1 h podem coincidir).
mkdir "$LOCK" 2>/dev/null || exit 0
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# 3. Cofre fechado: pede a senha.
if ! mount | grep -q " $MOUNT "; then
  senha="$(/usr/bin/osascript <<'EOF' 2>/dev/null
tell application "System Events"
  activate
  set r to display dialog "O sync de hoje ainda não rodou e o cofre está fechado. Digite a senha do cofre FinVault para abrir e sincronizar." default answer "" with hidden answer with title "Financeiro" buttons {"Agora não", "Abrir e sincronizar"} default button "Abrir e sincronizar" cancel button "Agora não" giving up after 600
  if gave up of r then return ""
  return text returned of r
end tell
EOF
)"
  [[ -z "$senha" ]] && exit 0   # cancelou ou não respondeu: tenta de novo na próxima hora
  if ! printf '%s\0' "$senha" | hdiutil attach -nobrowse -stdinpass "$VAULT" >/dev/null 2>&1; then
    unset senha
    avisar "Financeiro" "Não consegui abrir o cofre (senha incorreta?). Pergunto de novo em 1 hora."
    exit 0
  fi
  unset senha
fi

# 4. Sync.
if "$UV" run --project "$PROJECT" --frozen --no-dev fin sync; then
  avisar "Financeiro" "Sync de hoje concluído."
else
  codigo=$?
  avisar "Financeiro" "O sync falhou (código $codigo). Veja ~/Library/Logs/fin/sync.log."
  exit "$codigo"
fi
