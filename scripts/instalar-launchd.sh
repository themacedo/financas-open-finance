#!/bin/zsh
# Instala os 2 LaunchAgents (sync diário 18:30 com recuperação ao ligar e de hora em hora, e bot; LABELS=local.fin.sync para só o sync). Você roda; nada é feito com sudo.
set -euo pipefail
PROJECT="${0:A:h:h}"
UV="${UV:-$HOME/.local/bin/uv}"
AGENTS="$HOME/Library/LaunchAgents"
GUI="gui/$(id -u)"

[[ -x "$UV" ]] || { echo "uv não encontrado em $UV (defina UV=/caminho/uv)"; exit 1; }
for v in "$PROJECT" "$UV" "$HOME"; do
  # os caminhos entram no XML do plist e no sed: recuse caracteres que quebrariam um ou outro
  [[ "$v" == *[\&\<\>\#\"\']* ]] && { echo "caminho com caractere não suportado (& < > # aspas): $v"; exit 1; }
done
[[ -f "$PROJECT/config.yaml" ]] || { echo "falta $PROJECT/config.yaml (copie de config.example.yaml)"; exit 1; }
case "$PROJECT" in
  "$HOME/Documents"*|"$HOME/Desktop"*|"$HOME/Downloads"*)
    echo "AVISO: o projeto está numa pasta protegida pelo TCC ($PROJECT)."
    echo "       Na primeira execução o macOS pode pedir permissão (ou bloquear em silêncio)."
    echo "       Recomendado: mover para ~/Projetos/Financeiro-IA e rodar este script de lá."
    read "?Continuar mesmo assim? [s/N] " ans; [[ "$ans" == "s" ]] || exit 1;;
esac

mkdir -p "$AGENTS" "$HOME/Library/Logs/fin"
# stdout/stderr do launchd não têm rotação: guarda só a versão anterior de cada um
for f in "$HOME"/Library/Logs/fin/*.out.log(N) "$HOME"/Library/Logs/fin/*.err.log(N); do
  mv -f "$f" "$f.1"
done
"$UV" sync --project "$PROJECT" --frozen >/dev/null

for label in ${=LABELS:-local.fin.sync local.fin.bot}; do
  dst="$AGENTS/$label.plist"
  sed -e "s#__PROJECT__#$PROJECT#g" -e "s#__UV__#$UV#g" -e "s#__HOME__#$HOME#g" \
      "$PROJECT/launchd/$label.plist.template" > "$dst"
  chmod 644 "$dst"
  plutil -lint "$dst"
  launchctl bootout "$GUI/$label" 2>/dev/null || true
  # bootout volta antes de o serviço terminar de descarregar: espere até ~10 s
  for _ in {1..20}; do launchctl print "$GUI/$label" >/dev/null 2>&1 || break; sleep 0.5; done
  if ! launchctl bootstrap "$GUI" "$dst"; then
    sleep 2
    launchctl bootstrap "$GUI" "$dst"
  fi
  echo "carregado: $label"
done

echo
echo "Agora, com a tela à vista (o macOS pode pedir acesso ao Keychain: clique em 'Permitir sempre'):"
echo "  launchctl kickstart $GUI/local.fin.sync"
echo "  tail -f ~/Library/Logs/fin/sync.log"
echo "Estado:  launchctl print $GUI/local.fin.sync | head -30"
