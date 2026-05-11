#!/usr/bin/env python3
"""Alfred Gamma Engine — Free proxy build.

No paid data. No MenthorQ. Uses public Yahoo Finance option chains via yfinance.

Proxy map:
- NQ/MNQ -> QQQ options, scaled to NQ futures using NQ=F / QQQ.
- ES/MES -> SPY options, scaled to ES futures using ES=F / SPY.

This is an automatic free proxy, not exact CME futures-options dealer gamma.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yfinance as yf
from dateutil import parser as date_parser

CONTRACT_SIZE = 100.0
RISK_FREE_RATE = 0.045
MIN_IV = 0.02
MAX_IV = 3.00

MARKET_MAP = {
    "NQ": {"option_ticker": "QQQ", "future_ticker": "NQ=F", "name": "Nasdaq proxy using QQQ options"},
    "MNQ": {"option_ticker": "QQQ", "future_ticker": "NQ=F", "name": "Micro Nasdaq proxy using QQQ options"},
    "ES": {"option_ticker": "SPY", "future_ticker": "ES=F", "name": "S&P proxy using SPY options"},
    "MES": {"option_ticker": "SPY", "future_ticker": "ES=F", "name": "Micro S&P proxy using SPY options"},
}

@dataclass
class GammaLevel:
    symbol: str
    market: str
    source: str
    source_detail: str
    generated_at_utc: str
    option_ticker: str
    future_ticker: str
    option_spot: float
    futures_spot: float
    scale_ratio: float
    nearest_expiry: str
    call_wall: Optional[float]
    put_wall: Optional[float]
    hvl_gamma_flip: Optional[float]
    front_call_wall: Optional[float]
    front_put_wall: Optional[float]
    front_gamma_wall: Optional[float]
    expected_move_high: Optional[float]
    expected_move_low: Optional[float]
    expected_move_points: Optional[float]
    net_gex_regime: str
    total_call_gex: float
    total_put_gex: float
    total_net_gex: float
    gex_levels: List[Dict[str, float | str]]
    warnings: List[str]


def norm_pdf(x: np.ndarray | float) -> np.ndarray | float:
    return np.exp(-0.5 * np.asarray(x) ** 2) / math.sqrt(2.0 * math.pi)


def bs_gamma(S: float, K: np.ndarray, iv: np.ndarray, T: float, r: float = RISK_FREE_RATE) -> np.ndarray:
    iv = np.clip(iv.astype(float), MIN_IV, MAX_IV)
    K = K.astype(float)
    T = max(T, 1.0 / 365.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = (np.log(S / K) + (r + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
        gamma = norm_pdf(d1) / (S * iv * math.sqrt(T))
    return np.nan_to_num(gamma, nan=0.0, posinf=0.0, neginf=0.0)


def safe_mid(row: pd.Series) -> float:
    bid = float(row.get("bid", 0) or 0)
    ask = float(row.get("ask", 0) or 0)
    last = float(row.get("lastPrice", 0) or 0)
    if bid > 0 and ask > 0 and ask >= bid:
        return (bid + ask) / 2.0
    if last > 0:
        return last
    if ask > 0:
        return ask
    if bid > 0:
        return bid
    return 0.0


def get_last_price(ticker: str) -> float:
    tk = yf.Ticker(ticker)
    hist = tk.history(period="5d", interval="1d", auto_adjust=False)
    if hist.empty:
        raise RuntimeError(f"No price history for {ticker}")
    return float(hist["Close"].dropna().iloc[-1])


def get_expiry_t(max_days: int, expiry: str) -> float:
    exp_dt = date_parser.parse(expiry).date()
    now = datetime.now(timezone.utc).date()
    days = max((exp_dt - now).days, 0)
    # Give same-day/front expiry a small positive time value.
    return max(days / 365.0, 1.0 / 365.0)


def pick_expiries(ticker: yf.Ticker, max_days: int) -> List[str]:
    today = datetime.now(timezone.utc).date()
    expiries = []
    for exp in ticker.options:
        exp_date = date_parser.parse(exp).date()
        if 0 <= (exp_date - today).days <= max_days:
            expiries.append(exp)
    if not expiries and ticker.options:
        expiries = [ticker.options[0]]
    return expiries


def prep_chain(opt: pd.DataFrame, side: str, expiry: str, S: float) -> pd.DataFrame:
    if opt is None or opt.empty:
        return pd.DataFrame()
    df = opt.copy()
    df["side"] = side
    df["expiry"] = expiry
    df["mid"] = df.apply(safe_mid, axis=1)
    df["openInterest"] = pd.to_numeric(df.get("openInterest", 0), errors="coerce").fillna(0.0)
    df["volume"] = pd.to_numeric(df.get("volume", 0), errors="coerce").fillna(0.0)
    df["impliedVolatility"] = pd.to_numeric(df.get("impliedVolatility", np.nan), errors="coerce")
    df["impliedVolatility"] = df["impliedVolatility"].fillna(df["impliedVolatility"].median()).fillna(0.25)
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df = df.dropna(subset=["strike"])
    T = get_expiry_t(60, expiry)
    df["T"] = T
    df["gamma"] = bs_gamma(S, df["strike"].to_numpy(dtype=float), df["impliedVolatility"].to_numpy(dtype=float), T)
    # Dollar gamma per 1% underlying move, used for ranking. Dealer sign assumption: calls positive, puts negative.
    raw_gex = df["gamma"].to_numpy() * (S ** 2) * CONTRACT_SIZE * df["openInterest"].to_numpy() * 0.01
    df["gex_abs"] = np.abs(raw_gex)
    df["gex_signed"] = raw_gex if side == "call" else -raw_gex
    return df


def grouped_by_strike(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["strike", "call_gex", "put_gex", "net_gex", "abs_gex"])
    calls = df[df["side"] == "call"].groupby("strike", as_index=False)["gex_abs"].sum().rename(columns={"gex_abs": "call_gex"})
    puts = df[df["side"] == "put"].groupby("strike", as_index=False)["gex_abs"].sum().rename(columns={"gex_abs": "put_gex"})
    out = pd.merge(calls, puts, on="strike", how="outer").fillna(0.0)
    out["net_gex"] = out["call_gex"] - out["put_gex"]
    out["abs_gex"] = out["call_gex"] + out["put_gex"]
    return out.sort_values("strike").reset_index(drop=True)


def scale_level(level: Optional[float], ratio: float) -> Optional[float]:
    if level is None or pd.isna(level) or level <= 0:
        return None
    return round(float(level) * ratio, 2)


def max_strike(df: pd.DataFrame, mask: pd.Series, col: str) -> Optional[float]:
    sub = df[mask].copy()
    if sub.empty or sub[col].max() <= 0:
        return None
    row = sub.loc[sub[col].idxmax()]
    return float(row["strike"])


def gamma_flip(grouped: pd.DataFrame, spot: float) -> Optional[float]:
    if grouped.empty:
        return None
    g = grouped.sort_values("strike").copy()
    g["cum_net"] = g["net_gex"].cumsum()
    # Sign-cross candidates.
    signs = np.sign(g["cum_net"].to_numpy())
    strikes = g["strike"].to_numpy(dtype=float)
    candidates = []
    for i in range(1, len(signs)):
        if signs[i] == 0:
            candidates.append(strikes[i])
        elif signs[i - 1] == 0:
            candidates.append(strikes[i - 1])
        elif signs[i] != signs[i - 1]:
            candidates.append((strikes[i] + strikes[i - 1]) / 2.0)
    if candidates:
        return float(min(candidates, key=lambda x: abs(x - spot)))
    # Fallback: strike where cumulative net gamma is closest to zero.
    idx = g["cum_net"].abs().idxmin()
    return float(g.loc[idx, "strike"])


def expected_move(front_calls: pd.DataFrame, front_puts: pd.DataFrame, spot: float, ratio: float) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if front_calls.empty or front_puts.empty:
        return None, None, None
    strikes = sorted(set(front_calls["strike"].round(6)).intersection(set(front_puts["strike"].round(6))))
    if not strikes:
        return None, None, None
    atm = min(strikes, key=lambda k: abs(k - spot))
    c = front_calls.loc[(front_calls["strike"].round(6) == round(atm, 6))]
    p = front_puts.loc[(front_puts["strike"].round(6) == round(atm, 6))]
    if c.empty or p.empty:
        return None, None, None
    straddle = float(c.iloc[0]["mid"] + p.iloc[0]["mid"])
    if straddle <= 0:
        # IV fallback using average ATM IV and front expiry T.
        iv = float(np.nanmean([c.iloc[0]["impliedVolatility"], p.iloc[0]["impliedVolatility"]]))
        T = float(c.iloc[0]["T"])
        straddle = spot * iv * math.sqrt(T)
    em = round(straddle * ratio, 2)
    futures_spot = spot * ratio
    return round(futures_spot + em, 2), round(futures_spot - em, 2), em


def build_levels(market: str, max_days: int = 45, top_n: int = 10) -> GammaLevel:
    market = market.upper()
    if market not in MARKET_MAP:
        raise ValueError(f"Unsupported market {market}. Use one of {', '.join(MARKET_MAP)}")
    cfg = MARKET_MAP[market]
    option_ticker = cfg["option_ticker"]
    future_ticker = cfg["future_ticker"]
    opt_tk = yf.Ticker(option_ticker)

    option_spot = get_last_price(option_ticker)
    futures_spot = get_last_price(future_ticker)
    ratio = futures_spot / option_spot

    expiries = pick_expiries(opt_tk, max_days=max_days)
    if not expiries:
        raise RuntimeError(f"No option expiries found for {option_ticker}")

    warnings: List[str] = []
    frames = []
    front_calls = pd.DataFrame()
    front_puts = pd.DataFrame()
    for exp in expiries:
        try:
            chain = opt_tk.option_chain(exp)
            calls = prep_chain(chain.calls, "call", exp, option_spot)
            puts = prep_chain(chain.puts, "put", exp, option_spot)
            if exp == expiries[0]:
                front_calls, front_puts = calls, puts
            frames.extend([calls, puts])
        except Exception as e:
            warnings.append(f"Failed expiry {exp}: {e}")

    chain_all = pd.concat([f for f in frames if not f.empty], ignore_index=True) if frames else pd.DataFrame()
    if chain_all.empty:
        raise RuntimeError("No option-chain rows loaded")

    grouped = grouped_by_strike(chain_all)
    front_grouped = grouped_by_strike(pd.concat([front_calls, front_puts], ignore_index=True)) if not front_calls.empty or not front_puts.empty else pd.DataFrame()

    call_wall = max_strike(grouped, grouped["strike"] >= option_spot, "call_gex")
    put_wall = max_strike(grouped, grouped["strike"] <= option_spot, "put_gex")
    hvl = gamma_flip(grouped, option_spot)

    front_call_wall = max_strike(front_grouped, front_grouped["strike"] >= option_spot, "call_gex") if not front_grouped.empty else None
    front_put_wall = max_strike(front_grouped, front_grouped["strike"] <= option_spot, "put_gex") if not front_grouped.empty else None
    front_gamma_wall = max_strike(front_grouped, pd.Series([True] * len(front_grouped)), "abs_gex") if not front_grouped.empty else None

    em_high, em_low, em_points = expected_move(front_calls, front_puts, option_spot, ratio)

    top = grouped.sort_values("abs_gex", ascending=False).head(top_n).copy()
    top["distance"] = (top["strike"] - option_spot).abs()
    gex_levels = []
    for rank, row in enumerate(top.itertuples(), start=1):
        gex_levels.append({
            "rank": rank,
            "strike_proxy": round(float(row.strike), 2),
            "level": scale_level(float(row.strike), ratio),
            "type": "positive" if float(row.net_gex) >= 0 else "negative",
            "net_gex": round(float(row.net_gex), 2),
            "abs_gex": round(float(row.abs_gex), 2),
        })

    total_call_gex = float(grouped["call_gex"].sum())
    total_put_gex = float(grouped["put_gex"].sum())
    total_net_gex = total_call_gex - total_put_gex
    net_regime = "Positive" if total_net_gex > 0 else "Negative" if total_net_gex < 0 else "Neutral"

    return GammaLevel(
        symbol=f"{market}_GAMMA_LEVELS",
        market=market,
        source="FREE_PROXY_YAHOO",
        source_detail=f"{cfg['name']}; scaled by {future_ticker}/{option_ticker} ratio. Not exact CME futures options.",
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
        option_ticker=option_ticker,
        future_ticker=future_ticker,
        option_spot=round(option_spot, 4),
        futures_spot=round(futures_spot, 4),
        scale_ratio=round(ratio, 8),
        nearest_expiry=expiries[0],
        call_wall=scale_level(call_wall, ratio),
        put_wall=scale_level(put_wall, ratio),
        hvl_gamma_flip=scale_level(hvl, ratio),
        front_call_wall=scale_level(front_call_wall, ratio),
        front_put_wall=scale_level(front_put_wall, ratio),
        front_gamma_wall=scale_level(front_gamma_wall, ratio),
        expected_move_high=em_high,
        expected_move_low=em_low,
        expected_move_points=em_points,
        net_gex_regime=net_regime,
        total_call_gex=round(total_call_gex, 2),
        total_put_gex=round(total_put_gex, 2),
        total_net_gex=round(total_net_gex, 2),
        gex_levels=gex_levels,
        warnings=warnings,
    )


def write_outputs(levels: GammaLevel, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = levels.market
    data = asdict(levels)

    (out_dir / f"{prefix}_levels.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

    rows = [
        ("CALL_WALL", levels.call_wall),
        ("PUT_WALL", levels.put_wall),
        ("HVL_GAMMA_FLIP", levels.hvl_gamma_flip),
        ("FRONT_CALL_WALL", levels.front_call_wall),
        ("FRONT_PUT_WALL", levels.front_put_wall),
        ("FRONT_GAMMA_WALL", levels.front_gamma_wall),
        ("EXPECTED_MOVE_HIGH", levels.expected_move_high),
        ("EXPECTED_MOVE_LOW", levels.expected_move_low),
    ]
    for g in levels.gex_levels:
        rows.append((f"GEX{g['rank']}", g["level"]))
    pd.DataFrame(rows, columns=["level_name", "price"]).to_csv(out_dir / f"{prefix}_levels.csv", index=False)

    # Pine manual backup text. This is a fallback until a real TradingView feed path exists.
    lines = [
        f"// Alfred Gamma Engine manual backup — {levels.market}",
        f"// Source: {levels.source}",
        f"// Generated: {levels.generated_at_utc}",
        f"Call Resistance / Call Wall: {levels.call_wall}",
        f"Put Support / Put Wall: {levels.put_wall}",
        f"HVL / Gamma Flip: {levels.hvl_gamma_flip}",
        f"0DTE/Front Call Wall: {levels.front_call_wall}",
        f"0DTE/Front Put Wall: {levels.front_put_wall}",
        f"0DTE/Front Gamma Wall: {levels.front_gamma_wall}",
        f"1D Max / EM High: {levels.expected_move_high}",
        f"1D Min / EM Low: {levels.expected_move_low}",
        f"Net GEX Regime: {levels.net_gex_regime}",
    ]
    for g in levels.gex_levels:
        lines.append(f"GEX {g['rank']}: {g['level']} ({g['type']})")
    (out_dir / f"{prefix}_pine_manual_backup.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # One flat latest file for simple dashboards.
    if levels.market == "NQ":
        (out_dir / "latest_nq.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    if levels.market == "ES":
        (out_dir / "latest_es.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="NQ", choices=sorted(MARKET_MAP.keys()))
    ap.add_argument("--output", default="out")
    ap.add_argument("--max-days", type=int, default=45)
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    levels = build_levels(args.market, max_days=args.max_days, top_n=args.top)
    write_outputs(levels, Path(args.output))
    print(json.dumps(asdict(levels), indent=2))


if __name__ == "__main__":
    main()
