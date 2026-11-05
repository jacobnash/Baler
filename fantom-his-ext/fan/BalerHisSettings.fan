//
// BalerHisSettings - how BalerHisExt reaches Baler's
// API. Same mechanism haxall/haxall's own hxHttp lib uses for HttpExt/
// HttpSettings (src/core/hx/fan/Settings.fan, `@Setting`-annotated const
// fields, Ext.settings() overridden to return the typed subclass) -
// confirmed against that reference implementation, not guessed.
//

using xeto
using haystack
using hx

const class BalerHisSettings : Settings
{
  new make(Dict d, |This| f) : super(d) { f(this) }

  ** Base URL of the Baler API to read/write history through.
  ** Defaults to the gateway (JWT-enforced) rather than a direct
  ** `ingest_api` address, since every route requires auth now - point
  ** this at `ingest_api` directly only for a deployment where bypassing
  ** the gateway is a deliberate, trusted choice. `host.docker.internal`,
  ** not `localhost` - this Ext runs inside the Haxall container, and
  ** Baler runs on the host (confirmed resolvable under Colima;
  ** most Docker Desktop setups support the same name).
  **
  ** Update via Axon: `extSettingsUpdate("his", {baseUri: \`http://host.docker.internal:8080/ingest/\`})`
  @Setting
  const Uri baseUri := `http://host.docker.internal:8080/ingest/`

  ** Bearer token for an operator-role Baler account
  ** (`POST /auth/login` through the gateway - see the Haxall Drop-In
  ** doc's "Gateway auth" section). Required when `baseUri` points at the
  ** gateway; ignored for a direct `ingest_api` address. Empty by default,
  ** so a misconfigured deployment fails loudly (401 from the gateway) on
  ** the first read/write instead of silently talking to a dead port.
  **
  ** Update via Axon: `extSettingsUpdate("his", {authToken: "eyJhbGci..."})`
  @Setting
  const Str authToken := ""
}
