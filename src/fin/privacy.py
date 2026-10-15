"""Privacy guard for anything that leaves the machine towards the LLM.

Only (opaque id, normalized merchant description, debit/credit) may be sent.
This module decides, item by item, whether a description is safe to send.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

BLOCK_PATTERNS = [
    ("digits", re.compile(r"\d{8,}")),                       # CPF/CNPJ/account numbers
    ("digits_sep", re.compile(r"\d{3}[.\-/ ]?\d{3}[.\-/ ]?\d{3}")),  # formatted CPF fragments
    ("at", re.compile(r"@")),                                 # e-mail / Pix key
    ("uuid", re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)),
    ("phone", re.compile(r"\+?55\s?\(?\d{2}\)?\s?9?\d{4}-?\d{4}")),
    # CPF/CNPJ with OCR-like letters (O/o for 0, l/I for 1): "1l1.111.1l1-11"
    ("doc_ocr", re.compile(r"[\dOolI]{3}[.\-][\dOolI]{3}[.\-][\dOolI]{3}")),
    ("hex_key", re.compile(r"\b[0-9a-f]{32}\b", re.I)),          # random Pix key without hyphens
]

# Common Brazilian first names: a hit means "might be a person", so it stays local.
# False positives (e.g. "PADARIA SAO JOSE") only cost a manual review.
COMMON_FIRST_NAMES = {
    "ana", "maria", "jose", "joao", "antonio", "francisco", "carlos", "paulo", "pedro",
    "lucas", "luiz", "luis", "marcos", "gabriel", "rafael", "daniel", "marcelo", "bruno",
    "eduardo", "felipe", "rodrigo", "manoel", "manuel", "mateus", "matheus", "andre",
    "fernando", "fabio", "leonardo", "gustavo", "guilherme", "leandro", "tiago", "thiago",
    "ricardo", "sergio", "claudio", "vitor", "victor", "diego", "julio", "jorge", "roberto",
    "adriana", "juliana", "marcia", "fernanda", "patricia", "aline", "sandra", "camila",
    "amanda", "bruna", "jessica", "leticia", "julia", "luciana", "vanessa", "mariana",
    "gabriela", "vera", "vitoria", "larissa", "claudia", "beatriz", "luana", "rita",
    "sonia", "renata", "eliane", "josefa", "simone", "natalia", "cristiane", "carla",
    "debora", "rosangela", "jaqueline", "rosa", "daniela", "aparecida", "marlene",
    "terezinha", "raimunda", "andreia", "fabiana", "lucia", "raquel", "angela", "rafaela",
    "joana", "luzia", "elaine", "daiane", "regina", "carolina", "priscila", "isabel",
    "caio", "igor", "renan", "vinicius", "henrique",
    "alexandre", "rogerio", "wellington", "edson", "flavio", "murilo", "otavio", "samuel",
}


# Most common Brazilian surnames and forms of address: same idea as first names.
COMMON_SURNAMES = {
    "silva", "santos", "oliveira", "souza", "sousa", "rodrigues", "ferreira", "alves", "pereira", "lima",
    "gomes", "costa", "ribeiro", "martins", "carvalho", "almeida", "lopes", "soares", "fernandes", "vieira",
    "barbosa", "rocha", "dias", "nascimento", "andrade", "moreira", "nunes", "marques", "machado", "mendes",
    "freitas", "cardoso", "ramos", "goncalves", "santana", "teixeira", "araujo", "correia", "cavalcanti",
    "monteiro", "moura", "batista", "pinto", "reis", "campos", "macedo", "azevedo", "duarte", "fonseca",
}
ADDRESS_WORDS = {"dona", "seu", "sr", "sra", "srta", "dr", "dra"}
EXTRA_FIRST_NAMES = {"kaua", "wesley", "wellington", "everton", "cleber", "gilberto", "zeze", "tereza", "teresa",
                     "conceicao", "fatima", "francisca", "antonia", "adriano", "alex", "alan", "allan", "erick",
                     "eric", "kevin", "ryan", "arthur", "heitor", "davi", "miguel", "enzo", "sophia", "alice",
                     "laura", "helena", "valentina", "manuela", "isadora", "yasmin", "thais", "tais", "kelly",
                     "michele", "michelle", "luan", "nathan", "wagner", "willian", "william", "jefferson",
                     "ester", "esther", "gleyce", "gleice", "mayara", "hamilton", "cesar", "nelson", "adilson",
                     "cleide", "cleidiane", "edilson", "nilson", "wilson", "gilson", "jair", "jairo", "valdir",
                     "sueli", "neide", "solange", "rosana", "silvana", "tatiana", "monica", "denise", "viviane"}
COMMON_SURNAMES |= {"miranda", "meneses", "menezes", "barros", "figueiredo", "estevao", "vianna", "viana",
                    "moraes", "morais", "domingos", "darly", "pinheiro", "medeiros", "farias", "castro"}
# Card acquirers that print the seller's own name (often a person): never sent to the AI;
# the local dictionary learns them after one manual review.
PERSON_ACQUIRER_PREFIX = re.compile(r"^(?:MP|JIM|PG|PAG|SUMUP|SUM|TON|PICPAY|IZ|EC)\s+\S", re.I)
LINKER_TRUNC = re.compile(r"\b(?:DA|DE|DO|DOS|DAS)\s+([A-Z]{3,6})\s*$", re.I)
PERSON_TOKENS = COMMON_FIRST_NAMES | COMMON_SURNAMES | ADDRESS_WORDS | EXTRA_FIRST_NAMES
GLUE_NAMES = {n for n in COMMON_FIRST_NAMES | EXTRA_FIRST_NAMES if len(n) >= 4}
GLUE_SURNAMES = {n for n in COMMON_SURNAMES if len(n) >= 5}


class PrivacyViolation(RuntimeError):
    """Raised when a payload that should have been filtered reaches the LLM client."""


def block_reason(text: str, extra_terms: Iterable[str] = ()) -> str | None:
    """Return why `text` must not be sent, or None if it is safe."""
    if not text or not text.strip():
        return "empty"
    for name, pattern in BLOCK_PATTERNS:
        if pattern.search(text):
            return name
    tokens = set(re.findall(r"[a-z]+", _ascii(text)))
    for term in extra_terms:
        # each word of your terms blocks on its own ("joao souza" blocks "SOUZA" alone), accents ignored
        words = [w for w in re.findall(r"[a-z]+", _ascii(term)) if len(w) >= 3]
        if any(w in tokens for w in words) or (_ascii(term).strip() and _ascii(term).strip() in _ascii(text)):
            return "term"
    if tokens & PERSON_TOKENS:
        return "first_name"
    if PERSON_ACQUIRER_PREFIX.search(text.strip()):
        return "acquirer_person"
    m = LINKER_TRUNC.search(_ascii(text).upper())
    if m and any(sn.startswith(m.group(1).lower()) for sn in COMMON_SURNAMES):
        return "truncated_surname"
    # names glued together by card acquirers: "FERNANDOLUIZV", "ANDRESSASILVADE"
    for tok in tokens:
        if len(tok) >= 8 and any(tok.startswith(n) and len(tok) - len(n) >= 3 for n in GLUE_NAMES):
            return "glued_name"
        if len(tok) >= 8 and any(tok.endswith(n) and len(tok) - len(n) >= 3 for n in GLUE_SURNAMES):
            return "glued_name"
    return None


def _ascii(text: str) -> str:
    from fin.normalize import fold

    return fold(text).lower()


def assert_safe(items: list[dict], extra_terms: Iterable[str] = ()) -> None:
    allowed = {"id", "desc", "tipo"}
    for it in items:
        if set(it) - allowed:
            raise PrivacyViolation(f"campo não permitido no payload: {sorted(set(it) - allowed)}")
        if block_reason(it["desc"], extra_terms):
            raise PrivacyViolation(f"item {it['id']} bloqueado antes do envio")


def doc_hash(doc_digits: str, salt: str) -> str:
    """Keyed hash of a CPF/CNPJ: lets us compare documents without storing them."""
    return hashlib.sha256(f"{salt}|{doc_digits}".encode()).hexdigest()[:32]
