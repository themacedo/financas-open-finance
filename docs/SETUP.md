# Setup e referência

Sistema financeiro pessoal: Itaú, Nubank e Santander via **Meu Pluggy** (Open Finance), Caju manual,
ledger **SQLite** dentro de um cofre APFS criptografado, publicação no **Google Sheets** para o
**Data Studio** (antigo Looker Studio) e consultas no **Telegram**. A IA (Claude Haiku 5.5) só
categoriza estabelecimentos que o dicionário local não conhece.

O que já funciona: `uv run fin demo` roda o fluxo inteiro offline com dados sintéticos.

## Como o fluxo funciona

```
Pluggy (polling diário) ──► sync ──► ledger SQLite (cofre) ──► regras em código ──► categorização ──► Sheets / Telegram
                              │                               transferência interna      dicionário
                              │                               pagamento de fatura        cache
                              └ upsert idempotente            estorno, tarifa, IOF       LLM (só desconhecidos)
                                + religação de ids            salário, investimento      └► Pendente até você aprovar
                                                              Pix para pessoa (apelido)
                                                              projeção de parcelas
```

- **Valor, data, parcela e natureza nunca vêm da IA.** Vêm da Pluggy e de regras em código (`rules.py`).
- **O ledger é a fonte da verdade.** O Sheets é só uma foto para o Data Studio. Nada é apagado de vez:
  uma transação que some da Pluggy é marcada `removed_upstream` e sai dos números.
- **Os ids da Pluggy mudam.** Quando data, descrição ou valor mudam, a Pluggy recria a transação com outro id.
  O sync religa a linha antiga por `providerId`, por `natural_key` ou por casamento aproximado (mesma conta,
  descrição, valor e parcela, data a até 4 dias). A categoria e a sua aprovação são mantidas.
- **Ordem da categorização:** regra → dicionário (`dictionary.seed.yaml` + suas aprovações) → cache por
  descrição → LLM em lote, com JSON Schema estrito (enum da `taxonomy.yaml` + "Revisar").
  Sugestão da IA fica **Pendente**. Quando você aprova, o dicionário aprende e as linhas iguais se resolvem sozinhas.
- **Receita desconhecida não vai para a IA.** A fonte (Cliente PJ, Empresa CLT ou Pessoal) é decisão sua.

### Privacidade

- **O que vai ao LLM:** só `{"id": "d1", "desc": "IFOOD", "tipo": "debito"}`. O id é opaco e vale só
  para aquela requisição. Nunca vão data, conta, saldo, documento ou nome de pessoa.
- **Pix e TED para pessoa física** (CPF que não é o seu, ou transferência sem dados de contraparte) nunca vão à IA.
  Isso vale também quando a Pluggy não manda os dados Open Finance: a descrição (`PIX`, `TED`, `DOC`, `TRANSF...`,
  "Pix no crédito" do cartão) basta para segurar a linha. A pessoa vira `PESSOA_017`, e a categoria vem da tabela
  de contrapartes que você ensina. Pix sem documento e com descrição genérica ("PIX ENVIADO") ganha um apelido só
  dele e fica sempre pendente: aprovar um não ensina os outros.
- **Bloqueio antes do envio**, item a item, na descrição bruta **e** na normalizada:
  - 8 ou mais dígitos, CPF formatado (também com letras de OCR, como `1l1.111.1l1-11`), chave aleatória;
  - `@`, UUID, telefone;
  - cada palavra dos seus termos em `privacy.block_terms` (sem acento);
  - primeiros nomes e sobrenomes comuns, e "DONA", "SEU", "SR", "SRA".

  Item bloqueado vira "Revisar" localmente. No `/pendentes` do Telegram ele aparece como "(bloqueado)".
- **Subcategoria da IA** é só um rótulo curto (letras, até 30 caracteres); qualquer outra coisa é descartada.
- **Auditoria:** o corpo exato de cada requisição fica na tabela `llm_audit`, dentro do cofre.
- **CPF nunca é gravado em claro.** O ledger guarda só um hash com sal. O JSON bruto da Pluggy é gravado sem nomes,
  documentos, números de conta e `reason`.
- **Segredos ficam só no Keychain** (`fin setup`, com getpass, nunca impressos). Não existe `.env`.
- **Bot do Telegram:** responde só ao seu chat privado (chat_id + user_id). O resto é descartado em silêncio.
  Nenhum comando altera dados.
- **Logs** ficam em `~/Library/Logs/fin`, fora do cofre: contagens e o **tipo** do erro, sem traceback e sem a
  mensagem da exceção (que pode trazer descrição ou valor). O traceback só aparece com `fin -v ...` no terminal.
  `sync.log` e `bot.log` giram sozinhos; os `*.out.log`/`*.err.log` do launchd viram `.1` a cada `instalar-launchd.sh`.
- **Google Sheets e Telegram são nuvem de terceiros.** O Sheets recebe os lançamentos (é a publicação para o Data
  Studio); o Telegram não tem criptografia de ponta a ponta. Nada disso vai para a IA.
- **Keychain:** o "Permitir sempre" vale para o binário do Python que o uv usa, não para o `fin`. Qualquer script
  rodado com esse mesmo Python lê os segredos do serviço `fin` sem perguntar. Não rode scripts de terceiros com
  `uv run` neste projeto.

## Comandos

| Comando | O que faz |
|---|---|
| `fin demo [--out DIR]` | Fluxo completo offline com dados sintéticos (padrão `./demo-out`) |
| `fin setup pluggy\|anthropic\|telegram` | Pede os segredos (sem eco) e grava no Keychain |
| `fin setup google --key-file X.json` | Grava o JSON da service account no Keychain; depois apague o arquivo |
| `fin setup telegram-discover` | Descobre seu chat_id (depois de você mandar /start ao bot) e grava a allowlist |
| `fin setup dictionary` | Carrega o dicionário inicial genérico |
| `fin setup status` | Mostra quais segredos existem (nunca os valores) |
| `fin sync` | Caju, Pluggy, regras, categorização, Sheets e alertas no Telegram |
| `fin categorize` | Só a categorização |
| `fin review [list]` | Lista os pendentes |
| `fin review approve t000123 --categoria Alimentação --essencial Supérfluo` | Aprova e ensina o dicionário |
| `fin review apelido PESSOA_001 Aluguel` | Apelido local de uma contraparte (aparece no Sheets) |
| `fin publish [--csv DIR]` | Publica no Sheets (ou em CSV) |
| `fin report [--mes AAAA-MM] [--faturas] [--posso-comprar 1200 --parcelas 10] [--analista]` | Relatórios no terminal |
| `fin caju [--file X]` | Importa o CSV/YAML do Caju |
| `fin bot` | Bot do Telegram (long polling) |
| `fin doctor` | Confere ambiente, cofre, segredos e launchd, sem rede |

No Telegram: `/resumo [AAAA-MM]` (PF, PJ e consolidado), `/faturas`, `/posso_comprar 1.200,50 10x` (só PF), `/pendentes`.

Testes: `uv run pytest -q`.

## Setup (na ordem)

Tempo total estimado: de 3 a 4 horas. A primeira revisão de pendentes é a parte mais longa.

### 0. Antes de começar

- **Desligue o login automático:** Ajustes > Usuários e Grupos > Iniciar sessão automaticamente: Desligado.
  Hoje ele está ligado. Com ele ligado, qualquer pessoa que reinicie o Mac abre o Keychain e todos os segredos.
- **Mova o projeto para fora de `~/Documents`, por exemplo `~/Projetos/Financeiro-IA`.** Dois motivos:
  - privacidade: com o iCloud "Mesa e Documentos" ligado, o `config.yaml` (seu nome em `block_terms`, CNPJs dos
    empregadores, custos fixos, id da planilha, itemIds) sobe para a Apple;
  - `~/Documents` é pasta protegida pelo TCC, e o launchd pode ser bloqueado ao ler o projeto.

  ```
  mkdir -p ~/Projetos && mv ~/Documents/Financeiro-IA ~/Projetos/
  cd ~/Projetos/Financeiro-IA && rm -rf .venv && ~/.local/bin/uv sync
  ```
  O `.venv` guarda caminhos absolutos da pasta antiga: sem apagar, o `fin` pode quebrar ou importar do lugar errado.
- **Crie o `config.yaml` já agora:** `cp config.example.yaml config.yaml`. Os passos 2, 4 e 6 colam valores nele.

### 1. Cofre criptografado (5 min)

1. Rode `scripts/criar-cofre.sh`.
   - Ele cria `~/Vaults/FinVault.sparsebundle` (APFS, AES-256).
   - A senha é pedida pelo macOS. Guarde-a no gerenciador de senhas, **não no Keychain**.
   - No diálogo, **não marque "Lembrar senha nas Chaves"**: com login automático, o cofre passaria a abrir sozinho no boot.
2. Tire o volume do Spotlight: Ajustes > Siri e Spotlight > Privacidade do Spotlight > `+` `/Volumes/FinVault`.
   Confira com `mdutil -s /Volumes/FinVault`.
3. Depois de cada reboot, rode `scripts/montar-cofre.sh`. Sem o cofre:
   - o sync se recusa a rodar (`require_mount`), em vez de gravar dado fora do cofre, e **avisa no Telegram**;
   - o bot responde "cofre desmontado".
   - Se `data_dir` (ou `FIN_DATA_DIR`) apontar para fora de `require_mount`, nada roda.

### 2. Meu Pluggy + Pluggy (40-50 min, tudo dentro dos 15 dias de trial)

1. Em **meu.pluggy.ai**, crie a conta e conecte Itaú, Nubank e Santander.
   - Cada banco pede uma autorização Open Finance no app do próprio banco.
   - **Não reconecte o mesmo banco várias vezes no mês.** O limite de buscas de histórico é por CPF e banco,
     e é compartilhado com outros apps.
2. Em **dashboard.pluggy.ai**, entre com a mesma conta. Isso inicia o trial, sem cartão.
3. Em Customization > Connectors, se estiver em "Custom selection", ligue o **MeuPluggy**.
4. Em Applications, crie uma aplicação de **Development**.
   - Copie o Client ID.
   - O **Client Secret aparece uma vez só**: deixe a tela aberta e cole direto no `fin setup pluggy` (passo 6).
5. Em Applications > sua aplicação > Demo > Connect Account, escolha **MeuPluggy**. Não escolha o banco nem o Sandbox.
   - Faça isso **3 vezes, uma por banco**.
   - Se pedir CPF ou aprovação no banco, você escolheu o conector errado.
6. Em Demo > Connected Items, copie os 3 itemIds para `config.yaml`, cada um com o rótulo do banco.

### 3. Anthropic (10 min)

1. Em platform.claude.com:
   - crie a organização e coloque um crédito pequeno (US$ 5 sobra; o custo estimado é menor que US$ 0,10 por mês);
   - defina um limite de gasto mensal baixo;
   - crie uma API key só para este uso.
2. Rode `uv run fin setup anthropic` e cole a chave.

### 4. Google Sheets + service account (15 min)

1. Em console.cloud.google.com, crie o projeto `fin-pessoal`. **Não vincule faturamento.**
2. Em APIs e serviços > Biblioteca, ative só a **Google Sheets API**.
3. Em IAM > Contas de serviço, crie `fin-sheets-writer` **sem papel nenhum**.
4. Em Chaves > Adicionar chave > JSON, baixe o arquivo. Ele só pode ser baixado uma vez.
5. Rode `uv run fin setup google --key-file ~/Downloads/<arquivo>.json`. Ele imprime só o `client_email`.
   Depois **apague o arquivo**.
6. Crie uma planilha no seu Drive.
   - Em Arquivo > Configurações > Localidade, escolha Brasil.
   - Compartilhe como **Editor** com o `client_email`, sem notificar.
   - Copie o id da URL (`/d/<id>/edit`) para `sheets.spreadsheet_id`.
7. As abas, as proteções e as validações são criadas no primeiro `fin publish`. O script grava em modo RAW
   (nada é interpretado: "3/10" não vira data, texto com "=" não vira fórmula). São elas:
   - `lancamentos`, `resumo_mensal`, `parcelas_futuras` e `dicionario`: escritas pelo script, protegidas com aviso;
   - `revisoes`: você edita `categoria_final`, `essencial`, `necessidade` e `fonte`, e muda `status` para **Aprovado**.

### 5. Telegram (5 min)

1. No @BotFather:
   - `/newbot`: o username termina em `bot`. Copie o token.
   - `/setcommands`: `resumo`, `faturas`, `posso_comprar`, `pendentes`.
   - `/setjoingroups`: Disable.
2. Rode `uv run fin setup telegram` e cole o token.
3. Mande `/start` para o seu bot e rode `uv run fin setup telegram-discover`. Confirme o seu chat.
4. Não use curl com o token: ele fica no histórico do shell.

### 6. Configuração (15 min)

1. No `config.yaml` criado no passo 0, preencha:
   - `pluggy.items`: os itemIds do passo 2;
   - `sheets.spreadsheet_id`;
   - `fixed_costs`: aluguel, contas, assinaturas;
   - `privacy.block_terms`: nome, cada sobrenome e apelidos, separados;
   - `income.salary_day`.
2. `income.sources` e `cards.nubank.issuer_cnpj` precisam do CNPJ real. Rode o primeiro sync e veja
   `fin review list` ou o ledger, depois volte aqui.
3. Rode `uv run fin setup dictionary` para carregar o dicionário genérico.
4. Rode `uv run fin doctor`.

### 7. Primeiro sync (30-60 min, quase tudo revisão)

1. Rode `uv run fin sync --no-publish`. O primeiro sync traz até 12 meses.
2. Rode `uv run fin review list` e aprove os pendentes. Cada aprovação ensina o dicionário ou a contraparte.
   O Sheets (aba `revisoes`) também serve para isso.
3. Rode `uv run fin report` e confira contra o extrato de um mês. **Confira as datas** (ver TODO 9).
4. Rode `uv run fin publish`.

### 8. Data Studio (30-60 min)

Em datastudio.google.com: Criar > Fonte de dados > Google Sheets. Crie uma fonte por aba e marque
"usar a primeira linha como cabeçalho". Na fonte, mude o tipo de `data` para **Data (AAAA-MM-DD)** e o de
`competencia`/`mes` para **Ano e mês (AAAA-MM)**: elas chegam como texto. Sugestão de páginas:

1. **Visão geral** (`resumo_mensal`): receita, despesa e saldo do mês, % da renda em parcelas, série de 12 meses.
2. **Raio-x por categoria** (`lancamentos`, com filtro natureza = Despesa):
   - barras do mês contra a média de 3 meses;
   - tabela de categoria para estabelecimento;
   - filtros Essencial/Supérfluo, fixo/variável e conta.
3. **Radar de parcelas** (`parcelas_futuras`): barras empilhadas dos próximos 12 meses e a data "livre a partir de".

**Não compartilhe o relatório.** Com credencial do proprietário, quem tiver o link vê tudo.
Não renomeie colunas no Sheets: o Data Studio depende dos cabeçalhos.

### 9. launchd (10 min)

1. Rode `scripts/instalar-launchd.sh`. Ele cria `local.fin.sync` (diário às 11:30, com recuperação) e `local.fin.bot` (KeepAlive).
2. Rode `launchctl kickstart gui/$(id -u)/local.fin.sync` **com a tela à vista**.
   - Se aparecer o diálogo do Keychain, clique em **Permitir sempre**. Ele volta a aparecer depois de atualizar o Python.
   - Se aparecer o pedido de acesso a pastas, documentos ou **volumes removíveis** (TCC, por causa do `/Volumes/FinVault`), autorize.
   - Se ninguém responder ao diálogo, o sync se encerra sozinho em 30 min (código 124 no `fin doctor`) para não travar os dias seguintes.
3. Ainda com a tela à vista, mande `/resumo` ao bot: o processo do bot é outro e pode pedir as mesmas permissões.
   Se algo foi bloqueado em silêncio: Ajustes > Privacidade e Segurança > Arquivos e Pastas.
4. Rode `uv run fin doctor`: ele mostra o último código de saída de cada job.
5. Logs ficam em `~/Library/Logs/fin/`. Para remover: `scripts/desinstalar-launchd.sh`.
6. **Recuperação:** o `local.fin.sync` chama `scripts/sync-diario.sh` às 11:30, ao fazer login e de hora em hora.
   O script só roda o sync depois das 11:30 e se ainda não houve sync com sucesso no dia. Assim, um Mac desligado no
   horário sincroniza ao ligar.
   - Cofre fechado (depois de reiniciar): abre uma janela do macOS pedindo a senha do cofre. A senha vai direto para o
     `hdiutil` e não é gravada. "Agora não" pergunta de novo na hora seguinte. Na primeira vez, o macOS pede
     permissão para o System Events: autorize.
   - Ao terminar ou falhar, aparece uma notificação do macOS.
   - O sync busca a partir do último sync com sucesso, então um dia perdido não deixa buraco.
7. Falhas do sync (cofre desmontado, rede fora, erro inesperado) chegam como alerta no Telegram.
   Token do bot inválido ou segredo ausente: o bot sai e o launchd **não** o relança em laço; corrija e rode
   `scripts/instalar-launchd.sh` de novo.

### Desfazer tudo

```
scripts/desinstalar-launchd.sh
for a in pluggy_client_id pluggy_client_secret anthropic_api_key google_service_account_json \
         telegram_bot_token telegram_allowed_chat_id telegram_allowed_user_id; do
  security delete-generic-password -s fin -a "$a"
done
scripts/desmontar-cofre.sh
```
Depois apague à mão, se quiser: `~/Vaults/FinVault.sparsebundle` (o ledger), `~/Library/Logs/fin`, a chave da
service account no Google Cloud, a API key na Anthropic e o bot no @BotFather (`/deletebot`).

## Caju

Crie `caju.csv` dentro do diretório de dados, separado por `;` e com valores no formato BRL:

```
data;descricao;valor;categoria;fonte
2026-10-01;Crédito Caju;1000,00;Salário CLT;Empresa CLT
2026-10-31;Gastos do mês;-812,40;;
```

Positivo é crédito, negativo é gasto. Gasto sem categoria vira Alimentação. Pode ser só o total do mês.
O `fin sync` importa sozinho, e reimportar não duplica. O saldo do Caju aparece separado e **não** entra no saldo livre.

## Regras de cálculo

- **Sinal:** vem do `type` (DEBIT = saída, CREDIT = entrada). O sinal do `amount` só é usado se faltar o `type`;
  divergência entre os dois gera alerta no Telegram.
- **Data:** `T00:00:00Z` é data pura; qualquer outro horário é convertido para Brasília antes de pegar o dia.
- **Competência:**
  - compra no cartão conta no mês da fatura (`billForecastDate`; sem ele, usa o fechamento e o vencimento do config);
  - parcela que chega com a data da compra: parcela n entra n−1 faturas depois da compra;
  - conta bancária conta pela data.
- **Cartão:** crédito no cartão nunca é receita. Ou é pagamento de fatura (pareado com o débito no banco, mesmo valor,
  até 3 dias), ou é ajuste/estorno (despesa negativa, pendente). "Saldo restante da fatura anterior" (pagamento
  parcial) é Transferência; juros, IOF e multa continuam em Impostos/Taxas.
- **Pagamento de fatura no banco:** precisa do CNPJ do emissor ou de "fatura" junto de cartão/crédito/nome do banco.
  "PAGAMENTO FATURA VIVO" e "PAG CARTORIO" continuam despesa.
- **Transferência entre suas contas:** mesmo valor, até 2 dias. Automática só se um dos lados for comprovadamente
  seu (documento ou conta); senão vira sugestão pendente.
- **Pré-autorização:** um PENDING que some e volta como POSTED com outro id e valor até 20% diferente (posto,
  hotel) é religado, não duplicado.
- **Despesa:** soma das saídas com natureza Despesa. Estorno reduz a categoria da compra original.
  Transferência (entre contas próprias e pagamento de fatura) nunca conta.
- **Taxa de poupança** = (receitas − despesas) / receitas.
- **Parcelas comprometidas:** as parcelas reais futuras que já chegaram mais as projeções.
  A parcela n/N projeta n+1 até N, e a projeção some quando a parcela real chega. Também some quando a fatura do mês
  já fechou sem a parcela, ou quando chega estorno do mesmo estabelecimento perto do valor restante.
- **Fatura paga:** pagamentos entre o vencimento anterior + 5 dias e este vencimento + 5 dias (cobre antecipado e atrasado).
- **Próximo salário:** se o salário do mês já caiu antes do `salary_day`, o próximo é o do mês seguinte.
- **Saldo livre** = saldo das contas − faturas que vencem antes do próximo salário − custos fixos pendentes até lá
  (inclusive os do mês seguinte que vencem antes do salário).
- **`/posso_comprar` à vista:** compara com o saldo livre e pede folga de 10% da renda.
- **`/posso_comprar` parcelado:** para cada mês, faz renda de referência − fixos − média variável de 3 meses − parcelas já comprometidas.
  Fixos = o maior entre `fixed_costs` do config e a média de 3 meses das despesas com necessidade fixo (assinaturas etc.).

## PF e PJ (MEI)

- **Titular vem da conta:** cada item em `pluggy.items` aceita `owner: pf|pj` (padrão `pf`). O Nubank Empresas
  (`nubank_pj`) é `pj`. O ledger guarda o titular em `accounts.owner` (bancos antigos migram sozinhos para `pf`),
  e a `local_key` da PJ leva `:pj:`, então Nubank PF e Nubank PJ nunca se misturam. Mudar o `owner` de um item já
  sincronizado religa o histórico, sem duplicar.
- **PF↔PJ continua Transferência**, pareada como qualquer transferência entre contas suas, mas com rótulo:
  PJ→PF = **Retirada PJ→PF** (distribuição de lucro), PF→PJ = **Aporte PF→PJ**. Sem o outro lado, o rótulo vem do
  CPF/CNPJ ou da conta de destino. Se a conta PJ estiver no seu CPF, só o número da conta decide.
- **Categorias da PJ:** Faturamento PJ (cliente pelo CNPJ em `income.sources`; `categoria: PJ` numa conta `pj`
  vira Faturamento PJ), Impostos/DAS (dicionário: DAS, SIMEI, PGMEI, Simples Nacional) e Taxas bancárias PJ (tarifas da conta PJ).
- **Números sem dupla contagem:**
  - Consolidado: Retirada e Aporte não contam.
  - Visão PF: a retirada é receita com fonte "Retirada PJ", separada do salário CLT; o aporte sai do saldo, sem ser despesa.
  - Visão PJ: a retirada sai e o aporte entra no saldo, sem virar faturamento nem despesa.
  - Saldo PF + saldo PJ = saldo consolidado.
- **`/resumo`** mostra PF, PJ e consolidado. **`/posso_comprar`, saldo livre, parcelas e fixos usam só a PF**: saldo
  das contas PF, cartão (que é PF) e renda PF, que inclui as retiradas. O saldo da conta PJ não entra: dinheiro da
  empresa só é seu depois da retirada. `/faturas` não mudou.
- **Sheets:** `lancamentos` ganhou a coluna `titular` (PF/PJ). `resumo_mensal` tem uma linha por mês e titular
  (Consolidado, PF e PJ) e as colunas `retiradas_pj`/`aportes_pj`. **No Data Studio, filtre `titular`**: somar todas
  as linhas conta o mês duas vezes.

## TODO / não confirmado

1. **Campos Open Finance no item MeuPluggy:** não está confirmado se chegam `providerId`, `operationType`,
   `billId`, `billForecastDate` e `paymentType`. O código funciona com todos nulos: cai para `natural_key`, regex
   e fechamento/vencimento do config. Conferir no primeiro sync real.
2. **`GET /bills` no item MeuPluggy:** pode não funcionar. Sem ele, `/faturas` usa as transações e o `creditData`.
3. **itemId ao reconectar um banco:** não se sabe o que acontece. Se mudar, troque no `config.yaml`.
   A `account_local_key` (banco:subtipo:4 últimos dígitos) evita duplicar o histórico.
4. **Trial da Pluggy:** a doc oficial e a do Actual se contradizem sobre o que deixa de funcionar depois dos 15 dias.
   Faça todo o setup dentro do trial.
5. **Sinal do valor:** o código confia no `type` (DEBIT/CREDIT). Se o primeiro sync alertar divergência de sinal,
   confira umas linhas contra o extrato.
6. **Parcelas no cartão:** não está documentado se cada banco manda todas as parcelas de uma vez ou mês a mês.
   O agrupamento usa conta, descrição, total de parcelas e mês-base, com tolerância de valor.
7. **`billForecastDate`:** está tratado como o mês do vencimento da fatura. Confirmar com uma fatura real.
8. **CPF da contraparte mascarado** em algum banco: se vier sem os 11 dígitos, vira `unknown` e é tratado como pessoa.
9. **Datas:** meia-noite UTC é tratada como data pura; outros horários são convertidos para Brasília. Confira umas
   compras de fim de mês contra o extrato.
10. **Caixinhas do Nubank:** não está documentado como aparecem nas transações. Se vierem como Pix para si mesmo,
    viram Transferência. Se não, aprove como Investimento.
11. **Schema e modelo da Anthropic:** não foram testados contra a API real (os testes usam transporte simulado).
    Validar com o primeiro `fin categorize`. Erro 400 vira alerta.
12. **Escopo do Sheets:** usamos `spreadsheets`, sem `drive`. `gspread` 6.2.1 não declara suporte ao Python 3.14,
    mas funcionou nos testes de import.
13. **TCC e Keychain com o launchd:** testar com `kickstart`. Ver a seção 9.
14. **Matching de transferência:** pares com mesmo valor e até 2 dias de diferença. Dois Pix iguais no mesmo dia
    para pessoas diferentes podem parear errado se não houver dados de contraparte. Revise pelo `par_id`.
15. **Caju:** não tem cobertura Open Finance, então continua manual.
