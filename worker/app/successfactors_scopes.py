"""Reviewed execution identities; live Fantastic/RMK still supply job discovery."""
import json
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from app.adapters.successfactors import RMKError, bounded_locale_views, classify_platform, public_url


CONFIG_PATH = Path(__file__).parent / "config" / "successfactors_scopes.json"
Locale = Annotated[str, Field(pattern=r"^[a-z]{2}_[A-Z]{2}$")]


class ScopeIdentity(BaseModel):
    model_config = {"extra": "forbid"}
    host: str = Field(min_length=1, max_length=253, pattern=r"^[a-z0-9.-]+$")
    brand: str = Field(max_length=200, pattern=r"^[A-Za-z0-9_/-]*$")

    @field_validator("host")
    @classmethod
    def public_host(cls, value):
        url = public_url("https://" + value + "/")
        if urlsplit(url).hostname != value or classify_platform(url) == "migrated_sap":
            raise ValueError("Scope must identify a public RMK host")
        return value

    @field_validator("brand")
    @classmethod
    def normalized_brand(cls, value):
        if value != value.strip("/") or "//" in value:
            raise ValueError("Scope brand must use normalized path components")
        return value


class ValidatedScope(ScopeIdentity):
    native_tenant: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")
    native_search_brand: str = Field(max_length=200, pattern=r"^[A-Za-z0-9_/-]*$")
    preferred_locale: Locale
    locale_union: list[Locale] | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def reviewed_locale_policy(self):
        if self.native_search_brand != self.native_search_brand.strip("/") or "//" in self.native_search_brand:
            raise ValueError("Native search brand must be normalized")
        if self.locale_union is not None:
            if self.brand == self.native_search_brand or self.preferred_locale not in self.locale_union:
                raise ValueError("Locale union requires a reviewed brand-homepage recall case")
            if len(set(self.locale_union)) != len(self.locale_union):
                raise ValueError("Duplicate approved locale")
        return self


class ExcludedScope(ScopeIdentity):
    reason: str = Field(min_length=1, max_length=500)


class ScopeGate(BaseModel):
    model_config = {"extra": "forbid"}
    version: Literal[1]
    scopes: list[ValidatedScope] = Field(min_length=1, max_length=500)
    excluded_scopes: list[ExcludedScope]

    @model_validator(mode="after")
    def unique_disjoint_scopes(self):
        keys = [(s.host, s.brand) for s in self.scopes]
        native = [(s.native_tenant, s.native_search_brand) for s in self.scopes]
        excluded = [(s.host, s.brand) for s in self.excluded_scopes]
        if len(set(keys)) != len(keys) or len(set(native)) != len(native) or len(set(excluded)) != len(excluded):
            raise ValueError("Duplicate reviewed scope identity")
        if set(keys) & set(excluded):
            raise ValueError("Excluded scope cannot enter the validated allowlist")
        return self

    def select(self, discovered):
        available = {(s.host, s.brand): s for s in discovered}
        if any((s.host, s.brand) not in available for s in self.scopes):
            raise ValueError("Reviewed RMK scope is absent from current Fantastic discovery")
        return [available[(s.host, s.brand)] for s in self.scopes]

    def diagnostics(self, discovered):
        approved = {(s.host, s.brand) for s in self.scopes}
        excluded = {(s.host, s.brand) for s in self.excluded_scopes}
        return {"allowlist_sha256": sha256(self.model_dump_json().encode()).hexdigest(),
                "approved_scopes": len(self.scopes),
                "excluded_scopes": [s.model_dump() for s in self.excluded_scopes],
                "unapproved_discovered_scopes": [{"host": s.host, "brand": s.brand} for s in discovered
                                                if (s.host, s.brand) not in approved | excluded]}


def load_scope_gate(path=None):
    try:
        return ScopeGate.model_validate(json.loads(Path(path or CONFIG_PATH).read_text()))
    except (OSError, ValueError) as exc:
        raise ValueError("Invalid repository RMK validated-scope configuration") from exc


def apply_scope_gate(config, rule):
    """Fail closed on identity drift; bound public locale recall explicitly."""
    if (config.tenant != rule.native_tenant or config.brand != rule.native_search_brand
            or urlsplit(config.search_url).hostname != rule.host or config.hosts != {rule.host}):
        raise RMKError("config_regression", "Public RMK configuration differs from its reviewed scope")
    if rule.locale_union is None:
        config.locale = rule.preferred_locale
        config.locales = bounded_locale_views(rule.preferred_locale, config.advertised_locales)
        config.bounded_recall = True
    else:
        if not config.locale_home_used or not set(rule.locale_union).issubset(config.locales):
            raise RMKError("config_regression", "Public brand-homepage recall locales changed")
        config.locales = [rule.preferred_locale] + [v for v in rule.locale_union if v != rule.preferred_locale]
    return config
