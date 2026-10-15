"""Merchant description normalization.

"IFD*IFOOD 1234 SAO PAULO" -> "IFOOD". The result is the key used by the
dictionary, the cache and (after the privacy guard) the only text sent to the LLM.
"""

from __future__ import annotations

import re
import unicodedata

# Acquirer / gateway prefixes before "*" that are not the merchant itself.
ACQUIRER_PREFIXES = {
    "IFD", "PG", "PAG", "MP", "MERCADOPAGO", "MERCPAGO", "EC", "PICPAY", "PAYPAL", "PP",
    "SUMUP", "STONE", "CIELO", "GETNET", "PAGSEGURO", "PAGSEG", "EBANX", "DL", "ZP",
    "SHPP", "HOTMART", "HTM", "ASAAS", "IUGU", "PAGARME", "INFINITEPAY", "TON", "APPLE",
}

# Generic bank-statement prefixes that say nothing about the merchant.
GENERIC_PREFIXES = [
    "COMPRA CARTAO DEBITO", "COMPRA CARTAO", "COMPRA NO DEBITO", "COMPRA DEBITO",
    "COMPRA ELO", "COMPRA VISA", "COMPRA MASTER", "PAG BOLETO", "PAGTO BOLETO",
    "PAGAMENTO BOLETO", "PAGAMENTO DE BOLETO", "PAGTO", "PIX ENVIADO", "PIX RECEBIDO",
    "PIX TRANSF", "PIX QRS", "PIX QR", "PIX", "TED ENVIADA", "TED RECEBIDA", "TED",
    "DOC", "TRANSF", "DEB AUTOMATICO", "DEBITO AUTOMATICO",
]

# Words that say nothing about who/what was paid: a description made only of these is "generic"
# (never learned as a dictionary entry or as a counterpart).
GENERIC_WORDS = {
    "PIX", "TED", "DOC", "TEV", "TRANSF", "TRANSFERENCIA", "TRANSFERENCIAS", "ENVIADO", "ENVIADA", "RECEBIDO",
    "RECEBIDA", "PELO", "PELA", "DE", "DA", "DO", "DAS", "DOS", "PARA", "NO", "NA", "EM", "CREDITO", "DEBITO",
    "PAGAMENTO", "PAGTO", "PAG", "PGTO", "BOLETO", "BOLETOS", "QR", "QRS", "CODE", "CONTA", "CORRENTE", "POUPANCA",
    "MESMA", "OUTRA", "TITULARIDADE", "INSTITUICAO", "AGENDADO", "AGENDADA", "ENVIO", "RECEBIMENTO", "COMPRA",
    "CARTAO", "DEB", "AUTOMATICO", "SAQUE", "DEPOSITO", "TARIFA", "OUTROS", "DIVERSOS", "TRANSACAO", "OPERACAO",
}


def is_generic(desc_norm: str | None) -> bool:
    tokens = (desc_norm or "").split()
    return not tokens or all(t in GENERIC_WORDS for t in tokens)


CITIES = [
    "SAO PAULO", "RIO DE JANEIRO", "BELO HORIZONTE", "CURITIBA", "PORTO ALEGRE",
    "BRASILIA", "SALVADOR", "RECIFE", "FORTALEZA", "CAMPINAS", "OSASCO", "BARUERI",
    "NITEROI", "SAO GONCALO", "NOVA IGUACU", "DUQUE DE CAXIAS", "BELFORD ROXO",
    "SAO JOAO DE MERITI", "NILOPOLIS", "MESQUITA", "QUEIMADOS", "PETROPOLIS",
    "SANTO ANDRE", "SAO BERNARDO", "GUARULHOS", "FLORIANOPOLIS", "GOIANIA", "MANAUS",
    "BELEM", "VITORIA", "JOINVILLE", "RIBEIRAO PRETO", "SOROCABA", "SANTOS",
]
UFS = {
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA",
    "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO", "BR", "BRA",
}
NOISE_TOKENS = {"PARC", "PARCELA", "PARCELADO", "LTDA", "EIRELI", "ME", "EPP", "SA", "S/A", "CIA", "COM"}


def fold(text: str) -> str:
    """Upper-case, accents stripped."""
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch)).upper().strip()


def _letters_of_id(token: str) -> str:
    letters = re.sub(r"[^A-Z]", "", token)
    return f" {letters} " if len(letters) >= 3 else " "


def normalize_description(text: str, extra_cities: list[str] | None = None) -> str:
    s = fold(text)
    if not s:
        return ""
    # Acquirer prefix: "IFD*IFOOD" -> "IFOOD"; "UBER *TRIP" -> "UBER TRIP".
    if "*" in s:
        head, _, tail = s.partition("*")
        if head.strip().replace(" ", "") in ACQUIRER_PREFIXES and tail.strip():
            s = tail
        s = s.replace("*", " ")
    s = re.sub(r"https?://\S+|www\.\S+", " ", s)
    # tokens with digits: ids, dates and parcelas go away; a name glued to an id keeps its letters
    # ("31952531MARCOS" -> "MARCOS", "P3ESPACOLASERRJ" -> "PESPACOLASERRJ", "99APP" -> "APP")
    s = re.sub(r"\S*\d\S*", lambda m: _letters_of_id(m.group(0)), s)
    s = re.sub(r"[^A-Z&' ]+", " ", s)          # punctuation
    s = re.sub(r"\s+", " ", s).strip()
    for prefix in GENERIC_PREFIXES:
        if s == prefix:
            break
        if s.startswith(prefix + " "):
            s = s[len(prefix) + 1 :]
            break
    for city in sorted(CITIES + [fold(c) for c in (extra_cities or [])], key=len, reverse=True):
        s = re.sub(rf"(?:^| ){re.escape(city)}(?= |$)", " ", s)
    tokens = [t for t in s.split() if t not in NOISE_TOKENS]
    while len(tokens) > 1 and tokens[-1] in UFS:
        tokens.pop()
    return " ".join(tokens).strip()


def digits(text: str | None) -> str:
    return "".join(ch for ch in (text or "") if ch.isdigit())
