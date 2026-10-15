# Finanças Open Finance

> Finanças pessoais (PF + MEI) a partir do Open Finance, com regras em código e IA só onde ela ajuda.

Python 3.14 · SQLite · Pluggy (Open Finance) · Claude API · Google Sheets · launchd

---

## O problema

Tenho conta pessoal em três bancos, uma conta PJ (MEI), cartão de crédito com parcelas e um benefício de alimentação fora do Open Finance. Os apps de banco mostram cada conta isolada, e as planilhas manuais duram duas semanas.

Eu queria responder, todo dia e sem digitar nada:

- quanto sobra este mês depois das contas fixas e da fatura;
- quanto é meu (PF) e quanto é da empresa (PJ);
- para onde foi o dinheiro, por categoria e por estabelecimento.

## O que ele faz

Um job diário puxa as transações via Pluggy e grava num ledger SQLite dentro de um volume criptografado. Regras em código classificam o que é determinístico (transferência entre contas, pagamento de fatura, estorno, IOF, salário). O que sobra passa pelo dicionário e, só no fim, por um LLM. O resultado vai para o Google Sheets, com abas por mês e um dashboard de gráficos.

```
Pluggy ──► sync ──► ledger SQLite ──► regras ──► dicionário ──► cache ──► LLM ──► Sheets
           (diário)   (cofre AES-256)   (código)    (aprendido)              (só o que
                                                                              ninguém conhece)
```

## Decisões de projeto

- **Valor, data e natureza nunca vêm da IA.** O LLM só sugere a categoria de um estabelecimento desconhecido, com saída presa a um JSON Schema (enum da taxonomia + "Revisar"). A sugestão fica pendente até eu aprovar, e a aprovação vira regra no dicionário.
- **Privacidade antes do LLM.** Nomes de pessoas, CPFs e e-mails nas descrições viram apelidos (`PESSOA_001`) antes de qualquer chamada. Um teste prova que nenhum nome sai.
- **O ledger é a fonte da verdade, e nada é apagado.** Uma transação que some da origem é marcada como removida e sai dos números, mas continua no histórico.
- **Os ids da origem mudam.** Quando data, descrição ou valor mudam, o provedor recria a transação com outro id. O sync religa a linha antiga (por id do provedor, chave natural ou casamento aproximado) e preserva categoria e aprovação. O upsert é idempotente.
- **PF e PJ separados.** O faturamento da empresa e os gastos dela ficam na visão PJ; retiradas viram renda PF. Gastos pessoais pagos pela conta PJ voltam para a visão PF.
- **Fatura de cartão como o banco vê.** Ciclo de fechamento/vencimento configurável, parcelas futuras projetadas, e a fatura aberta ancorada no limite usado que o banco informa.
- **Segredos fora do disco.** Chaves no Keychain do macOS; config sem segredo; dados só no volume criptografado.

## Rodar sem conta nenhuma

```bash
uv sync
uv run fin demo
```

O demo gera dados sintéticos (contas, cartão, parcelas, Pix, estornos), roda o fluxo inteiro offline com um LLM simulado e grava o relatório e as abas em CSV em `demo-out/`.

```bash
uv run pytest -q   # 123 testes
```

## Estrutura

| Módulo | Papel |
|---|---|
| `sync.py`, `pluggy.py` | coleta, upsert idempotente, religação de ids |
| `rules.py` | regras determinísticas (transferência, fatura, estorno, tarifa, salário) |
| `categorize.py`, `llm.py` | dicionário → cache → LLM em lote com JSON Schema |
| `privacy.py` | anonimização antes de qualquer chamada externa |
| `reports.py` | saldos, faturas, contas fixas, visão PF/PJ |
| `sheets.py`, `dashboard.py` | abas por mês, painel e dashboard com filtros |
| `ledger.py` | SQLite e migrações |

Setup completo (cofre, Pluggy, Anthropic, Google, agendamento) em [docs/SETUP.md](docs/SETUP.md).

## Limitações

- Feito para uso próprio, num Mac: o agendamento é launchd e o cofre é um sparsebundle APFS.
- Depende do conector gratuito do Meu Pluggy; os campos que cada banco envia variam (detalhes em `docs/SETUP.md`).
- Benefícios sem Open Finance (como vale-alimentação) entram por CSV manual.
- Não é ferramenta de investimento nem de recomendação financeira; é controle de gastos.

---

<details>
<summary>🇺🇸 English version</summary>

<br>

> Personal finances (individual + sole-proprietor company) from Brazilian Open Finance, with rules in code and AI only where it helps.

**Problem.** Three personal bank accounts, a company account, a credit card with installments and a meal benefit outside Open Finance. Bank apps show each account in isolation, and manual spreadsheets don't last.

**What it does.** A daily job pulls transactions through Pluggy into a SQLite ledger on an encrypted volume. Code rules classify the deterministic cases (internal transfers, card bill payments, refunds, fees, salary). The rest goes through a learned dictionary, a cache and, last, an LLM. Results are published to Google Sheets with monthly tabs and a chart dashboard.

**Design decisions**
- Amount, date and nature never come from the AI. The LLM only suggests a category for unknown merchants, constrained by a JSON Schema; suggestions stay pending until approved, and approvals become dictionary rules.
- Person names, tax IDs and e-mails are replaced by aliases before any LLM call, and a test proves no name leaks.
- The ledger is the source of truth and nothing is hard-deleted.
- Upstream ids change when a transaction is edited; sync relinks the old row and keeps its category and approval. Upserts are idempotent.
- Personal and company views are kept apart, including personal spending paid from the company account.
- Card bills follow the bank's closing/due cycle, with projected installments.
- Secrets live in the macOS Keychain, never in config files.

**Try it without any account:** `uv sync && uv run fin demo` runs the whole pipeline offline on synthetic data with a mocked LLM. `uv run pytest -q` runs 123 tests.

**Limitations.** Built for personal use on a Mac (launchd, APFS vault); relies on the free Meu Pluggy connector; not an investment tool.

</details>
