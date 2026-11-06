"""
A vendored subset of PNNL ConStrain 0.8.0 (https://github.com/pnnl/ConStrain,
PyPI `constrain==0.8.0`): the rule-check base class and four verification
items, with plotting, EnergyPlus and workflow code left out. Why vendored
rather than a dependency: todo/cx3-constrain-gl36-spike.md (in short,
`constrain` pins brickschema==0.7.1 -> rdflib 6 and pyshacl 0.22, which
can't co-install with Baler's rdflib>=7 / pyshacl>=0.30, and it drags in
PyQt6, scikit-learn, eppy and a 173 MB demo tree).

The verify() bodies are upstream's, unchanged, so a result here means what
it means in ConStrain. Only pandas is needed (the `cx-gl36` extra).

Upstream licence, which applies to the files in this package:

BSD 2-Clause License

Copyright (c) 2021, Battelle Memorial Institute

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
this list of conditions and the following disclaimer in the documentation
and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

UPSTREAM = "constrain 0.8.0"

# Upstream's constrain/tolerances.json, the entries these items read.
# ConStrain's own workflow loads that file; CheckLibBase falls back to 0.0
# for anything absent.
TOLERANCES = {
    "temperature": {"unit": "deg. C", "types": {"supply_air": 0.5, "zone": 0.5, "outdoor_air": 0.5,
                                                "discharge_air": 0.5, "return_air": 0.5, "general": 0.5}},
    "waterflow": {"unit": "m3/s", "types": {"hot": 0.0, "chilled": 0.0, "general": 0.0}},
    "ratio": {"unit": "%", "types": {"flow": 0.05, "efficiency": 0.02, "occupancy": 0.05, "operation": 0.0,
                                     "temperature": 0.01, "general": 0.05}},
}
