# SolarQ Battery Sizing MVP

A simple Streamlit app that converts the uploaded German 8,760-hour average load profile into a normalized annual profile, estimates PV production from system size and specific yield, and simulates hourly battery charging/discharging.

## Inputs

- Annual electricity consumption (kWh/year)
- Electricity purchase price (EUR/kWh)
- Optional export compensation (EUR/kWh)
- Number of PV panels
- Panel power (Wp)
- Specific PV yield (kWh/kWp/year)
- Battery family and size
- Advanced: generic-profile latitude and assumed round-trip battery efficiency

## Battery catalogue included

### SunMini
- Base: 4.02 kWh
- Extension module: 2.01 kWh
- Technical max system capacity used in this MVP: 12.06 kWh
- Max PV input used: 2.8 kW
- Charge/discharge power used: 2.5 kW

The uploaded datasheet does not state usable energy / DoD or round-trip efficiency, so the MVP treats nominal capacity as usable and keeps efficiency as an editable assumption.

### SunSaver
- Nominal sizes: 10 / 15 / 20 / 25 / 30 kWh
- 95% DoD -> usable energy = 95% of nominal
- Max charge/discharge power: 12 kW
- Max recommended PV array: 19.2 kW

## How the model works

1. The 8,760 hourly values in the source Excel are converted into weights summing to 1.
2. `hourly load = annual consumption × hourly load weight`.
3. `PV size (kWp) = panels × panel Wp / 1000`.
4. `annual PV = PV size × specific yield`.
5. A generic Germany solar curve distributes annual PV across 8,760 hours.
6. Each hour:
   - PV first serves load.
   - PV surplus charges the battery, subject to capacity and charge-power limits.
   - The battery discharges into the remaining load, subject to SOC and discharge-power limits.
   - Remaining deficit is imported; remaining surplus is exported.
7. Savings compare the simulated energy bill with a grid-only baseline.

## Run locally (Windows / macOS / Linux)

```bash
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

## Important limitations of this MVP

- The PV hourly shape is generic for Germany, not site-specific PVGIS data.
- Battery round-trip efficiency is an assumption because it is not in the provided sheets.
- No dynamic tariff / grid charging optimization.
- No battery CAPEX, installation cost, degradation, or replacement cost; therefore the app can compare technical performance and annual energy savings, but it cannot yet produce a true economic optimum or payback.
- The SunMini sheet contains marketing text about larger expansion/scaling that does not align clearly with the 12.06 kWh technical max shown in the table. This MVP uses the conservative technical-table limit until that is clarified.
