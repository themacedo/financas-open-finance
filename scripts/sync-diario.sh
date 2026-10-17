#!/bin/zsh
# Sync diário com recuperação: o launchd chama este script às 18:30, ao fazer login e a cada hora.
# 18:30 porque o Pluggy busca nos bancos uma vez por dia, perto das 17:20-18:05; antes disso o sync só
# leria os dados da tarde anterior. Ele só roda o `fin sync` se não houve sync com sucesso desde as
# 18:30 mais recentes (hoje, se já passou; senão, ontem). Assim um Mac desligado à noite sincroniza ao ligar.
# Cofre fechado (depois de um reboot): pede a senha numa janela do macOS; a senha vai direto
# para o hdiutil pela entrada padrão e não é gravada em lugar nenhum. Cancelou: pergunta de novo na hora seguinte.
set -uo pipefail

PROJECT="${0:A:h:h}"
UV="${FIN_UV:-$HOME/.local/bin/uv}"
VAULT="${VAULT:-$HOME/Vaults/FinVault.sparsebundle}"
MOUNT="/Volumes/FinVault"
LOG="${FIN_LOG_FILE:-$HOME/Library/Logs/fin/sync.log}"
HORA_MINIMA="${FIN_SYNC_APOS:-1830}"
LOCK="${TMPDIR:-/tmp}/fin-sync-diario.lock"

avisar() {  # notificação do macOS (título, texto)
  /usr/bin/osascript -e "display notification \"$2\" with title \"$1\"" >/dev/null 2>&1 || true
}

# 1. Já sincronizado depois das 18:30 mais recentes: nada a fazer.
if [[ "$(date +%H%M)" -ge "$HORA_MINIMA" ]]; then dia="$(date +%F)"; else dia="$(date -v-1d +%F)"; fi
marco="$dia ${HORA_MINIMA[1,2]}:${HORA_MINIMA[3,4]}"
ultimo="$(grep ' sync success' "$LOG" 2>/dev/null | tail -1 | cut -c1-16)"
[[ -n "$ultimo" && ! "$ultimo" < "$marco" ]] && exit 0

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
  # 5. Painel: manda o painel.json novo para o repositório privado (falha só avisa).
  zsh "$PROJECT/scripts/publicar-painel.sh" || avisar "Financeiro" "Sync ok, mas o painel não foi atualizado."
  avisar "Financeiro" "Sync de hoje concluído."
else
  codigo=$?
  avisar "Financeiro" "O sync falhou (código $codigo). Veja ~/Library/Logs/fin/sync.log."
  exit "$codigo"
fi
