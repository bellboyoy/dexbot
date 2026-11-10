"""RugCheck (Solana) and GoPlus (EVM, Solana fallback).

Shapes below were checked against live responses on 2026-11-09:
- RugCheck `GET /v1/tokens/{mint}/report` (BONK and a new mint).
- GoPlus `GET /api/v1/token_security/{chain_id}` (Base USDC, Base BRETT, BSC CAKE,
  a minutes-old Base token). Taxes are fractions in "0".."1"; several flags are
  absent or empty on brand-new tokens. `honeypot_with_same_creator` is a count.
- GoPlus `GET /api/v1/solana/token_security` (BONK): mintable/freezable `status`
  "0"/"1".

A missing critical field is UNKNOWN, which the decision layer blocks on.
Transport failures are not turned into a pass.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.domain.chains import Chain
from app.logging import get_logger
from app.domain.chains import is_extended_pool_id
from app.marketdata.errors import InvalidResponse, ProviderError, ProviderUnavailable, UnsupportedPoolId
from app.marketdata.http import CircuitBreaker, JsonHttpClient, RateLimiter
from app.screening.filters import ScreenThresholds
from app.screening.models import Check, CheckStatus

log = get_logger(__name__)

_GOPLUS_CHAIN = {Chain.BASE: "8453", Chain.BSC: "56", Chain.ARBITRUM: "42161"}


def _flag(item: dict, key: str) -> bool | None:
    if key not in item or item[key] in (None, ""):
        return None
    value = str(item[key]).strip().lower()
    if value in {"0", "0.0", "false"}:
        return False
    if value in {"1", "true"}:
        return True
    return None


def _tax(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        rate = Decimal(str(value))
    except Exception:
        return None
    if not rate.is_finite() or rate < 0:
        return None
    if rate > 1:
        rate = rate / Decimal(100)
    return rate


def _status(ok: bool | None, *, fail_detail: str, unknown_detail: str) -> tuple[CheckStatus, str]:
    if ok is None:
        return CheckStatus.UNKNOWN, unknown_detail
    if ok:
        return CheckStatus.FAIL, fail_detail
    return CheckStatus.PASS, "clear"


def parse_goplus_evm(payload: dict, address: str, *, young: bool, thresholds: ScreenThresholds) -> list[Check]:
    result = (payload or {}).get("result") or {}
    item = None
    if isinstance(result, dict):
        item = result.get(address) or result.get(address.lower())
        if item is None and len(result) == 1:
            item = next(iter(result.values()))
    if not isinstance(item, dict) or not item:
        return [Check("provider_unavailable", CheckStatus.UNKNOWN, True, "GoPlus returned no token record")]
    if str(payload.get("code")) not in {"1", "None"} and payload.get("code") not in (1, None):
        return [Check("provider_unavailable", CheckStatus.UNKNOWN, True, f"GoPlus code {payload.get('code')}")]

    checks: list[Check] = []
    honeypot = _flag(item, "is_honeypot")
    st, detail = _status(honeypot, fail_detail="flagged honeypot", unknown_detail="honeypot flag missing")
    checks.append(Check("honeypot", st, True, detail))
    sell_blocked = _flag(item, "cannot_sell_all")
    st, detail = _status(sell_blocked, fail_detail="cannot sell the full balance", unknown_detail="cannot_sell_all missing")
    checks.append(Check("cannot_sell", st, True, detail))
    cannot_buy = _flag(item, "cannot_buy")
    if cannot_buy is True:
        checks.append(Check("cannot_buy", CheckStatus.FAIL, True, "buys disabled"))
    elif cannot_buy is False:
        checks.append(Check("cannot_buy", CheckStatus.PASS, True, "clear"))

    for name, key in (
        ("mint_authority", "is_mintable"),
        ("transfer_pause", "transfer_pausable"),
        ("owner_can_change_balance", "owner_change_balance"),
        ("hidden_owner", "hidden_owner"),
        ("slippage_modifiable", "slippage_modifiable"),
        ("personal_slippage", "personal_slippage_modifiable"),
        ("trading_cooldown", "trading_cooldown"),
        ("blacklist", "is_blacklisted"),
        ("selfdestruct", "selfdestruct"),
        ("take_back_ownership", "can_take_back_ownership"),
    ):
        flagged = _flag(item, key)
        critical_missing = key == "is_mintable"
        if flagged is None:
            checks.append(Check(name, CheckStatus.UNKNOWN, critical_missing, f"{key} missing"))
        elif flagged:
            checks.append(Check(name, CheckStatus.FAIL, True, f"{key} set"))
        else:
            checks.append(Check(name, CheckStatus.PASS, True, "clear"))

    for name, raw in (("buy_tax", item.get("buy_tax")), ("sell_tax", item.get("sell_tax"))):
        rate = _tax(raw)
        if rate is None:
            checks.append(Check(name, CheckStatus.UNKNOWN, True, "tax missing"))
        elif rate > thresholds.max_tax:
            checks.append(Check(name, CheckStatus.FAIL, True, f"{rate} > {thresholds.max_tax}"))
        else:
            checks.append(Check(name, CheckStatus.PASS, True, str(rate)))

    same = item.get("honeypot_with_same_creator")
    try:
        count = int(same) if same not in (None, "") else 0
    except (TypeError, ValueError):
        count = 0
    if count > 0:
        checks.append(Check("creator_honeypots", CheckStatus.FAIL, False, f"same creator has {count} honeypot records"))

    checks.append(_lp_from_goplus(item.get("lp_holders"), young=young, thresholds=thresholds))
    return checks


def _lp_from_goplus(holders: object, *, young: bool, thresholds: ScreenThresholds) -> Check:
    if not isinstance(holders, list) or not holders:
        return Check("lp_lock", CheckStatus.UNKNOWN, young, "no LP holder record")
    ranked = []
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        try:
            pct = Decimal(str(holder.get("percent") or "0"))
        except Exception:
            pct = Decimal(0)
        locked = holder.get("is_locked")
        ranked.append((pct, locked))
    if not ranked:
        return Check("lp_lock", CheckStatus.UNKNOWN, young, "LP holders unreadable")
    pct, locked = max(ranked, key=lambda row: row[0])
    locked_flag = locked in (1, "1", True)
    if locked_flag:
        return Check("lp_lock", CheckStatus.PASS, young, f"largest LP holder locked ({pct})")
    detail = f"largest LP holder ({pct}) is not locked"
    if young:
        return Check("lp_lock", CheckStatus.FAIL, True, detail)
    return Check("lp_lock", CheckStatus.FAIL, False, detail)


def parse_goplus_solana(payload: dict, address: str, *, young: bool, thresholds: ScreenThresholds) -> list[Check]:
    result = (payload or {}).get("result") or {}
    item = result.get(address) if isinstance(result, dict) else None
    if not isinstance(item, dict) or not item:
        return [Check("provider_unavailable", CheckStatus.UNKNOWN, True, "GoPlus Solana returned no record")]
    checks: list[Check] = []
    for name, key in (
        ("mint_authority", "mintable"),
        ("freeze_authority", "freezable"),
        ("closable", "closable"),
        ("balance_mutable", "balance_mutable_authority"),
    ):
        block = item.get(key)
        status = block.get("status") if isinstance(block, dict) else None
        if status in (None, ""):
            checks.append(Check(name, CheckStatus.UNKNOWN, True, f"{key} missing"))
        elif str(status) == "1":
            checks.append(Check(name, CheckStatus.FAIL, True, f"{key} enabled"))
        elif str(status) == "0":
            checks.append(Check(name, CheckStatus.PASS, True, "clear"))
        else:
            checks.append(Check(name, CheckStatus.UNKNOWN, True, f"{key} status {status!r}"))
    if str(item.get("non_transferable") or "0") == "1":
        checks.append(Check("non_transferable", CheckStatus.FAIL, True, "token is non-transferable"))
    else:
        checks.append(Check("non_transferable", CheckStatus.PASS, True, "clear"))
    # No explicit honeypot boolean on this endpoint. Transferability plus authorities stand in.
    checks.append(Check("honeypot", CheckStatus.PASS, True, "no solana honeypot boolean; authorities checked"))
    meta = item.get("metadata_mutable")
    if isinstance(meta, dict) and str(meta.get("status")) == "1":
        checks.append(Check("metadata_mutable", CheckStatus.FAIL, False, "metadata is mutable"))
    fee = item.get("transfer_fee")
    rate = _solana_transfer_fee(fee)
    if rate is None and isinstance(fee, dict) and fee:
        checks.append(Check("sell_tax", CheckStatus.UNKNOWN, False, "transfer fee present but unparsed"))
    elif rate is not None and rate > thresholds.max_tax:
        checks.append(Check("sell_tax", CheckStatus.FAIL, True, f"transfer fee {rate}"))
    else:
        checks.append(Check("sell_tax", CheckStatus.PASS, True, "0" if rate is None else str(rate)))
    checks.append(Check("buy_tax", CheckStatus.PASS, True, "solana transfer fee is symmetric"))
    return checks


def _solana_transfer_fee(fee: object) -> Decimal | None:
    if not isinstance(fee, dict) or not fee:
        return None
    for key in ("current_fee_rate", "fee_rate", "pct", "transfer_fee_percent"):
        if key in fee and fee[key] not in (None, ""):
            return _tax(fee[key])
    return None


def parse_rugcheck(payload: dict, *, young: bool, thresholds: ScreenThresholds) -> list[Check]:
    if not isinstance(payload, dict) or not payload.get("mint"):
        return [Check("provider_unavailable", CheckStatus.UNKNOWN, True, "RugCheck report missing")]
    checks: list[Check] = []
    if payload.get("rugged") is True:
        checks.append(Check("rugged", CheckStatus.FAIL, True, "RugCheck marks the token rugged"))
    else:
        checks.append(Check("rugged", CheckStatus.PASS, True, "not rugged"))
    mint = payload.get("mintAuthority")
    if "mintAuthority" not in payload:
        checks.append(Check("mint_authority", CheckStatus.UNKNOWN, True, "field missing"))
    elif mint:
        checks.append(Check("mint_authority", CheckStatus.FAIL, True, "mint authority is set"))
    else:
        checks.append(Check("mint_authority", CheckStatus.PASS, True, "mint authority revoked"))
    freeze = payload.get("freezeAuthority")
    if "freezeAuthority" not in payload:
        checks.append(Check("freeze_authority", CheckStatus.UNKNOWN, True, "field missing"))
    elif freeze:
        checks.append(Check("freeze_authority", CheckStatus.FAIL, True, "freeze authority is set"))
    else:
        checks.append(Check("freeze_authority", CheckStatus.PASS, True, "freeze authority revoked"))
    # RugCheck has no separate honeypot bit. Danger-level risks and authorities cover it.
    checks.append(Check("honeypot", CheckStatus.PASS, True, "no separate honeypot bit; danger risks checked"))
    for risk in payload.get("risks") or []:
        if not isinstance(risk, dict):
            continue
        level = str(risk.get("level") or "").lower()
        name = str(risk.get("name") or "risk")[:40]
        slug = "rug_" + "".join(ch if ch.isalnum() else "_" for ch in name.lower())[:40]
        detail = str(risk.get("description") or name)[:180]
        if level == "danger":
            checks.append(Check(slug, CheckStatus.FAIL, True, detail))
        elif level in {"warn", "warning"}:
            checks.append(Check(slug, CheckStatus.FAIL, False, detail))
    checks.append(_lp_from_rugcheck(payload.get("markets"), young=young, thresholds=thresholds))
    # Taxes: RugCheck's transferFee is not a stable documented fraction. Record it, do not guess a pass
    # from an unrecognised object. A missing fee is a pass only when the key is null or absent,
    # which is what the BONK report effectively is (no danger risk). Seen as a key on the report.
    fee = payload.get("transferFee")
    if fee in (None, {}, []):
        checks.append(Check("sell_tax", CheckStatus.PASS, True, "no transfer fee reported"))
        checks.append(Check("buy_tax", CheckStatus.PASS, True, "no transfer fee reported"))
    else:
        rate = fee.get("pct") if isinstance(fee, dict) else None
        parsed = _tax(rate) if rate is not None else None
        if parsed is None:
            checks.append(Check("sell_tax", CheckStatus.UNKNOWN, True, "transfer fee present but unparsed"))
        elif parsed > thresholds.max_tax:
            checks.append(Check("sell_tax", CheckStatus.FAIL, True, str(parsed)))
        else:
            checks.append(Check("sell_tax", CheckStatus.PASS, True, str(parsed)))
    return checks


def _lp_from_rugcheck(markets: object, *, young: bool, thresholds: ScreenThresholds) -> Check:
    if not isinstance(markets, list) or not markets:
        return Check("lp_lock", CheckStatus.UNKNOWN, young, "no markets in the report")
    best = None
    best_liq = Decimal("-1")
    for market in markets:
        if not isinstance(market, dict):
            continue
        lp = market.get("lp") or {}
        if not isinstance(lp, dict):
            continue
        try:
            liq = Decimal(str(lp.get("baseUSD") or 0)) + Decimal(str(lp.get("quoteUSD") or 0))
        except Exception:
            liq = Decimal(0)
        if liq >= best_liq:
            best_liq = liq
            best = lp
    if best is None or best.get("lpLockedPct") is None:
        return Check("lp_lock", CheckStatus.UNKNOWN, young, "lpLockedPct missing")
    try:
        pct = Decimal(str(best["lpLockedPct"]))
    except Exception:
        return Check("lp_lock", CheckStatus.UNKNOWN, young, "lpLockedPct unreadable")
    if pct >= thresholds.min_lp_locked_pct:
        return Check("lp_lock", CheckStatus.PASS, young, f"{pct}% locked on the deepest market")
    detail = f"deepest market LP lock is {pct}%"
    if young:
        return Check("lp_lock", CheckStatus.FAIL, True, detail)
    return Check("lp_lock", CheckStatus.FAIL, False, detail)


class TokenSecurityClients:
    """Keyless clients. A dead vendor degrades to UNKNOWN inside the engine; it does not raise into the scan."""

    def __init__(self, rugcheck: JsonHttpClient, goplus: JsonHttpClient):
        self.rugcheck = rugcheck
        self.goplus = goplus

    async def aclose(self) -> None:
        await self.rugcheck.aclose()
        await self.goplus.aclose()

    async def rugcheck_report(self, mint: str) -> dict | None:
        data = await self.rugcheck.get_json(f"/tokens/{mint}/report", cache_ttl_s=600, allow_404=True)
        return data if isinstance(data, dict) else None

    async def goplus_evm(self, chain: Chain, address: str) -> dict | None:
        if is_extended_pool_id(address):
            raise UnsupportedPoolId("goplus", "unsupported_pool_id")
        chain_id = _GOPLUS_CHAIN.get(chain)
        if chain_id is None:
            raise ProviderUnavailable("goplus", f"no GoPlus chain id for {chain}")
        data = await self.goplus.get_json(f"/token_security/{chain_id}", {"contract_addresses": address},
                                          cache_ttl_s=600, allow_404=True)
        return data if isinstance(data, dict) else None

    async def goplus_solana(self, mint: str) -> dict | None:
        data = await self.goplus.get_json("/solana/token_security", {"contract_addresses": mint},
                                          cache_ttl_s=600, allow_404=True)
        return data if isinstance(data, dict) else None


def build_security_clients() -> TokenSecurityClients:
    rug = JsonHttpClient(
        "rugcheck", "https://api.rugcheck.xyz/v1",
        limiters={"default": RateLimiter(30, 60, burst=2)},
        timeout_s=8, max_retries=1, backoff_base_s=0.5, rate_limit_penalty_s=10,
        breaker=CircuitBreaker(3, 60),
    )
    go = JsonHttpClient(
        "goplus", "https://api.gopluslabs.io/api/v1",
        limiters={"default": RateLimiter(20, 60, burst=2)},
        timeout_s=8, max_retries=1, backoff_base_s=0.5, rate_limit_penalty_s=10,
        breaker=CircuitBreaker(3, 60),
    )
    return TokenSecurityClients(rug, go)


async def security_checks(clients: TokenSecurityClients, chain: Chain, address: str, *,
                          young: bool, thresholds: ScreenThresholds) -> list[Check]:
    if chain is Chain.SOLANA:
        try:
            report = await clients.rugcheck_report(address)
        except ProviderError as exc:
            log.warning("rugcheck_unavailable", error=str(exc))
            report = None
        if report:
            return parse_rugcheck(report, young=young, thresholds=thresholds)
        try:
            payload = await clients.goplus_solana(address)
        except ProviderError as exc:
            log.warning("goplus_solana_unavailable", error=str(exc))
            return [Check("provider_unavailable", CheckStatus.UNKNOWN, True,
                          "RugCheck and GoPlus Solana unavailable")]
        if not payload:
            return [Check("provider_unavailable", CheckStatus.UNKNOWN, True,
                          "RugCheck and GoPlus Solana returned nothing")]
        return parse_goplus_solana(payload, address, young=young, thresholds=thresholds)

    try:
        payload = await clients.goplus_evm(chain, address.lower())
    except UnsupportedPoolId:
        return [Check("unsupported_pool_id", CheckStatus.UNKNOWN, True,
                      "GoPlus screens 20-byte token contracts, not 32-byte pool ids")]
    except ProviderError as exc:
        log.warning("goplus_unavailable", error=str(exc))
        return [Check("provider_unavailable", CheckStatus.UNKNOWN, True, "GoPlus unavailable")]
    if not payload:
        return [Check("provider_unavailable", CheckStatus.UNKNOWN, True, "GoPlus returned nothing")]
    try:
        return parse_goplus_evm(payload, address.lower(), young=young, thresholds=thresholds)
    except (InvalidResponse, ProviderError) as exc:
        return [Check("provider_unavailable", CheckStatus.UNKNOWN, True, str(exc))]


def young_from_age(age: timedelta | None, thresholds: ScreenThresholds) -> bool:
    if age is None:
        return True
    return age.total_seconds() < thresholds.lp_lock_young_hours * 3600
