from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd


# Approximate monthly distribution for a generic German PV profile.
# It is used only to distribute an annual PV yield across the 8,760 hours.
# The annual energy is always rescaled exactly to the user's input yield.
GERMANY_MONTHLY_PV_SHARES = np.array(
    [0.018, 0.035, 0.075, 0.105, 0.125, 0.135,
     0.135, 0.120, 0.095, 0.072, 0.050, 0.035],
    dtype=float,
)


@dataclass(frozen=True)
class BatterySpec:
    family: str
    label: str
    nominal_kwh: float
    usable_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    max_pv_kwp: float
    source_note: str


def battery_catalogue() -> list[BatterySpec]:
    """Battery variants based on the two uploaded LumenHaus datasheets."""
    items: list[BatterySpec] = []

    # SunMini: base battery 4.02 kWh; extension module 2.01 kWh;
    # technical table states max system capacity 12.06 kWh.
    for n_extensions in range(0, 5):
        nominal = 4.02 + 2.01 * n_extensions
        items.append(
            BatterySpec(
                family="SunMini",
                label=f"SunMini {nominal:.2f} kWh",
                nominal_kwh=nominal,
                # No DoD / usable-energy value is stated in the uploaded sheet.
                # MVP therefore treats nominal as usable and exposes efficiency separately.
                usable_kwh=nominal,
                max_charge_kw=2.5,
                max_discharge_kw=2.5,
                max_pv_kwp=2.8,
                source_note=(
                    "SunMini technical table: 4.02 kWh base, 2.01 kWh extension, "
                    "12.06 kWh max system capacity, 2.8 kW max PV input, 2.5 kW output."
                ),
            )
        )

    # SunSaver: 10-30 kWh nominal, 95% DoD, 12 kW charge/discharge, 19.2 kW PV array.
    for nominal in (10, 15, 20, 25, 30):
        items.append(
            BatterySpec(
                family="SunSaver",
                label=f"SunSaver {nominal} kWh",
                nominal_kwh=float(nominal),
                usable_kwh=float(nominal) * 0.95,
                max_charge_kw=12.0,
                max_discharge_kw=12.0,
                max_pv_kwp=19.2,
                source_note=(
                    "SunSaver technical table: 10/15/20/25/30 kWh nominal, 95% DoD, "
                    "12 kW max charge/discharge, 19.2 kW recommended PV array."
                ),
            )
        )

    return items


def load_profile(path: str | Path) -> pd.DataFrame:
    """Load the normalized 8,760-hour German consumption profile."""
    df = pd.read_csv(path)
    required = {"date", "hour", "day_type", "month", "load_weight"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Profile is missing columns: {sorted(missing)}")
    if len(df) != 8760:
        raise ValueError(f"Expected 8,760 rows, found {len(df):,}")

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], dayfirst=True, errors="raise")
    df["timestamp"] = df["date"] + pd.to_timedelta(df["hour"].astype(int) - 1, unit="h")
    total = float(df["load_weight"].sum())
    if total <= 0:
        raise ValueError("load_weight must have a positive total")
    df["load_weight"] = df["load_weight"] / total
    return df


def _solar_shape_for_month(timestamps: pd.Series, latitude_deg: float) -> np.ndarray:
    """Clear-sky-like hourly shape based on solar elevation; relative weights only."""
    ts = pd.DatetimeIndex(timestamps)
    day = ts.dayofyear.to_numpy(dtype=float)
    # Mid-point of each hourly interval.
    solar_hour = ts.hour.to_numpy(dtype=float) + 0.5

    lat = np.deg2rad(latitude_deg)
    decl = np.deg2rad(23.45) * np.sin(2.0 * np.pi * (284.0 + day) / 365.0)
    hour_angle = np.deg2rad(15.0 * (solar_hour - 12.0))
    sin_elev = np.sin(lat) * np.sin(decl) + np.cos(lat) * np.cos(decl) * np.cos(hour_angle)
    # Slightly sharpen noon and reduce very-low-sun contributions.
    return np.maximum(sin_elev, 0.0) ** 1.25


def pv_hourly_profile(
    timestamps: pd.Series,
    annual_pv_kwh: float,
    latitude_deg: float = 51.0,
) -> np.ndarray:
    """
    Build a generic Germany hourly PV profile and scale it to annual_pv_kwh.

    Important: annual_pv_kwh is preserved exactly. The generic solar curve only
    controls *when* production occurs, which is necessary for battery dispatch.
    """
    if annual_pv_kwh <= 0:
        return np.zeros(len(timestamps), dtype=float)

    ts = pd.DatetimeIndex(timestamps)
    raw = _solar_shape_for_month(pd.Series(ts), latitude_deg)
    weights = np.zeros(len(ts), dtype=float)

    for month in range(1, 13):
        mask = ts.month == month
        month_raw = raw[mask]
        month_sum = float(month_raw.sum())
        if month_sum > 0:
            weights[mask] = month_raw / month_sum * GERMANY_MONTHLY_PV_SHARES[month - 1]

    total = float(weights.sum())
    if total <= 0:
        raise ValueError("Could not build a positive PV profile")
    weights /= total
    return weights * annual_pv_kwh


def build_hourly_energy(
    profile: pd.DataFrame,
    annual_consumption_kwh: float,
    panel_count: int,
    panel_power_w: float,
    yield_kwh_per_kwp: float,
    latitude_deg: float = 51.0,
) -> tuple[pd.DataFrame, dict]:
    if annual_consumption_kwh < 0:
        raise ValueError("annual_consumption_kwh must be >= 0")
    if panel_count < 0 or panel_power_w < 0 or yield_kwh_per_kwp < 0:
        raise ValueError("PV inputs must be >= 0")

    pv_kwp = panel_count * panel_power_w / 1000.0
    annual_pv_kwh = pv_kwp * yield_kwh_per_kwp

    out = profile[["timestamp", "day_type", "month", "load_weight"]].copy()
    out["load_kwh"] = out["load_weight"] * float(annual_consumption_kwh)
    out["pv_kwh"] = pv_hourly_profile(out["timestamp"], annual_pv_kwh, latitude_deg)

    meta = {
        "pv_kwp": pv_kwp,
        "annual_pv_kwh": annual_pv_kwh,
        "annual_consumption_kwh": float(annual_consumption_kwh),
    }
    return out, meta


def _dispatch_one_year(
    load_kwh: np.ndarray,
    pv_kwh: np.ndarray,
    battery: Optional[BatterySpec],
    roundtrip_efficiency: float,
    initial_soc_kwh: float,
) -> tuple[pd.DataFrame, float]:
    n = len(load_kwh)
    if n != len(pv_kwh):
        raise ValueError("load and PV arrays must have same length")

    if battery is None or battery.usable_kwh <= 0:
        direct = np.minimum(load_kwh, pv_kwh)
        export = np.maximum(pv_kwh - direct, 0.0)
        grid = np.maximum(load_kwh - direct, 0.0)
        result = pd.DataFrame(
            {
                "direct_solar_kwh": direct,
                "battery_charge_ac_kwh": np.zeros(n),
                "battery_discharge_ac_kwh": np.zeros(n),
                "soc_kwh": np.zeros(n),
                "grid_import_kwh": grid,
                "grid_export_kwh": export,
            }
        )
        return result, 0.0

    rte = float(roundtrip_efficiency)
    if not (0 < rte <= 1.0):
        raise ValueError("roundtrip_efficiency must be in (0, 1]")
    eta_c = np.sqrt(rte)
    eta_d = np.sqrt(rte)

    cap = float(battery.usable_kwh)
    max_c = float(battery.max_charge_kw)  # 1-hour step -> kWh per step
    max_d = float(battery.max_discharge_kw)
    soc = min(max(float(initial_soc_kwh), 0.0), cap)

    direct = np.zeros(n)
    charge_ac = np.zeros(n)
    discharge_ac = np.zeros(n)
    soc_trace = np.zeros(n)
    grid = np.zeros(n)
    export = np.zeros(n)

    for i in range(n):
        load = max(float(load_kwh[i]), 0.0)
        pv = max(float(pv_kwh[i]), 0.0)

        direct_i = min(load, pv)
        direct[i] = direct_i
        surplus = pv - direct_i
        deficit = load - direct_i

        # Charge only from PV surplus. AC-side charge is converted into stored energy.
        if surplus > 0 and soc < cap:
            charge_room_ac = (cap - soc) / eta_c
            c = min(surplus, max_c, charge_room_ac)
            charge_ac[i] = c
            soc += c * eta_c
            surplus -= c

        # Discharge only to serve remaining load; no grid arbitrage in MVP.
        if deficit > 0 and soc > 0:
            max_deliverable_from_soc = soc * eta_d
            d = min(deficit, max_d, max_deliverable_from_soc)
            discharge_ac[i] = d
            soc -= d / eta_d
            deficit -= d

        grid[i] = max(deficit, 0.0)
        export[i] = max(surplus, 0.0)
        soc = min(max(soc, 0.0), cap)
        soc_trace[i] = soc

    result = pd.DataFrame(
        {
            "direct_solar_kwh": direct,
            "battery_charge_ac_kwh": charge_ac,
            "battery_discharge_ac_kwh": discharge_ac,
            "soc_kwh": soc_trace,
            "grid_import_kwh": grid,
            "grid_export_kwh": export,
        }
    )
    return result, soc


def simulate(
    hourly: pd.DataFrame,
    battery: Optional[BatterySpec],
    energy_price_eur_per_kwh: float,
    export_price_eur_per_kwh: float = 0.0,
    roundtrip_efficiency: float = 0.90,
    settle_years: int = 4,
) -> tuple[pd.DataFrame, dict]:
    """Run hourly self-consumption + battery dispatch and return detailed KPIs."""
    load = hourly["load_kwh"].to_numpy(dtype=float)
    pv = hourly["pv_kwh"].to_numpy(dtype=float)

    # Find a near-periodic starting SOC to avoid Jan-1 initialization bias.
    if battery is None:
        initial_soc = 0.0
    else:
        initial_soc = 0.5 * battery.usable_kwh
        for _ in range(max(settle_years, 0)):
            _, end_soc = _dispatch_one_year(load, pv, battery, roundtrip_efficiency, initial_soc)
            if abs(end_soc - initial_soc) < 1e-6:
                break
            initial_soc = end_soc

    dispatch, end_soc = _dispatch_one_year(load, pv, battery, roundtrip_efficiency, initial_soc)
    result = pd.concat([hourly.reset_index(drop=True), dispatch], axis=1)

    annual_load = float(load.sum())
    annual_pv = float(pv.sum())
    direct = float(result["direct_solar_kwh"].sum())
    batt_discharge = float(result["battery_discharge_ac_kwh"].sum())
    batt_charge = float(result["battery_charge_ac_kwh"].sum())
    grid_import = float(result["grid_import_kwh"].sum())
    grid_export = float(result["grid_export_kwh"].sum())

    self_consumed = direct + batt_discharge
    self_consumption_ratio = self_consumed / annual_pv if annual_pv > 0 else 0.0
    autonomy_ratio = self_consumed / annual_load if annual_load > 0 else 0.0

    baseline_cost = annual_load * energy_price_eur_per_kwh
    net_energy_cost = grid_import * energy_price_eur_per_kwh - grid_export * export_price_eur_per_kwh
    savings_vs_grid_only = baseline_cost - net_energy_cost

    cycles = 0.0
    if battery is not None and battery.usable_kwh > 0:
        cycles = batt_discharge / battery.usable_kwh

    kpis = {
        "annual_load_kwh": annual_load,
        "annual_pv_kwh": annual_pv,
        "direct_solar_kwh": direct,
        "battery_charge_kwh": batt_charge,
        "battery_discharge_kwh": batt_discharge,
        "grid_import_kwh": grid_import,
        "grid_export_kwh": grid_export,
        "self_consumed_kwh": self_consumed,
        "self_consumption_ratio": self_consumption_ratio,
        "autonomy_ratio": autonomy_ratio,
        "baseline_cost_eur": baseline_cost,
        "net_energy_cost_eur": net_energy_cost,
        "savings_vs_grid_only_eur": savings_vs_grid_only,
        "equivalent_cycles_per_year": cycles,
        "initial_soc_kwh": initial_soc,
        "end_soc_kwh": end_soc,
    }
    return result, kpis


def compare_batteries(
    hourly: pd.DataFrame,
    pv_kwp: float,
    energy_price_eur_per_kwh: float,
    export_price_eur_per_kwh: float = 0.0,
    roundtrip_efficiency: float = 0.90,
    families: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    fam_set = set(families) if families else None

    _, pv_only = simulate(
        hourly,
        None,
        energy_price_eur_per_kwh,
        export_price_eur_per_kwh,
        roundtrip_efficiency,
    )

    records = []
    for bat in battery_catalogue():
        if fam_set is not None and bat.family not in fam_set:
            continue
        _, k = simulate(
            hourly,
            bat,
            energy_price_eur_per_kwh,
            export_price_eur_per_kwh,
            roundtrip_efficiency,
        )
        records.append(
            {
                "family": bat.family,
                "battery": bat.label,
                "nominal_kwh": bat.nominal_kwh,
                "usable_kwh": bat.usable_kwh,
                "pv_compatible": pv_kwp <= bat.max_pv_kwp + 1e-9,
                "self_consumption_pct": 100 * k["self_consumption_ratio"],
                "autonomy_pct": 100 * k["autonomy_ratio"],
                "grid_import_kwh": k["grid_import_kwh"],
                "grid_export_kwh": k["grid_export_kwh"],
                "annual_savings_eur": k["savings_vs_grid_only_eur"],
                "incremental_battery_savings_eur": (
                    k["savings_vs_grid_only_eur"] - pv_only["savings_vs_grid_only_eur"]
                ),
                "equivalent_cycles": k["equivalent_cycles_per_year"],
            }
        )
    return pd.DataFrame.from_records(records)


def heuristic_size(comparison: pd.DataFrame, capture_fraction: float = 0.90) -> Optional[pd.Series]:
    """
    Technical heuristic: smallest compatible battery capturing at least a fraction
    of the maximum incremental battery savings. It is NOT an economic optimum
    because battery CAPEX is not included.
    """
    if comparison.empty:
        return None
    valid = comparison[comparison["pv_compatible"]].copy()
    if valid.empty:
        return None
    max_inc = float(valid["incremental_battery_savings_eur"].max())
    if max_inc <= 0:
        return valid.sort_values("usable_kwh").iloc[0]
    target = capture_fraction * max_inc
    hits = valid[valid["incremental_battery_savings_eur"] >= target]
    if hits.empty:
        return valid.sort_values("incremental_battery_savings_eur", ascending=False).iloc[0]
    return hits.sort_values("usable_kwh").iloc[0]
