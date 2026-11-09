"""
The building's lifecycle record (UBIS, epic Baler#221): positions and the
assets installed in them, documents, maintenance. Design:
todo/building-lifecycle-ubis.md.

Each feature is one module here that registers its tables with
`lifecycle.rows` and its HTTP routes with `lifecycle.routes`. ops_api
serves every route under /ops/ (see `lifecycle.http`), so a feature adds
no new service, port or gateway location. `routes.load()` imports every
module in this package, so a new module needs no list edited to be served.
"""
