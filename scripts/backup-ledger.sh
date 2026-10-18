#!/bin/zsh
# Backup diário do cofre: cópia consistente do ledger + plano, dicionário, logos e config, compactada e
# criptografada com age, numa pasta sincronizada com a nuvem (Google Drive por padrão). A nuvem só vê um
# arquivo ilegível; para abrir é preciso a chave privada (backup-chave.txt, no cofre e numa cópia sua fora do Mac).
# Mantém os últimos 30 dias e o dia 1º de cada mês por 12 meses. Cada backup é aberto e conferido logo depois.
#
#   scripts/backup-ledger.sh              faz o backup de hoje (substitui o de hoje, se já houver)
#   scripts/backup-ledger.sh --restaurar arquivo.tar.gz.age pasta-destino
set -uo pipefail

PROJECT="${0:A:h:h}"
FIN="${FIN_DATA_DIR:-/Volumes/FinVault/fin}"
CHAVE="$FIN/backup-chave.txt"
DESTINO="${FIN_BACKUP_DIR:-$(print -l "$HOME"/Library/CloudStorage/GoogleDrive-*/"My Drive"(N) | head -1)/Financeiro-backup}"
MANTER_DIAS=30
MANTER_MESES=12

falha() { echo "backup: $*" >&2; exit 1; }
[[ -d "$FIN" && -f "$FIN/ledger.db" ]] || falha "cofre fechado ou ledger ausente ($FIN)"
[[ -f "$CHAVE" ]] || falha "chave ausente ($CHAVE); gere com: age-keygen -o $CHAVE"
command -v age >/dev/null || falha "age não instalado"

tmp="$(mktemp -d "$FIN/.backup-XXXXXX")" || falha "não consegui criar pasta temporária no cofre"
trap 'rm -rf "$tmp"' EXIT

if [[ "${1:-}" == "--restaurar" ]]; then
  [[ -f "${2:-}" && -n "${3:-}" ]] || falha "uso: $0 --restaurar arquivo.tar.gz.age pasta-destino"
  mkdir -p "$3" && age -d -i "$CHAVE" "$2" | tar -xzf - -C "$3" || falha "não consegui abrir $2"
  echo "restaurado em $3: $(ls "$3" | tr '\n' ' ')"
  exit 0
fi

[[ "$DESTINO" == /* && -d "${DESTINO:h}" ]] || falha "pasta do Google Drive não encontrada (defina FIN_BACKUP_DIR)"
mkdir -p "$DESTINO" || falha "não consegui criar $DESTINO"

# 1. Cópia consistente (a API de backup do SQLite funciona mesmo com o ledger em uso, modo WAL).
mkdir -p "$tmp/fin"
sqlite3 "$FIN/ledger.db" ".backup '$tmp/fin/ledger.db'" || falha "sqlite .backup falhou"
[[ "$(sqlite3 "$tmp/fin/ledger.db" 'PRAGMA integrity_check')" == "ok" ]] || falha "cópia do ledger corrompida"
linhas="$(sqlite3 "$tmp/fin/ledger.db" 'SELECT COUNT(*) FROM transactions')"
for f in plano.yaml dicionario_pessoal.yaml logos; do [[ -e "$FIN/$f" ]] && cp -R "$FIN/$f" "$tmp/fin/"; done
[[ -f "$PROJECT/config.yaml" ]] && cp "$PROJECT/config.yaml" "$tmp/fin/"

# 2. Compacta e criptografa só para a chave pública (o backup não precisa da chave privada para ser feito).
publica="$(age-keygen -y "$CHAVE")" || falha "não consegui ler a chave"
nome="fin-$(date +%F).tar.gz.age"
tar -czf - -C "$tmp" fin | age -r "$publica" -o "$tmp/$nome" || falha "falha ao criptografar"

# 3. Confere: abre o arquivo criptografado e compara o ledger com a cópia.
mkdir -p "$tmp/teste"
age -d -i "$CHAVE" "$tmp/$nome" | tar -xzf - -C "$tmp/teste" || falha "o backup não abre"
[[ "$(sqlite3 "$tmp/teste/fin/ledger.db" 'SELECT COUNT(*) FROM transactions')" == "$linhas" ]] || falha "o backup não confere"

mv -f "$tmp/$nome" "$DESTINO/$nome" || falha "não consegui gravar em $DESTINO"

# 4. Rotação: últimos 30 dias + o dia 1º de cada um dos últimos 12 meses.
limite_dias="$(date -v-${MANTER_DIAS}d +%F)"
limite_meses="$(date -v-${MANTER_MESES}m +%F)"
for f in "$DESTINO"/fin-????-??-??.tar.gz.age(N); do
  d="${${f:t}[5,14]}"
  [[ "$d" > "$limite_dias" || "$d" == "$limite_dias" ]] && continue
  [[ "${d[9,10]}" == "01" && ( "$d" > "$limite_meses" || "$d" == "$limite_meses" ) ]] && continue
  rm -f "$f"
done

echo "backup ok: $nome ($(du -h "$DESTINO/$nome" | cut -f1), $linhas lançamentos) em ${DESTINO/#$HOME/~}"
