//
// BalerHisExt - redirects Haxall's history read/write to
// Baler's Postgres/Timescale store over HTTP, instead of Folio.
//
// Same shape as haxall/haxall's own default His ext,
// src/core/hxd/fan/HxdHisExt.fan - two methods, `pt.id` for identity,
// just HTTP calls to Baler's ingest_api.py instead of `rt.db.his`.
//

using concurrent
using util
using web
using xeto
using haystack
using hx

**
** BalerHisExt - see IHisExt (hx.pod) for the contract this
** implements. Enable via Axon: `libAdd(["his"])`.
**
** Named "his", not a namespaced "td.his" - RuntimeExts.getByType picks
** the alphabetically-first Ext implementing IHisExt when more than one
** is enabled (HxExts.fan), and the built-in historian ("hx.hxd.his" in
** plain Haxall) is a boot lib that CANNOT be removed via libRemove
** (confirmed live: `hx::CannotRemoveBootLibErr`). Sorting ahead of it
** alphabetically is currently the only way to become the active
** historian in this runtime - a real, load-bearing workaround, not a
** style choice.
**
const class BalerHisExt : ExtObj, IHisExt
{
  ** Settings record - see BalerHisSettings.fan.
  override BalerHisSettings settings() { super.settings }

  ** WebClient with the Bearer token attached, if one is configured -
  ** every route requires auth now that ingest_api sits behind the
  ** gateway (viewer role minimum for reads, operator for writes).
  private WebClient client(Uri uri)
  {
    c := WebClient(uri)
    tok := settings.authToken
    if (!tok.isEmpty) c.reqHeaders["Authorization"] = "Bearer $tok"
    return c
  }

  override Void read(Dict pt, Span? span, Dict? opts, |HisItem| f)
  {
    q := Str:Str[:]
    q["point"] = pt.id.id
    if (span != null)
    {
      q["start"] = (span.start.toJava / 1000).toStr
      q["end"]   = (span.end.toJava / 1000).toStr
    }
    uri := (settings.baseUri.toStr + "history?" + Uri.encodeQuery(q)).toUri
    resStr := client(uri).getStr
    items := (Obj?[])JsonInStream(resStr.in).readJson
    items.each |Obj? raw|
    {
      item := (Str:Obj?)raw
      // item["ts"] is a plain Fantom Float straight off JsonInStream, not
      // a haystack::Number - same trap as the value field below, just on
      // the timestamp instead. No (Number) cast needed here at all.
      tsSecs := (Float)item["ts"]
      ts := DateTime.fromJava((tsSecs * 1000f).toInt, span?.tz ?: TimeZone.cur)
      f(HisItem(ts, fromWireVal(item["value"])))
    }
  }

  override Future write(Dict pt, HisItem[] items, Dict? opts := null)
  {
    id := pt.id.id
    toWrite := items.findAll |item| { item.val !== None.val }
    toDelete := items.findAll |item| { item.val === None.val }

    if (!toWrite.isEmpty)
    {
      body := toWrite.map |item->Obj?| { Str:Obj?["point": id, "value": toWireVal(item.val), "ts": item.ts.toJava / 1000f] }
      json := JsonOutStream.writeJsonToStr(body)
      c := client(settings.baseUri + `ingest`)
      c.reqHeaders["Content-Type"] = "application/json"
      c.postStr(json)
    }

    toDelete.each |item|
    {
      q := Uri.encodeQuery(["point": id, "ts": (item.ts.toJava / 1000f).toStr])
      c := client((settings.baseUri.toStr + "history?" + q).toUri)
      c.reqMethod = "DELETE"
      c.writeReq.readRes
    }

    return Future.makeCompletable.complete(null)
  }

  ** ponytail: xeto::NA.val (a real, storable "not available" value -
  ** distinct from xeto::None.val, which means "delete this item" and
  ** never reaches here) has no equivalent in Baler's plain JSON
  ** value union (number/string/boolean/null). Collapsed to `null` on
  ** write, `null` mapped back to NA on read (below) - a clean round
  ** trip for values this ext itself wrote, but a real value that was
  ** legitimately null (if Baler ever allowed writing one) would
  ** come back as NA instead. Upgrade path: give Baler's wire
  ** envelope a real "NA" sentinel if this distinction matters in practice.
  private static Obj? toWireVal(Obj? val)
  {
    if (val === NA.val) return null
    if (val is Number) return ((Number)val).toFloat
    return val
  }

  private static Obj? fromWireVal(Obj? val)
  {
    if (val == null) return NA.val
    // JsonInStream.readJson returns plain Fantom Float/Int for JSON
    // numbers, not haystack::Number - HisItem.val must be a real Number
    // for numeric values, confirmed live (ClassCastException: Double
    // cannot be cast to Number when this wasn't wrapped).
    if (val is Float) return Number((Float)val)
    if (val is Int) return Number(((Int)val).toFloat)
    return val
  }
}
