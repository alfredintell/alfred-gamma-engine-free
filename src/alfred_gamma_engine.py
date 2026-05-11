#!/usr/bin/env python3
"""
Alfred Gamma Engine — Free Build
Generates option-derived proxy gamma levels for:
- NQ/MNQ from QQQ options scaled to NQ futures
- ES/MES from SPY options scaled to ES futures

This is a free workaround. It is not MenthorQ and not CME futures-option-chain exact.
It uses Yahoo Finance ETF options chains, then scales ETF strikes to futures prices.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yfinance as yf


OUT_DIR = Path("out")
OUT_DIR.mkdir(exist_ok=True)


@dataclass
class GammaLevelSet:
    symbol: str
    futures_symbol: str
    proxy_symbol: str
    source: str
    generated_at_utc: str
    futures_price: float
    proxy_price: float
    scale_ratio: float
    call_resistance: float
    put_support: float
    hvl_gamma_flip: float
    zero_dte_call_wall: float
    zero_dte_put_wall: float
    zero_dte_gamma_wall: float
    expected_move_high: float
    expected_move_low: float
    gex_levels: List[float]
    note: str


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _last_close(symbol: str) -> Optional[float]:
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="5d", interval="1d", auto_adjust=False)
        if hist is None or hist.empty:
            return None
        val = float(hist["Close"].dropna().iloc[-1])
        if math.isfinite(val) and val > 0:
            return val
    except Exception as e:
        print(f"[WARN] Could not fetch last close for {symbol}: {e}")
    return None


def _chain_for_nearest_expiry(proxy_symbol: str) -> Tuple[Optional[str], Optional[pd.DataFrame], Optional[pd.DataFrame]]:
    try:
        t = yf.Ticker(proxy_symbol)
        expiries = list(t.options or [])
        if not expiries:
            print(f"[WARN] No options expiries found for {proxy_symbol}")
            return None, None, None

        expiry = expiries[0]
        chain = t.option_chain(expiry)
        calls = chain.calls.copy()
        puts = chain.puts.copy()

        if calls.empty or puts.empty:
            print(f"[WARN] Empty options chain for {proxy_symbol} {expiry}")
            return expiry, None, None

        return expiry, calls, puts
    except Exception as e:
        print(f"[WARN] Could not fetch option chain for {proxy_symbol}: {e}")
        return None, None, None


def _clean_chain(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in ["strike", "openInterest", "volume", "impliedVolatility", "lastPrice", "bid", "ask"]:
        if col not in out.columns:
            out[col] = np.nan
        out[col] = pd.to_numeric(out[col], errors="coerce")

    out["openInterest"] = out["openInterest"].fillna(0.0)
    out["volume"] = out["volume"].fillna(0.0)
    out["impliedVolatility"] = out["impliedVolatility"].replace([np.inf, -np.inf], np.nan).fillna(0.25)
    out["mid"] = ((out["bid"].fillna(0) + out["ask"].fillna(0)) / 2.0)
    out.loc[out["mid"] <= 0, "mid"] = out["lastPrice"]
    out["mid"] = out["mid"].fillna(0.0)
    out = out.dropna(subset=["strike"])
    out = out[out["strike"] > 0]
    return out


def _norm_pdf(x: np.ndarray | float) -> np.ndarray | float:
    return np.exp(-0.5 * np.asarray(x) ** 2) / math.sqrt(2.0 * math.pi)


def _simple_gamma(proxy_price: float, strike: pd.Series, iv: pd.Series, days_to_expiry: float) -> pd.Series:
    """
    Approximate Black-Scholes gamma for free proxy ETF options.
    For the free workaround, this is enough to rank strikes by exposure.
    """
    T = max(days_to_expiry / 365.0, 1.0 / 365.0)
    sigma = iv.clip(lower=0.03, upper=3.0)
    F = max(proxy_price, 0.01)
    K = strike.clip(lower=0.01)
    d1 = (np.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    gamma = _norm_pdf(d1) / (F * sigma * math.sqrt(T))
    return pd.Series(gamma, index=strike.index).replace([np.inf, -np.inf], np.nan).fillna(0.0)


def _round_to_tick(x: float, tick: float) -> float:
    if not math.isfinite(x) or x <= 0:
        return 0.0
    return round(x / tick) * tick


def _nearest_value(vals: List[float], fallback: float) -> float:
    vals = [float(v) for v in vals if math.isfinite(float(v)) and float(v) > 0]
    return vals[0] if vals else fallback


def _top_strikes(exposure_df: pd.DataFrame, n: int, current_price: float, scale_ratio: float, tick: float) -> List[float]:
    if exposure_df.empty:
        return []
    tmp = exposure_df.copy()
    tmp["abs_gex"] = tmp["net_gex"].abs()
    tmp = tmp.sort_values("abs_gex", ascending=False)
    levels = []
    for _, row in tmp.iterrows():
        lvl = _round_to_tick(float(row["strike"]) * scale_ratio, tick)
        if lvl > 0 and lvl not in levels:
            levels.append(lvl)
        if len(levels) >= n:
            break
    return levels


def _expected_move(proxy_price: float, calls: pd.DataFrame, puts: pd.DataFrame, scale_ratio: float, futures_price: float, tick: float) -> Tuple[float, float]:
    """
    ATM straddle proxy expected move.
    """
    if calls.empty or puts.empty:
        width = futures_price * 0.01
        return _round_to_tick(futures_price + width, tick), _round_to_tick(futures_price - width, tick)

    atm_strike = min(calls["strike"], key=lambda k: abs(float(k) - proxy_price))
    call_atm = calls.iloc[(calls["strike"] - atm_strike).abs().argsort()[:1]]
    put_atm = puts.iloc[(puts["strike"] - atm_strike).abs().argsort()[:1]]

    call_mid = float(call_atm["mid"].iloc[0]) if not call_atm.empty else 0.0
    put_mid = float(put_atm["mid"].iloc[0]) if not put_atm.empty else 0.0
    move_proxy = max(call_mid + put_mid, proxy_price * 0.006)
    move_fut = move_proxy * scale_ratio

    return _round_to_tick(futures_price + move_fut, tick), _round_to_tick(futures_price - move_fut, tick)


def build_levels(symbol: str, futures_symbol: str, proxy_symbol: str, tick: float) -> Optional[GammaLevelSet]:
    print(f"[INFO] Building {symbol}: {proxy_symbol} options -> {futures_symbol}")

    proxy_price = _last_close(proxy_symbol)
    futures_price = _last_close(futures_symbol)

    if proxy_price is None:
        print(f"[ERROR] Missing proxy price for {proxy_symbol}")
        return None
    if futures_price is None:
        print(f"[WARN] Missing futures price for {futures_symbol}; using scaled proxy fallback")
        futures_price = proxy_price

    scale_ratio = futures_price / proxy_price if proxy_price > 0 else 1.0

    expiry, raw_calls, raw_puts = _chain_for_nearest_expiry(proxy_symbol)
    if raw_calls is None or raw_puts is None:
        print(f"[ERROR] Missing options chain for {proxy_symbol}")
        return None

    calls = _clean_chain(raw_calls)
    puts = _clean_chain(raw_puts)

    # Near-expiry/free proxy assumption: use 1 trading day minimum.
    days_to_expiry = 1.0

    calls["gamma"] = _simple_gamma(proxy_price, calls["strike"], calls["impliedVolatility"], days_to_expiry)
    puts["gamma"] = _simple_gamma(proxy_price, puts["strike"], puts["impliedVolatility"], days_to_expiry)

    calls["call_gex"] = calls["gamma"] * calls["openInterest"].clip(lower=0) * 100.0
    puts["put_gex"] = puts["gamma"] * puts["openInterest"].clip(lower=0) * 100.0

    grouped_calls = calls.groupby("strike", as_index=False)["call_gex"].sum()
    grouped_puts = puts.groupby("strike", as_index=False)["put_gex"].sum()
    exposure = pd.merge(grouped_calls, grouped_puts, on="strike", how="outer").fillna(0.0)
    exposure["net_gex"] = exposure["call_gex"] - exposure["put_gex"]
    exposure["total_gex"] = exposure["call_gex"] + exposure["put_gex"]

    above = exposure[exposure["strike"] >= proxy_price].copy()
    below = exposure[exposure["strike"] <= proxy_price].copy()

    if not above.empty:
        cr_proxy = float(above.sort_values("call_gex", ascending=False)["strike"].iloc[0])
    else:
        cr_proxy = proxy_price * 1.01

    if not below.empty:
        ps_proxy = float(below.sort_values("put_gex", ascending=False)["strike"].iloc[0])
    else:
        ps_proxy = proxy_price * 0.99

    # HVL/gamma flip: closest strike where cumulative net gamma changes sign.
    exp_sorted = exposure.sort_values("strike").copy()
    exp_sorted["cum_net"] = exp_sorted["net_gex"].cumsum()
    flip_proxy = proxy_price
    if not exp_sorted.empty:
        sign_change = exp_sorted[(exp_sorted["cum_net"].shift(1).fillna(exp_sorted["cum_net"]) * exp_sorted["cum_net"]) < 0]
        if not sign_change.empty:
            flip_proxy = float(sign_change.iloc[(sign_change["strike"] - proxy_price).abs().argsort()[:1]]["strike"].iloc[0])
        else:
            flip_proxy = float(exp_sorted.iloc[(exp_sorted["cum_net"]).abs().argsort()[:1]]["strike"].iloc[0])

    gamma_wall_proxy = float(exposure.sort_values("total_gex", ascending=False)["strike"].iloc[0]) if not exposure.empty else proxy_price

    em_high, em_low = _expected_move(proxy_price, calls, puts, scale_ratio, futures_price, tick)
    gex_levels = _top_strikes(exposure, 10, futures_price, scale_ratio, tick)
    while len(gex_levels) < 10:
        offset = (len(gex_levels) + 1) * tick * 10
        gex_levels.append(_round_to_tick(futures_price + (offset if len(gex_levels) % 2 == 0 else -offset), tick))

    levelset = GammaLevelSet(
        symbol=symbol,
        futures_symbol=futures_symbol,
        proxy_symbol=proxy_symbol,
        source=f"FREE_PROXY_YAHOO_{proxy_symbol}_OPTIONS_SCALED_TO_{symbol}",
        generated_at_utc=_now_utc(),
        futures_price=_round_to_tick(futures_price, tick),
        proxy_price=round(proxy_price, 4),
        scale_ratio=round(scale_ratio, 8),
        call_resistance=_round_to_tick(cr_proxy * scale_ratio, tick),
        put_support=_round_to_tick(ps_proxy * scale_ratio, tick),
        hvl_gamma_flip=_round_to_tick(flip_proxy * scale_ratio, tick),
        zero_dte_call_wall=_round_to_tick(cr_proxy * scale_ratio, tick),
        zero_dte_put_wall=_round_to_tick(ps_proxy * scale_ratio, tick),
        zero_dte_gamma_wall=_round_to_tick(gamma_wall_proxy * scale_ratio, tick),
        expected_move_high=em_high,
        expected_move_low=em_low,
        gex_levels=gex_levels[:10],
        note="Free proxy level set. Uses ETF options and scales strikes to futures. Not MenthorQ, not exact CME futures options."
    )

    return levelset


def write_outputs(levels: GammaLevelSet) -> None:
    prefix = levels.symbol.upper()

    json_path = OUT_DIR / f"{prefix}_levels.json"
    csv_path = OUT_DIR / f"{prefix}_levels.csv"
    txt_path = OUT_DIR / f"{prefix}_pine_manual_backup.txt"
    latest_path = OUT_DIR / f"latest_{prefix.lower()}.json"

    data = asdict(levels)

    json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    latest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    flat = {
        "symbol": levels.symbol,
        "generated_at_utc": levels.generated_at_utc,
        "futures_price": levels.futures_price,
        "call_resistance": levels.call_resistance,
        "put_support": levels.put_support,
        "hvl_gamma_flip": levels.hvl_gamma_flip,
        "zero_dte_call_wall": levels.zero_dte_call_wall,
        "zero_dte_put_wall": levels.zero_dte_put_wall,
        "zero_dte_gamma_wall": levels.zero_dte_gamma_wall,
        "expected_move_high": levels.expected_move_high,
        "expected_move_low": levels.expected_move_low,
    }
    for i, lvl in enumerate(levels.gex_levels, start=1):
        flat[f"gex_{i}"] = lvl

    pd.DataFrame([flat]).to_csv(csv_path, index=False)

    txt = []
    txt.append(f"ALFRED GAMMA LEVELS — {levels.symbol}")
    txt.append(f"Generated: {levels.generated_at_utc}")
    txt.append(f"Source: {levels.source}")
    txt.append("")
    txt.append("Paste these into AlfredsEye Option Levels inputs:")
    txt.append("")
    txt.append(f"Call Resistance / Call Wall: {levels.call_resistance}")
    txt.append(f"Put Support / Put Wall: {levels.put_support}")
    txt.append(f"HVL / Gamma Flip: {levels.hvl_gamma_flip}")
    txt.append(f"0DTE Call Wall: {levels.zero_dte_call_wall}")
    txt.append(f"0DTE Put Wall: {levels.zero_dte_put_wall}")
    txt.append(f"0DTE Gamma Wall: {levels.zero_dte_gamma_wall}")
    txt.append(f"1D Max / Expected Move High: {levels.expected_move_high}")
    txt.append(f"1D Min / Expected Move Low: {levels.expected_move_low}")
    txt.append("")
    for i, lvl in enumerate(levels.gex_levels, start=1):
        txt.append(f"GEX {i}: {lvl}")
    txt.append("")
    txt.append(levels.note)

    txt_path.write_text("\n".join(txt), encoding="utf-8")
    print(f"[OK] Wrote {json_path}, {csv_path}, {txt_path}")


def main() -> None:
    configs = [
        # NQ/MNQ proxy
        ("NQ", "NQ=F", "QQQ", 0.25),
        # ES/MES proxy
        ("ES", "ES=F", "SPY", 0.25),
    ]

    built = 0
    for symbol, futures_symbol, proxy_symbol, tick in configs:
        try:
            levels = build_levels(symbol, futures_symbol, proxy_symbol, tick)
            if levels is not None:
                write_outputs(levels)
                built += 1
        except Exception as e:
            print(f"[ERROR] Failed to build {symbol}: {e}")

    if built == 0:
        raise RuntimeError("No gamma level files were generated.")
    print(f"[DONE] Generated {built} symbol level set(s).")


if __name__ == "__main__":
    main()
