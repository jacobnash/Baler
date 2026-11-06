# Vendored from PNNL ConStrain 0.8.0, constrain/checklib.py.
# Copyright (c) 2021, Battelle Memorial Institute. BSD 2-Clause License,
# full text in this package's __init__.py.
#
# Kept: CheckLibBase.__init__/get_tolerance and RuleCheckBase's verdicts,
# as upstream wrote them. Dropped: plotting (matplotlib/seaborn), markdown
# reports, the EnergyPlus-only compliance classes, and module-level logging
# configuration. Upstream's logging of every result dict at INFO is kept at
# DEBUG here.
# ruff: noqa: RUF012, SIM118 - upstream code kept as written (vendored)

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import pandas as pd

log = logging.getLogger(__name__)


class CheckLibBase(ABC):
    """Abstract class defining interfaces for item-specific verification classes"""

    points: list[str] = []

    def __init__(self, df: pd.DataFrame, params=None, results_folder=None, tolerances=None):
        self.result = pd.DataFrame()
        self.points_list = self.points
        full_df = df.copy(deep=True)
        if params is not None:
            for k, v in params.items():
                full_df[k] = v

        col_list = full_df.columns.values.tolist()
        if not set(self.points_list).issubset(set(col_list)):
            log.warning("Dataset is not sufficient for running %s", self.__class__.__name__)
            log.warning("%s", set(col_list))
        self.df = full_df[self.points_list]
        self.df.index = pd.to_datetime(self.df.index)
        self.df = self.df.sort_index()
        self.results_folder = results_folder
        self.tolerances = tolerances
        self.verify()
        self.result.name = ""
        self.df["Verification Result"] = self.result

    @abstractmethod
    def check_bool(self):
        """implementation of the checking boolean return"""

    @abstractmethod
    def check_detail(self) -> dict:
        """implementation of the checking detailed return in Dict"""

    @abstractmethod
    def verify(self):
        """checking logic implementation, not for user"""

    @property
    def get_checks(self):
        return self.check_bool(), self.check_detail()

    def get_tolerance(self, variable_type: str | None = None, variable_subtype: str = "general") -> float:
        if self.tolerances is not None:
            if variable_type in self.tolerances.keys():
                return self.tolerances[variable_type]["types"][variable_subtype]
            else:
                return 0.0
        else:
            return 0.0


class RuleCheckBase(CheckLibBase):
    def check_bool(self):
        if len(self.result[self.result == False] > 0):
            return False
        elif len(self.result[self.result == True] > 0):
            return True
        else:
            return "Untested"

    def check_detail(self) -> dict:
        output = {
            "Sample #": len(self.result),
            "Pass #": len(self.result[self.result == True]),
            "Fail #": len(self.result[self.result == False]),
            "Untested #": len(self.result[self.result == "Untested"]),
            "Verification Passed?": self.check_bool(),
        }
        log.debug("Verification results dict: %s", output)
        return output
