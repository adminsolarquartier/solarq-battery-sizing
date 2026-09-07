from pathlib import Path

import pandas as pd
import streamlit as st

from battery_model import (
    battery_catalogue,
    build_hourly_energy,
    compare_batteries,
    heuristic_size,
    load_profile,
    simulate,
)


BASE_DIR = Path(__file__).resolve().parent
PROFILE_PATH = BASE_DIR / "normalized_germany_profile.csv"

st.set_page_config(page_title="SolarQ Battery Sizing", page_icon="🔋", layout="wide")

st.title("SolarQ - Battery Sizing MVP")
st.caption(
    "Hourly simulation using the normalized German consumption profile + a generic German PV production shape."
)

profile = load_profile(PROFILE_PATH)
catalogue = battery_catalogue()

with st.sidebar:
    st.header("1. Consumption")
    annual_consumption = st.number_input(
        "Annual consumption (kWh/year)", min_value=0.0, value=3500.0, step=100.0
    )
    energy_price = st.number_input(
        "Grid energy price (€/kWh)", min_value=0.0, value=0.35, step=0.01, format="%.3f"
    )
    export_price = st.number_input(
        "Export compensation (€/kWh)", min_value=0.0, value=0.00, step=0.01, format="%.3f"
    )

    st.header("2. PV system")
    panels = st.number_input("Number of panels", min_value=0, value=8, step=1)
    panel_power = st.number_input("Panel power (Wp)", min_value=0.0, value=450.0, step=10.0)
    pv_yield = st.number_input(
        "Specific yield (kWh/kWp·year)", min_value=0.0, value=950.0, step=25.0
    )

    with st.expander("Advanced modelling assumptions"):
        latitude = st.number_input(
            "Latitude for hourly PV shape (°)", min_value=47.0, max_value=55.0, value=51.0, step=0.1
        )
        roundtrip_eff = st.slider(
            "Battery round-trip efficiency (assumption)", 0.70, 1.00, 0.90, 0.01
        )
        st.caption(
            "Efficiency is not stated in the uploaded product sheets, so it remains an editable assumption."
        )

hourly, meta = build_hourly_energy(
    profile,
    annual_consumption,
    int(panels),
    panel_power,
    pv_yield,
    latitude,
)

st.subheader("System overview")
col1, col2, col3, col4 = st.columns(4)
col1.metric("PV size", f"{meta['pv_kwp']:.2f} kWp")
col2.metric("Annual PV production", f"{meta['annual_pv_kwh']:,.0f} kWh")
col3.metric("Annual consumption", f"{annual_consumption:,.0f} kWh")
col4.metric("PV / load ratio", f"{(meta['annual_pv_kwh']/annual_consumption if annual_consumption else 0):.0%}")

st.divider()

mode = st.radio(
    "Battery analysis",
    ["Compare all available sizes", "Inspect one battery"],
    horizontal=True,
)

if mode == "Compare all available sizes":
    family_filter = st.multiselect(
        "Battery families",
        ["SunMini", "SunSaver"],
        default=["SunMini", "SunSaver"],
    )

    comparison = compare_batteries(
        hourly,
        meta["pv_kwp"],
        energy_price,
        export_price,
        roundtrip_eff,
        families=family_filter,
    )

    if comparison.empty:
        st.info("Select at least one battery family.")
    else:
        recommended = heuristic_size(comparison, 0.90)
        if recommended is not None:
            st.success(
                "Technical sizing heuristic: **{}**. It is the smallest compatible option that captures at least "
                "90% of the maximum incremental battery savings in the selected range. This is not yet a financial "
                "optimum because battery CAPEX is not included.".format(recommended["battery"])
            )

        display = comparison.copy()
        display["self_consumption_pct"] = display["self_consumption_pct"].map(lambda x: f"{x:.1f}%")
        display["autonomy_pct"] = display["autonomy_pct"].map(lambda x: f"{x:.1f}%")
        display["annual_savings_eur"] = display["annual_savings_eur"].map(lambda x: f"€{x:,.0f}")
        display["incremental_battery_savings_eur"] = display["incremental_battery_savings_eur"].map(lambda x: f"€{x:,.0f}")
        display["equivalent_cycles"] = display["equivalent_cycles"].map(lambda x: f"{x:.0f}")
        display["grid_import_kwh"] = display["grid_import_kwh"].map(lambda x: f"{x:,.0f}")
        display["grid_export_kwh"] = display["grid_export_kwh"].map(lambda x: f"{x:,.0f}")
        display = display.rename(
            columns={
                "family": "Family",
                "battery": "Battery",
                "nominal_kwh": "Nominal kWh",
                "usable_kwh": "Usable kWh",
                "pv_compatible": "PV compatible",
                "self_consumption_pct": "Self-consumption",
                "autonomy_pct": "Autonomy",
                "grid_import_kwh": "Grid import kWh",
                "grid_export_kwh": "Grid export kWh",
                "annual_savings_eur": "Total annual savings",
                "incremental_battery_savings_eur": "Battery-only incremental savings",
                "equivalent_cycles": "Equivalent cycles/year",
            }
        )
        st.dataframe(display, use_container_width=True, hide_index=True)

        incompatible = comparison.loc[~comparison["pv_compatible"], "battery"].tolist()
        if incompatible:
            st.warning(
                "PV input exceeds the technical PV limit used for: " + ", ".join(incompatible) + "."
            )

        chart_df = comparison.set_index("battery")[["incremental_battery_savings_eur"]]
        st.bar_chart(chart_df)

else:
    labels = [b.label for b in catalogue]
    selected_label = st.selectbox("Battery", labels)
    battery = next(b for b in catalogue if b.label == selected_label)

    if meta["pv_kwp"] > battery.max_pv_kwp:
        st.warning(
            f"PV size is {meta['pv_kwp']:.2f} kWp, above the {battery.max_pv_kwp:.1f} kWp PV limit used for {battery.family}."
        )

    detail, k = simulate(
        hourly,
        battery,
        energy_price,
        export_price,
        roundtrip_eff,
    )
    _, pv_only = simulate(hourly, None, energy_price, export_price, roundtrip_eff)
    battery_incremental = k["savings_vs_grid_only_eur"] - pv_only["savings_vs_grid_only_eur"]

    st.caption(battery.source_note)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Usable battery", f"{battery.usable_kwh:.2f} kWh")
    c2.metric("Self-consumption", f"{k['self_consumption_ratio']:.1%}")
    c3.metric("Autonomy", f"{k['autonomy_ratio']:.1%}")
    c4.metric("Equivalent cycles/year", f"{k['equivalent_cycles_per_year']:.0f}")

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Grid import", f"{k['grid_import_kwh']:,.0f} kWh")
    c6.metric("Grid export", f"{k['grid_export_kwh']:,.0f} kWh")
    c7.metric("Total savings vs grid-only", f"€{k['savings_vs_grid_only_eur']:,.0f}/yr")
    c8.metric("Incremental value of battery", f"€{battery_incremental:,.0f}/yr")

    st.subheader("Monthly energy balance")
    monthly = detail.assign(month=detail["timestamp"].dt.to_period("M").astype(str)).groupby("month").agg(
        Load_kWh=("load_kwh", "sum"),
        PV_kWh=("pv_kwh", "sum"),
        Grid_import_kWh=("grid_import_kwh", "sum"),
        Grid_export_kWh=("grid_export_kwh", "sum"),
        Battery_discharge_kWh=("battery_discharge_ac_kwh", "sum"),
    )
    st.bar_chart(monthly[["Load_kWh", "PV_kWh", "Grid_import_kWh"]])

    st.subheader("Example week - load, PV and battery SOC")
    # Choose a summer week to make charge/discharge behaviour easy to inspect.
    mask = (detail["timestamp"] >= pd.Timestamp("2025-06-16")) & (
        detail["timestamp"] < pd.Timestamp("2025-06-23")
    )
    week = detail.loc[mask, ["timestamp", "load_kwh", "pv_kwh", "soc_kwh"]].set_index("timestamp")
    week = week.rename(columns={"load_kwh": "Load kWh", "pv_kwh": "PV kWh", "soc_kwh": "Battery SOC kWh"})
    st.line_chart(week)

    st.download_button(
        "Download hourly simulation CSV",
        data=detail.to_csv(index=False).encode("utf-8"),
        file_name="solarq_battery_hourly_simulation.csv",
        mime="text/csv",
    )

st.divider()
st.markdown(
    "**MVP assumptions:** the uploaded German load curve is normalized to the annual-consumption input; "
    "PV annual production is `panels × Wp / 1000 × yield`; a generic German hourly solar shape distributes "
    "that annual PV energy; the battery charges only from PV surplus and discharges only to cover load; "
    "fixed charges, taxes, dynamic tariffs and battery CAPEX are not yet included."
)
