"""Functions this package publishes. Each one takes a client from
baler_fn.connect() as its first argument, so it runs against whatever
Baler the caller is connected to, with the caller's own API key."""

import pandas as pd

from baler_fn import baler_function


@baler_function(tags=["inventory"])
def point_table(client, filter="point", limit=None):
    """Points matching a Haystack filter, one row each: id, dis, kind, unit, curVal.

    Example: point_table(client, "temp and sensor")
    """
    rows = client.read(filter, limit=limit)
    cols = ["id", "dis", "kind", "unit", "curVal"]
    return pd.DataFrame([{c: r.get(c) for c in cols} for r in rows], columns=cols)


@baler_function(tags=["history", "stats"])
def his_stats(client, point_id, range="today"):
    """Count, mean, min, max, first and last sample of one point over a Haystack range.

    Example: his_stats(client, "urn:point:...", "yesterday")
    """
    df = client.his_read_df(point_id, range)
    vals = pd.to_numeric(df["val"], errors="coerce").dropna()
    if vals.empty:
        return {"count": 0, "mean": None, "min": None, "max": None, "first": None, "last": None}
    return {
        "count": int(vals.size),
        "mean": float(vals.mean()),
        "min": float(vals.min()),
        "max": float(vals.max()),
        "first": vals.index[0].isoformat(),
        "last": vals.index[-1].isoformat(),
    }


@baler_function(tags=["history", "stats"])
def hourly_profile(client, point_id, range="today", tz="UTC"):
    """Mean, min and max of a point for each hour of the day, in the timezone tz.

    Example: hourly_profile(client, "urn:point:...", "2026-11-01,2026-11-07", tz="America/Chicago")
    """
    df = client.his_read_df(point_id, range)
    vals = pd.to_numeric(df["val"], errors="coerce").dropna()
    if vals.empty:
        return pd.DataFrame(columns=["mean", "min", "max"])
    local = vals.tz_convert(tz)
    return local.groupby(local.index.hour).agg(["mean", "min", "max"]).rename_axis("hour")
