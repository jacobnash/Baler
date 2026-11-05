//
// WindrowDispatch - subscribes to Windrow's real MQTT bridge output directly
// (same wire envelope {point, value, ts} mqtt_listener.py consumes) and
// pushes each reading into the matching point's curVal via the real
// connector framework (updateCurOk). From there it's ordinary Haxall:
// hisCollectCov (already configured on the point) notices the curVal
// change and historizes it - through "his" (BalerHisExt), into
// Baler. This class never talks to Baler directly; it only
// makes real BACnet-sourced data look like a normal Haxall point.
//
// ponytail: subscribes once, to whatever points exist at onOpen time -
// points added to this connector afterward won't be picked up until the
// connector reopens. Real connectors handle this via onWatch/onUnwatch;
// skipped here since this is a fixed, known point set for now.
//

using concurrent
using xeto
using haystack
using util
using hx
using hxConn
using mqtt

class WindrowDispatch : ConnDispatch
{
  new make(Obj arg) : super(arg) {}

  private MqttClient? client

  override Void onOpen()
  {
    uriVal := rec["uri"] ?: throw FaultErr("Missing 'uri' tag")
    config := ClientConfig
    {
      it.serverUri = uriVal
      it.version   = MqttVersion.v3_1_1
      it.clientId  = "windrowConn-" + id.toStr.replace("-", "")
    }
    c := MqttClient(config, trace.asLog)
    c.connect(ConnectConfig {}).get(10sec)
    this.client = c

    conn.points.each |ConnPoint pt|
    {
      topic := pt.curAddr as Str
      if (topic == null) return
      c.subscribeWith
        .topicFilter(topic)
        .qos(0)
        .onMessage(|Str t, Message msg| { onWindrowMessage(pt, msg) })
        .send
        .get
    }
  }

  override Void onClose()
  {
    try
      client?.disconnect?.get
    catch (Err ignore) {}
    client = null
  }

  override Dict onPing()
  {
    Etc.dict0
  }

  private Void onWindrowMessage(ConnPoint pt, Message msg)
  {
    try
    {
      str  := msg.payload.in.readAllStr
      data := (Str:Obj?)JsonInStream(str.in).readJson
      val  := toNum(data["value"])
      if (val == null) { pt.updateCurErr(FaultErr("null value")); return }
      pt.updateCurOk(val)
    }
    catch (Err e)
    {
      pt.updateCurErr(e)
    }
  }

  private static Obj? toNum(Obj? val)
  {
    if (val is Float) return Number((Float)val)
    if (val is Int)   return Number(((Int)val).toFloat)
    return val
  }
}
