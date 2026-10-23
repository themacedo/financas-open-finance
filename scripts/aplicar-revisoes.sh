#!/bin/zsh
# Aplica no ledger as revisões feitas na tela Revisar (site, celular, app). O launchd chama a cada 10 minutos.
# Elas esperam na fila da nuvem (assinatura fin-comandos-mac, 7 dias): com o cofre fechado ou sem rede, nada se
# perde, só espera. Uma mensagem só é confirmada (ack) depois de aplicada; se o `fin revisoes` quebrar no meio, o
# Pub/Sub entrega de novo. Com algo aplicado: painel e lista de pendentes novos para o site e para a nuvem.
set -uo pipefail

PROJECT="${0:A:h:h}"
UV="${FIN_UV:-$HOME/.local/bin/uv}"
GCLOUD="${GCLOUD:-/usr/local/bin/gcloud}"
MOUNT="/Volumes/FinVault"
ASSINATURA="projects/fincloud-themacedo/subscriptions/fin-comandos-mac"
RM="gs://fincloud-themacedo-readmodels"
LOG="${FIN_REVISOES_LOG:-$HOME/Library/Logs/fin/revisoes.log}"
LOCK="${TMPDIR:-/tmp}/fin-revisoes.lock"

registrar() { print -r -- "$(date '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG"; }

mount | grep -q " $MOUNT " || exit 0                      # cofre fechado: as revisões esperam na fila
# DENTRO_DO_SYNC=1: chamado pelo sync-diario antes do `fin sync` (ele já tem a trava do ledger e publica no fim)
DENTRO="${DENTRO_DO_SYNC:-}"
[[ -z "$DENTRO" && -d "${TMPDIR:-/tmp}/fin-sync-diario.lock" ]] && exit 0  # o sync está mexendo no ledger: fica para a próxima
mkdir "$LOCK" 2>/dev/null || exit 0
TMP="$(mktemp -d)"
trap 'rmdir "$LOCK" 2>/dev/null; rm -rf "$TMP"' EXIT

"$GCLOUD" pubsub subscriptions pull "$ASSINATURA" --limit=100 --format=json >"$TMP/lote.json" 2>"$TMP/erro.txt" \
  || { registrar "pull falhou: $(head -c 300 "$TMP/erro.txt")"; exit 0; }
[[ "$(tr -d '[:space:]' <"$TMP/lote.json")" == "[]" || ! -s "$TMP/lote.json" ]] && exit 0

if ! "$UV" run --project "$PROJECT" --frozen --no-dev fin revisoes "$TMP/lote.json" >"$TMP/res.txt" 2>>"$LOG"; then
  registrar "fin revisoes falhou; as mensagens voltam para a fila"; exit 1
fi
res="$(head -1 "$TMP/res.txt")"
acks="$(print -r -- "$res" | /usr/bin/python3 -I -c 'import json,sys; print(",".join(json.load(sys.stdin)["ackIds"]))')"
[[ -n "$acks" ]] && "$GCLOUD" pubsub subscriptions ack "$ASSINATURA" --ack-ids="$acks" >/dev/null 2>>"$LOG"
registrar "$(print -r -- "$res" | /usr/bin/python3 -I -c 'import json,sys; r=json.load(sys.stdin); print("aplicadas=%s ignoradas=%s erros=%s" % (r["aplicadas"], r["ignoradas"], r["erros"]), *["%s:%s" % (d.get("id", "-"), d["motivo"]) for d in r["detalhes"] if d["resultado"] == "erro"])')"

# algo aplicado: o `fin revisoes` já regerou painel.json e pendentes.json; publica os dois
if [[ -z "$DENTRO" ]] && print -r -- "$res" | grep -q '"aplicadas": [1-9]'; then
  zsh "$PROJECT/scripts/publicar-painel.sh" >/dev/null 2>&1 || registrar "publicar-painel falhou"
  "$GCLOUD" storage cp --quiet --no-user-output-enabled "$MOUNT/fin/painel.json" "$RM/painel/painel.json" >/dev/null 2>&1 || registrar "envio do painel falhou"
  "$GCLOUD" storage cp --quiet --no-user-output-enabled "$MOUNT/fin/pendentes.json" "$RM/pendentes/pendentes.json" >/dev/null 2>&1 || registrar "envio dos pendentes falhou"
fi
