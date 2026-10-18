"""Config (config.yaml) and taxonomy (taxonomy.yaml) loading."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    pass


class VaultUnmounted(RuntimeError):
    pass


OWNERS = ("pf", "pj")  # titular da conta: pessoa física ou a sua PJ (MEI)


@dataclass(frozen=True)
class Category:
    name: str
    natureza: str
    necessidade: str
    llm: bool


@dataclass(frozen=True)
class Taxonomy:
    naturezas: list[str]
    categories: dict[str, Category]
    necessidades: list[str]
    essencial: list[str]
    fontes: list[str]

    REVIEW = "Revisar"

    @property
    def names(self) -> list[str]:
        return list(self.categories)

    @property
    def llm_categories(self) -> list[str]:
        cats = [c.name for c in self.categories.values() if c.llm]
        if self.REVIEW not in cats:
            cats.append(self.REVIEW)
        return cats

    def natureza_of(self, categoria: str) -> str:
        cat = self.categories.get(categoria)
        return cat.natureza if cat else "Despesa"

    def necessidade_of(self, categoria: str) -> str:
        cat = self.categories.get(categoria)
        return cat.necessidade if cat else "variavel"

    def canonical(self, name: str | None) -> str | None:
        """Case/accent-insensitive lookup of a category name."""
        if not name:
            return None
        from fin.normalize import fold

        wanted = fold(name)
        for c in self.categories:
            if fold(c) == wanted:
                return c
        return None


@dataclass(frozen=True)
class CardConfig:
    issuer_cnpj: str = ""
    closing_day: int = 0
    due_day: int = 0


@dataclass(frozen=True)
class FixedCost:
    nome: str
    valor_cents: int
    dia: int
    padrao: str
    de: str | None = None  # AAAA-MM: first month it is due (installments that start later)
    ate: str | None = None  # AAAA-MM: last month it is due
    antecedencia: int = 10  # how many days before the due date a payment still counts for that month


@dataclass
class Config:
    data_dir: Path
    require_mount: Path | None
    items: list[dict[str, str]]
    window_days: int = 45
    missing_syncs_to_remove: int = 3
    stale_hours: int = 48
    cards: dict[str, CardConfig] = field(default_factory=dict)
    extra_own_accounts: list[dict[str, str]] = field(default_factory=list)
    spreadsheet_id: str = ""
    salary_day: int = 5
    expected_income_cents: int = 0
    income_sources: list[dict[str, str]] = field(default_factory=list)
    fixed_costs: list[FixedCost] = field(default_factory=list)
    caju_file: str = "caju.csv"
    block_terms: list[str] = field(default_factory=list)
    llm_enabled: bool = True
    llm_chunk_size: int = 40
    llm_min_confidence: float = 0.6
    llm_workspace_id: str = ""
    telegram_enabled: bool = True
    source_path: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ledger.db"

    def owner_of_item(self, item_id: str) -> str:
        return next((i["owner"] for i in self.items if i.get("item_id") == item_id), "pf")

    @property
    def has_pj(self) -> bool:
        return any(i["owner"] == "pj" for i in self.items)

    def card(self, bank: str) -> CardConfig:
        return self.cards.get(bank, CardConfig())

    def ensure_data_dir(self) -> Path:
        """Refuse to run (and never create dirs) when the encrypted vault is not mounted."""
        if self.require_mount is not None and not os.path.ismount(self.require_mount):
            raise VaultUnmounted(
                f"cofre desmontado: {self.require_mount} não está montado "
                "(rode: hdiutil attach -nobrowse ~/Vaults/FinVault.sparsebundle)"
            )
        if self.require_mount is not None and not self.data_dir.resolve().is_relative_to(Path(self.require_mount).resolve()):
            raise VaultUnmounted(
                f"data_dir {self.data_dir} fica fora do cofre {self.require_mount}: nada é gravado "
                "(confira data_dir no config.yaml e a variável FIN_DATA_DIR)"
            )
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self.data_dir


def _cents(value: Any) -> int:
    from fin.money import to_cents

    return to_cents(value or 0)


def _item(raw: dict[str, Any]) -> dict[str, str]:
    item = {k: str(v) for k, v in raw.items()}
    owner = item.get("owner", "pf").strip().lower() or "pf"
    if owner not in OWNERS:
        raise ConfigError(f"config: owner inválido no item {item.get('bank')}: {owner} (use pf ou pj)")
    item["owner"] = owner
    return item


def config_from_dict(raw: dict[str, Any], source: Path | None = None) -> Config:
    pluggy = raw.get("pluggy") or {}
    income = raw.get("income") or {}
    llm = raw.get("llm") or {}
    env_dir = os.environ.get("FIN_DATA_DIR")
    data_dir = Path(env_dir or raw.get("data_dir") or "").expanduser()
    if not str(data_dir) or str(data_dir) == ".":
        raise ConfigError("config: data_dir não definido")
    mount = raw.get("require_mount")
    cards = {
        bank: CardConfig(
            issuer_cnpj="".join(ch for ch in str(c.get("issuer_cnpj") or "") if ch.isdigit()),
            closing_day=int(c.get("closing_day") or 0),
            due_day=int(c.get("due_day") or 0),
        )
        for bank, c in (raw.get("cards") or {}).items()
    }
    fixed = [
        FixedCost(
            nome=str(f["nome"]),
            valor_cents=_cents(f.get("valor")),
            dia=int(f.get("dia") or 1),
            padrao=str(f.get("padrao") or f["nome"]).upper(),
            de=str(f["de"])[:7] if f.get("de") else None,
            ate=str(f["ate"])[:7] if f.get("ate") else None,
            antecedencia=int(f.get("antecedencia") or 10),
        )
        for f in (raw.get("fixed_costs") or [])
    ]
    return Config(
        data_dir=data_dir,
        require_mount=Path(mount) if mount else None,
        items=[_item(i) for i in (pluggy.get("items") or [])],
        window_days=int(pluggy.get("window_days", 45)),
        missing_syncs_to_remove=int(pluggy.get("missing_syncs_to_remove", 3)),
        stale_hours=int(pluggy.get("stale_hours", 48)),
        cards=cards,
        extra_own_accounts=[dict(a) for a in (raw.get("extra_own_accounts") or [])],
        spreadsheet_id=str((raw.get("sheets") or {}).get("spreadsheet_id") or ""),
        salary_day=int(income.get("salary_day", 5)),
        expected_income_cents=_cents(income.get("expected_monthly")),
        income_sources=[dict(s) for s in (income.get("sources") or [])],
        fixed_costs=fixed,
        caju_file=str((raw.get("caju") or {}).get("file") or "caju.csv"),
        block_terms=[str(t).lower() for t in ((raw.get("privacy") or {}).get("block_terms") or [])],
        llm_enabled=bool(llm.get("enabled", True)),
        llm_workspace_id=str(llm.get("workspace_id") or ""),
        llm_chunk_size=int(llm.get("chunk_size", 40)),
        llm_min_confidence=float(llm.get("min_confidence", 0.6)),
        telegram_enabled=bool((raw.get("telegram") or {}).get("enabled", True)),
        source_path=source,
    )


def config_path() -> Path:
    env = os.environ.get("FIN_CONFIG")
    if env:
        return Path(env).expanduser()
    cwd = Path.cwd() / "config.yaml"
    return cwd if cwd.exists() else PROJECT_ROOT / "config.yaml"


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        raise ConfigError(
            f"config não encontrado em {path}. Copie config.example.yaml para config.yaml e preencha."
        )
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return config_from_dict(raw, path)


def taxonomy_from_dict(raw: dict[str, Any]) -> Taxonomy:
    cats: dict[str, Category] = {}
    for name, spec in (raw.get("categorias") or {}).items():
        spec = spec or {}
        cats[name] = Category(
            name=name,
            natureza=spec.get("natureza", "Despesa"),
            necessidade=spec.get("necessidade", "variavel"),
            llm=bool(spec.get("llm", False)),
        )
    if Taxonomy.REVIEW not in cats:
        cats[Taxonomy.REVIEW] = Category(Taxonomy.REVIEW, "Despesa", "variavel", True)
    return Taxonomy(
        naturezas=list(raw.get("naturezas") or []),
        categories=cats,
        necessidades=list(raw.get("necessidades") or ["fixo", "variavel"]),
        essencial=list(raw.get("essencial") or ["Essencial", "Supérfluo"]),
        fontes=list(raw.get("fontes") or []),
    )


def load_taxonomy(path: Path | None = None) -> Taxonomy:
    path = path or Path(os.environ.get("FIN_TAXONOMY", "") or PROJECT_ROOT / "taxonomy.yaml")
    with Path(path).open(encoding="utf-8") as f:
        return taxonomy_from_dict(yaml.safe_load(f) or {})
