"""
Proof that the storage layer doesn't need to know which ontology it's
speaking: load Brick's real published ontology into the live Oxigraph
store (the same `RemoteStore.load_ontology` every daemon has available but
none call by default - see architecture.mdx), add one building's instance
data, query it with real SPARQL. Swap in a different ontology file and the
same loader/query code works unchanged - that's the "pick at load time"
idea made concrete.

Needs a running Oxigraph (`docker compose up -d oxigraph`, or the full
stack). Uses the vendored `ontology/Brick-only.ttl` by default - no
external download required.
"""

import os
import sys

from rdflib import Namespace

from baler.remote_store import RemoteStore

BRICK = Namespace("https://brickschema.org/schema/Brick#")
BLDG = Namespace("urn:mybuilding#")

brick_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "ontology", "Brick-only.ttl")

store = RemoteStore()
store.load_ontology(brick_path)
print(f"loaded {brick_path!r} into Oxigraph's {store.ONTOLOGY_GRAPH!r} graph")

store.add_entity(BLDG.chiller_1, BRICK.Chiller)
store.add_entity(BLDG.ahu_1, BRICK.AHU)
store.add_relationship(BLDG.chiller_1, BRICK.feeds, BLDG.ahu_1)

rows = store.query(f"""
    PREFIX brick: <{BRICK}>
    PREFIX bldg: <{BLDG}>
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    SELECT ?fed ?cls WHERE {{
        bldg:chiller_1 brick:feeds ?fed .
        ?fed a ?cls .
        ?cls rdfs:subClassOf* brick:Equipment .
    }}
""")

for row in rows:
    print(f"chiller_1 feeds -> {row.fed} (a {row.cls}, subclass of brick:Equipment per the loaded ontology)")
