#! /usr/bin/env fan
//
// windrowConn - real Haxall connector for Windrow's BACnet/Modbus-to-MQTT bridge.
//

using build

class Build : BuildPod
{
  new make()
  {
    podName = "windrowConn"
    summary = "Windrow bridge connector"
    meta    = ["org.name":     "Baler",
               "proj.name":    "Baler",
               "license.name": "Academic Free License 3.0",
               "vcs.name":     "Git"]
    depends  = ["sys @{fan.depend}",
                "util @{fan.depend}",
                "concurrent @{fan.depend}",
                "xeto @{hx.depend}",
                "haystack @{hx.depend}",
                "axon @{hx.depend}",
                "hx @{hx.depend}",
                "hxConn @{hx.depend}",
                "mqtt @{hx.depend}"]
    srcDirs = [`fan/`]
    resDirs = [`lib/`]
    index   = ["xeto.bindings": "windrow", "ph.lib": "windrow"]
  }
}
