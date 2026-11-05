#! /usr/bin/env fan
//
// tdHis - Baler history-provider lib for Haxall/SkySpark.
//

using build

**
** Build: tdHis
**
class Build : BuildPod
{
  new make()
  {
    podName = "tdHis"
    summary = "Baler history provider"
    meta    = ["org.name":     "Baler",
               "proj.name":    "Baler",
               "license.name": "Academic Free License 3.0",
               "vcs.name":     "Git"]
    depends  = ["sys @{fan.depend}",
                "util @{fan.depend}",
                "web @{fan.depend}",
                "concurrent @{fan.depend}",
                "xeto @{hx.depend}",
                "haystack @{hx.depend}",
                "axon @{hx.depend}",
                "hx @{hx.depend}",
                "hxApi @{hx.depend}"]
    srcDirs = [`fan/`]
    resDirs = [`lib/`]
    // "xeto.bindings" only binds this pod to an EXISTING xeto lib
    // definition - it doesn't define one. The lib itself must exist as
    // real Xeto source under src/xeto/his/lib.xeto (see FindPods.fan in
    // xetoGen and SrcLibCmd in xetom - `xeto build -all` only considers
    // pods bound to libs already known to env.repo, i.e. ones with real
    // .xeto source; lib.trio alone, sufficient in pre-4.0 Haxall, is
    // not enough here).
    //
    // ponytail: named "his", not "td.his" - RuntimeExts.getByType picks
    // the FIRST IHisExt-implementing ext alphabetically by name
    // (HxExts.fan: `list.sort |a,b| a.name<=>b.name`), and the boot
    // historian "hx.hxd.his" cannot be removed in the standalone "hxd"
    // runtime (CannotRemoveBootLibErr), so winning the alphabetical
    // sort is currently the only lever to become "the" active
    // historian short of a real upstream override mechanism. Real fix:
    // get Haxall to support explicit His-provider selection - this is
    // a load-bearing workaround, not a design choice, and won't survive
    // a boot lib renamed to sort earlier.
    index   = ["xeto.bindings": "his", "ph.lib": "his"]
  }
}
