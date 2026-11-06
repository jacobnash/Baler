# Vendored from PNNL ConStrain 0.8.0: constrain/library/zone_temp_control.py,
# supply_air_temp_reset.py, chilled_water_reset.py and hot_water_reset.py.
# Copyright (c) 2021, Battelle Memorial Institute. BSD 2-Clause License,
# full text in this package's __init__.py.
#
# `points` and verify() are upstream's, unchanged (only reformatted). The
# plot()/calculate_plot_day() overrides are dropped. Upstream's docstrings,
# trimmed to the requirement each item checks, are kept for reference.
# ruff: noqa: RUF012 - upstream `points` lists kept as written (vendored)

from __future__ import annotations

from baler.commissioning.vendor_constrain.checklib import RuleCheckBase


class ZoneTempControl(RuleCheckBase):
    """ASHRAE 90.1-2019 6.4.3.1.2 Dead Band: zone thermostatic controls
    that control both heating and cooling shall provide a dead band of at
    least 5 F (2.77 C). Pass per sample when cooling setpoint - heating
    setpoint > 2.77 C (less the zone temperature tolerance)."""

    points = [
        "temperature_air_zone_cool_setpoint",
        "temperature_air_zone_heat_setpoint",
    ]

    def verify(self):
        self.result = (
            self.df["temperature_air_zone_cool_setpoint"] - self.df["temperature_air_zone_heat_setpoint"]
        ) > (2.77 - self.get_tolerance("temperature", "zone"))


class SupplyAirTempReset(RuleCheckBase):
    """ASHRAE 90.1-2016 6.5.3.5 Supply Air Temperature Reset: the SAT
    setpoint's range over the data must be at least 25% of (design zone
    cooling setpoint - minimum SAT setpoint)."""

    points = [
        "temperature_air_supply_setpoint",
        "temperature_air_zone_design_cool_setpoint",
    ]

    def verify(self):
        t_sa_set_max = max(self.df["temperature_air_supply_setpoint"])
        t_sa_set_min = min(self.df["temperature_air_supply_setpoint"])

        self.result = (t_sa_set_max - t_sa_set_min) >= (
            self.df["temperature_air_zone_design_cool_setpoint"] - t_sa_set_min
        ) * 0.25 * (1 - self.get_tolerance("ratio", "temperature") * 1)


class CHWReset(RuleCheckBase):
    """ASHRAE 90.1-2016 6.5.4.4 Chilled-Water Temperature Reset Controls.
    Per sample: no flow, or the CHW temperature is where the outdoor-air
    reset schedule allows it (see todo/cx3-constrain-gl36-spike.md for
    how loose upstream's low-OAT branch is)."""

    points = [
        "temperature_air_outdoor",
        "temperature_air_outdoor_max",
        "temperature_air_outdoor_min",
        "temperature_water_chilled",
        "flow_mass_water_chilled",
        "temperature_water_chilled_setpoint_max",
        "temperature_water_chilled_setpoint_min",
    ]

    def verify(self):
        self.result = (
            (self.df["flow_mass_water_chilled"] <= self.get_tolerance("waterflow", "chilled"))
            | (
                (
                    self.df["temperature_air_outdoor"]
                    <= self.df["temperature_air_outdoor_min"] + self.get_tolerance("temperature", "outdoor_air")
                )
                & (
                    self.df["temperature_water_chilled"]
                    >= self.df["temperature_water_chilled_setpoint_min"] - self.get_tolerance("temperature", "general")
                )
            )
            | (
                (
                    self.df["temperature_air_outdoor"]
                    >= (self.df["temperature_air_outdoor_max"] - self.get_tolerance("temperature", "outdoor_air"))
                )
                & (
                    self.df["temperature_water_chilled"]
                    <= self.df["temperature_water_chilled_setpoint_max"] + self.get_tolerance("temperature", "general")
                )
            )
            | (
                (
                    (
                        self.df["temperature_air_outdoor"]
                        >= self.df["temperature_air_outdoor_min"] - self.get_tolerance("temperature", "outdoor_air")
                    )
                    & (
                        self.df["temperature_air_outdoor"]
                        <= self.df["temperature_air_outdoor_max"] + self.get_tolerance("temperature", "outdoor_air")
                    )
                )
                & (
                    (
                        self.df["temperature_water_chilled"]
                        >= self.df["temperature_water_chilled_setpoint_min"]
                        - self.get_tolerance("temperature", "general")
                    )
                    & (
                        self.df["temperature_water_chilled"]
                        <= self.df["temperature_water_chilled_setpoint_max"]
                        + self.get_tolerance("temperature", "general")
                    )
                )
            )
        )


class HWReset(RuleCheckBase):
    """ASHRAE 90.1-2016 6.5.4.4 Hot-Water Temperature Reset Controls. Per
    sample: no flow; or at/below the low outdoor temperature the HW
    temperature is at/above its maximum setpoint; or at/above the high
    outdoor temperature it is at/below its minimum; or in between it is
    within [min, max]."""

    points = [
        "temperature_air_outdoor",
        "temperature_air_outdoor_max",
        "temperature_air_outdoor_min",
        "temperature_water_hot",
        "flow_mass_water_hot",
        "temperature_water_hot_setpoint_max",
        "temperature_water_hot_setpoint_min",
    ]

    def verify(self):
        self.result = (
            (self.df["flow_mass_water_hot"] <= self.get_tolerance("waterflow", "hot"))
            | (
                (
                    self.df["temperature_air_outdoor"]
                    <= self.df["temperature_air_outdoor_min"] + self.get_tolerance("temperature", "outdoor_air")
                )
                & (
                    self.df["temperature_water_hot"]
                    >= self.df["temperature_water_hot_setpoint_max"] - self.get_tolerance("temperature", "general")
                )
            )
            | (
                (
                    self.df["temperature_air_outdoor"]
                    >= (self.df["temperature_air_outdoor_max"] - self.get_tolerance("temperature", "outdoor_air"))
                )
                & (
                    self.df["temperature_water_hot"]
                    <= self.df["temperature_water_hot_setpoint_min"] + self.get_tolerance("temperature", "general")
                )
            )
            | (
                (
                    (
                        self.df["temperature_air_outdoor"]
                        >= self.df["temperature_air_outdoor_min"] - self.get_tolerance("temperature", "outdoor_air")
                    )
                    & (
                        self.df["temperature_air_outdoor"]
                        <= self.df["temperature_air_outdoor_max"] + self.get_tolerance("temperature", "outdoor_air")
                    )
                )
                & (
                    (
                        self.df["temperature_water_hot"]
                        >= self.df["temperature_water_hot_setpoint_min"] - self.get_tolerance("temperature", "general")
                    )
                    & (
                        self.df["temperature_water_hot"]
                        <= self.df["temperature_water_hot_setpoint_max"] + self.get_tolerance("temperature", "general")
                    )
                )
            )
        )
