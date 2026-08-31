# Security audit — lco-instrument-web (August 2026)

Scope: the SPA, its static server, the Cloudflare Access deployment
tooling, and the instrument WebSocket contract the SPA depends on.
Findings are ordered worst-first. Everything below was verified by
reading the code or running it; the two items that could not be
verified without a live deployment are labelled as such.

**Bottom line.** The application code is well built — no XSS, no
prototype pollution, no injected markup, and the instrument WebSocket
servers correctly bind to loopback. The exposure is elsewhere, in two
places:

1. **Credentials on disk are being served over HTTP.** A live
   Cloudflare Access API token and the Google OAuth client secret are
   fetchable from the static server. This turns "any Carnegie account"
   into "administrator of every telescope's access policy."
2. **Nothing authenticates the instrument WebSocket.** The client
   trusts whatever the peer says, and a crafted link can point that
   client at an attacker's server from the real, Access-protected page.

---

# Critical

## C1 — The static server publishes `.secrets`, `.git/`, and editor history

`server.py` uses `SimpleHTTPRequestHandler`, which serves **every file
under the served directory** — dotfiles included — with directory
listings on. The served directory is the repository root.

Verified live against a running `python3 server.py`:

| Request | Result |
|---|---|
| `GET /.secrets` | **200**, 187 bytes — `CLOUDFLARE_API_TOKEN` plus the Google OAuth client ID and secret |
| `GET /.git/config` | **200**, 832 bytes |
| `GET /.git/` | **200**, browsable — `objects/`, `refs/`, `logs/` reachable, i.e. full source and history |
| `GET /.history/` | **200**, listing six more copies of `.secrets` |
| `GET /.venv/…`, `/uv.lock`, `/deploy/access-policies.yml` | **200** |

There are two independent routes to those URLs:

1. **Through the tunnel.** The documented ingress
   ([docs/deploy-cloudflare.md](deploy-cloudflare.md)) forwards
   everything that is not `^/<app>/ws$` to `localhost:8080`. So
   `https://<telescope>.chimera.observer/.secrets` returns the token to
   anyone who clears Cloudflare Access — every `@carnegiescience.edu`
   address, plus `hreggiani@gmail.com` on `sbs`.
2. **Around the tunnel entirely.** [server.py:40](../server.py#L40)
   binds `0.0.0.0`, so `http://<gateway-mac>:8080/.secrets` is readable
   by anyone on the observatory LAN or VPN with **no authentication at
   all**. Cloudflare Access is not in this path.

### Why this ranks first

The exposed `CLOUDFLARE_API_TOKEN` carries *Access: Apps and Policies →
Edit* across the whole account. Whoever holds it can rewrite every
telescope's allow-list — add themselves to Clay, Baade and Swope, or
delete the allow rules and lock the observatory out of its own
instruments. The blast radius is account-wide, not host-wide, and
because the sync script replaces a telescope's entire policy in one
PUT, a single API call is enough.

The same file holds the **Google OAuth client secret** (`GOCSPX-…`) —
the credential for the identity provider guarding every telescope.

### Counter-measures

1. **Rotate both credentials before touching the code.** Roll the
   Cloudflare token (dashboard → Manage Account → Account API Tokens)
   and the Google OAuth client secret (Cloud Console → Credentials →
   the Web application client → add a new secret, then delete the old).
   The code fix stops future exposure; it does nothing about a
   credential that has already been readable.
2. **Serve an allowlist, not a directory.** Deny any path component
   beginning with `.`, disable directory listings, and enumerate the
   files the SPA actually needs. A tested implementation is in
   [Appendix A](#appendix-a--verified-hardened-handler).
3. **Bind to loopback.** Change [server.py:40](../server.py#L40) to
   `127.0.0.1`. `cloudflared` connects over loopback, so the tunnel is
   unaffected and route (2) disappears.
4. **Move `.secrets` out of the served tree.** Keep it in the
   observatory password manager and `export` at sync time — which is
   what [docs/deploy-cloudflare.md](deploy-cloudflare.md) § Creating
   the API token already instructs. The file on disk contradicts the
   documented practice.
5. **`chmod 600`** whatever credential files remain. `.secrets` is
   currently `0644`.

## C2 — Editor local history has copied secrets across the whole workspace

The VSCode Local History extension writes a timestamped copy of every
saved file into `.history/`. Here that produced **six extra copies** of
`.secrets`, each holding a live token, each mode `0644`, each served
over HTTP per C1.

This is not confined to this repository. Across
`/Users/william/workspace`, `.history/` exists in 41 repos and **13
contain secret-bearing files** — roughly 800 files carrying API tokens,
private keys or passwords: `lco-instr-monitor` (284), `sbs-grafana`
(183), `chimera` (104), `lcogt_ocs` (69), `henrietta` (49),
`inventory` (34), `mirmos` (34), and others. A second world-readable
credential file, `/Users/william/workspace/mask/.env`, also surfaced.

`.gitignore` protects git. It does nothing about the filesystem, and
every one of these is one `server.py -d`, one shared directory, or one
backup away from disclosure.

### Counter-measures

1. Set `local-history.exclude` in VSCode settings to cover
   `**/.secrets`, `**/.env*`, `**/*credential*`, `**/*.pem`, `**/*.key`.
2. Purge the existing copies and rotate every credential found in them.
3. Add `.history/` to the global gitignore so no repo can commit it.

---

# High

## H1 — A crafted link on the *real* hostname redirects the control WebSocket to an attacker

[ws.js:31](../ws.js#L31) builds the socket URL by string concatenation,
`` `${scheme}//${host}${wsPath}` ``, with `ws_path` taken unvalidated
from the query string. The comment block above it describes tunnel mode
as "host defaults to the page host," which reads as a guarantee that
`ws_path` can only select a path on the already-authenticated
hostname. It is not one — a `ws_path` starting with `@` turns the page
host into a URL *userinfo* component. Verified with `new URL()`:

| `ws_path` | resulting URL | origin |
|---|---|---|
| `/adc/ws` | `wss://sbs.chimera.observer/adc/ws` | `wss://sbs.chimera.observer` |
| `@evil.example/adc/ws` | `wss://sbs.chimera.observer@evil.example/adc/ws` | **`wss://evil.example`** |

The operator is on the correct domain, on the genuine
Access-authenticated page, and the only tell is `#conn-url` in the
header — grey `.muted` text that begins with the real hostname.

This is the delivery mechanism that makes every spoofing finding below
remotely exploitable with nothing but a phishing link: no MITM, no LAN
access, no credential theft. The `host` parameter has the same weakness
(`host=evil.example/#` → origin `ws://evil.example`), though `host` is
attacker-specifiable by design.

**Counter-measure.** Validate the shape before building the URL, and
let the `URL` parser adjudicate rather than string concatenation:

```js
if (wsPath && !/^\/[A-Za-z0-9._~\-/]*$/.test(wsPath)) throw new Error("invalid ws_path");
if (host && !/^[A-Za-z0-9.\-]+$/.test(host))          throw new Error("invalid host");
```

A CSP `connect-src` restricted to the expected WebSocket origins
(Appendix A) independently neuters this, and is worth having as a
second layer.

## H2 — Every policy sync silently deletes any hardening added in the dashboard

[sync-access-policies.py:106-114](../deploy/sync-access-policies.py#L106-L114)
embeds the full policy list in the application PUT. The dry-run payload
confirms what goes on the wire:

```json
"policies": [{"name": "sbs operators", "decision": "allow",
              "include": [...], "exclude": [], "require": []}]
```

`require: []` is sent literally, every run. Any requirement added in
the dashboard — MFA, device posture, country restriction — is **deleted
by the next sync**, with no warning and no diff. The documentation
mentions that dashboard edits are overwritten, but frames it as a
convenience property. In security terms it is a mechanism that quietly
reverts hardening, and nobody will notice until they look.

**Counter-measure.** Represent requirements in the YAML so they survive
a sync and are reviewable in git: an optional per-telescope `require:`
block (`mfa: true` → `{"auth_method": {"auth_method": "mfa"}}`,
country → `{"geo": {"country_code": "CL"}}`) rendered into the policy
instead of a hardcoded `[]`. Until that exists, treat these
applications as read-only in the dashboard and say so in the runbook.

## H3 — The WebSocket server's stated security model no longer matches the deployment

`Common/WSServer.h` states the v1 envelope plainly:

> Listener binds to loopback only … combined with the **VPN-only
> network model**, this is the v1 security envelope.
> No token / Origin auth in v1; the `requireToken` property is
> reserved for v2 (it intentionally does nothing if set today).

The loopback bind is real and correctly implemented —
`nw_parameters_set_local_only(params, true)` is present in all three
instruments (ADC, DCU, PFS `WSServer.m:219-223`), and the 64 KB frame
cap is a good touch. But the Cloudflare Tunnel deployment
**deliberately bridges that loopback port to the public internet**. The
premise the component rests on — only local processes can reach the
writable command surface — is no longer true, and the sole remaining
control is Cloudflare Access at the edge.

Two consequences:

- **Any local process or user on the instrument Mac can command the
  telescope.** No token, no prompt. Acceptable under the original
  model; worth revisiting now.
- **Cross-site WebSocket hijacking — needs one check against the live
  deployment.** WebSocket handshakes are not subject to the same-origin
  policy and the server validates no `Origin`. If Cloudflare issues
  `CF_Authorization` as `SameSite=None`, a logged-in operator who
  visits a malicious page gives that page the ability to open
  `wss://clay.chimera.observer/adc/ws` *with their session* and issue
  commands. If the cookie is `Lax`, the browser withholds it and the
  attack fails. **Check the cookie attributes** — one `curl -sI`
  against a telescope hostname decides whether this is a non-issue or a
  remote telescope-control vulnerability.

**Counter-measures.** Validate `Origin` in `WSServer` against an
allowlist of telescope hostnames plus `http://localhost:*` for
development, and reject the upgrade otherwise — this closes the
cross-site path regardless of Cloudflare's cookie policy. Then
implement `requireToken` (already declared) so the WS surface
authenticates independently of network position. Finally, correct the
`WSServer.h` comment: a security comment asserting a model the
deployment has abandoned actively misleads the next reader.

## H4 — One idle TCP connection takes the control surface down

[server.py:44](../server.py#L44) uses `HTTPServer`, not
`ThreadingHTTPServer`, and never sets a handler `timeout` (the stdlib
default is `None`). Verified against the real server: opening one
socket, sending the nine bytes `GET / HTT`, and never finishing the
request line makes a legitimate `GET /` time out. Releasing the hog
restored service immediately.

| Condition | `GET /` |
|---|---|
| Baseline | `200` |
| One half-open connection | **timeout after 6 s** |
| Hog released | `200` |

One connection, no data, no rate. Combined with the `0.0.0.0` bind
(C1), anyone on the summit or gateway LAN can wedge the telescope
control surface for every operator **without passing Cloudflare Access
at all**. `request_queue_size` is 5, so a handful of sockets also
exhausts the backlog. This contradicts the repo's own threat model —
[docs/deploy-cloudflare.md](deploy-cloudflare.md) explicitly treats the
LAN as untrusted when it warns against proxying WS across it in
cleartext.

**Counter-measure**, alongside the loopback bind from C1:

```python
from http.server import ThreadingHTTPServer

class NoStoreHandler(SimpleHTTPRequestHandler):
    timeout = 10                      # per-connection read timeout
    ...

srv = ThreadingHTTPServer(("127.0.0.1", args.port), NoStoreHandler)
srv.daemon_threads = True
```

The `127.0.0.1` bind is the load-bearing half — cloudflared connects
over loopback, so the wildcard bind buys nothing and costs this.

## H5 — `xib2ir` is unclaimed on PyPI, so `pip install -e .` is a code-execution vector

[pyproject.toml](../pyproject.toml) declares `xib2ir` as a bare
dependency and redirects it locally with
`[tool.uv.sources] xib2ir = { workspace = true }`. **pip, poetry and
pdm all ignore `[tool.uv.sources]`** — only uv honours it. Verified:
`https://pypi.org/pypi/xib2ir/json` returns **404**. The name is free
for anyone to register.

Today, a developer or CI job running `pip install -e .` instead of
`uv sync` fails loudly. The moment someone registers `xib2ir` on PyPI,
that same command silently installs their package and runs its build
backend as the deploying user — the user who holds
`CLOUDFLARE_API_TOKEN`. The failure mode converts from noisy to silent
without anything in this repo changing.

**Counter-measure.** Drop `xib2ir` from `dependencies` and let uv's
workspace membership install it — `[tool.uv.workspace] members` already
lists `tools/xib2ir`, so nothing else changes and no tool can resolve
the name upstream. Registering the name defensively on PyPI is a
weaker fallback; it protects this project but leaves the pattern in
place.

---

# Medium

## M1 — A hostile WebSocket peer chooses same-origin URLs for credentialed fetches

[window-host.js:235](../window-host.js#L235) takes `msg.app` from the
`hello` frame with no character validation and interpolates it into two
`fetch()` URLs ([:199](../window-host.js#L199),
[:118](../window-host.js#L118)). Verified resolutions:

| `msg.app` | resolved URL |
|---|---|
| `../../../x` | `https://<host>/x/manifest.json` |
| `//evil.example` | `https://<host>/instruments///evil.example/manifest.json` |
| `..%2f..%2fadmin?` | `https://<host>/instruments/..%2f..%2fadmin?/manifest.json` |

The leading `./` prevents this from ever going cross-origin — confirmed.
The real impact is that the peer picks an arbitrary **same-origin** path
for a `fetch()` that, per the default `credentials: "same-origin"`,
carries the operator's Cloudflare Access cookie. Because instruments are
paths on one hostname, "same origin" covers every instrument endpoint on
that telescope. Response bodies are not exfiltrated; the *request* is
the payload against any state-changing GET. Chained with H1, no MITM is
required.

**Counter-measure.** Allowlist at the boundary in `onHello`, and apply
the same test to `tab.layout`:

```js
if (!app || !/^[a-z0-9_-]{1,32}$/.test(app)) return;
```

## M2 — The client cannot distinguish what it displays from what it sends

The highest-consequence category for a control surface, and a design
property rather than one bug. Three independent mechanisms, verified
against live bindings:

**(a) Option labels and wire values are both server-supplied and
independent.** [renderer.js:237-238](../renderer.js#L237-L238) sets
`o.value` from `opt.encoder` and `o.textContent` from `opt.name`, both
out of the same server-pushed array. `$state` resolves to `node.value`
and is sent verbatim. With `instruments/pfs/camera/bindings.yml:271-279`,
a peer publishing `slit_options: [{name: "Slit 1 (safe)", encoder: <anything>}]`
means the operator reads the label, picks it, and `move_slit` carries an
encoder they never saw.

**(b) The server renames the operator's own buttons.**
[renderer.js:665-671](../renderer.js#L665-L671) — `label_path`
overwrites button text from topic data. In
`instruments/dcu/bindings.yml:24-26` the caption, the on/off lamp
colour (`class_map`), and the argument (`$not_state`, which negates the
*server-reported* state) all come from the same untrusted frame. A peer
reporting `lamps[0].on = false` while the lamp is physically on makes
the operator's "turn it off" click emit `on: true`. All three channels
lie consistently; the UI offers no cross-check.

**(c) `enabled_if` interlocks are advisory only.**
[renderer.js:700-724](../renderer.js#L700-L724) evaluates interlocks
purely from server-reported state. `instruments/dcu/bindings.yml:108-113`
mirrors the FFS/MCal brake interlock from `DCUcontroller.m:1064-1069` —
a peer reporting the permissive tuple re-enables the control. Correct
as a UI mirror; it must never be the enforcement point.

**Safety implication:** the client is a pure display of whatever the
peer asserts, with no independent notion of instrument truth. Physical
consequences are mechanism collisions (FFS/MCal), lamps energised
unexpectedly, and wrong optics inserted into the beam. On the LAN path
the transport is plaintext `ws://` with no client auth, so an on-path
attacker gets all of (a)–(c) plus forged `ack` frames confirming
commands that never ran.

**Counter-measures, in priority order:**

1. Enforce every interlock in the Cocoa controller and reject
   out-of-policy commands server-side. Treat `enabled_if` as cosmetic.
2. Use `wss://` on the LAN path too — [ws.js:26-27](../ws.js#L26-L27)
   follows the page protocol and so silently downgrades on HTTP.
3. For irreversible or collision-capable commands, confirm against the
   resolved **wire value**, not the server-supplied label, so (a) and
   (b) become visible.
4. Constrain `label_path`/`options_path` to render alongside a
   binding-declared static name.

## M3 — Calibration executes the server's selection while the operator sees the client's

Selection is sent as **ordinal row indices**
([renderer.js:393-402](../renderer.js#L393-L402)) and
`calibration_execute` carries **no arguments**
(`instruments/pfs/calibration/bindings.yml:46-56`) — it runs whatever
the server currently has selected. The two can diverge, and one path is
not adversarial at all:

1. **Benign.** Clicking rows debounces into `calibration_select`, whose
   rejection is swallowed by `.catch(() => {})`
   ([renderer.js:492](../renderer.js#L492)). If that command errors or
   hits the 10 s ack timeout, the rows stay highlighted while the
   server's selection is stale — and
   [renderer.js:572-576](../renderer.js#L572-L576) then *refuses* to
   re-adopt the server's true selection because the local set is
   non-empty. The operator clicks Execute and the instrument runs a
   different set of exposures than the ones on screen. The failure
   reaches the log pane, but as one line in a scrolling firehose with no
   indication on the control itself.
2. **Adversarial.** Indices are meaningless without agreement on the
   array they index; a peer whose `entries[]` does not match its
   internal list makes the highlighted row read "Flat, 1 s" while index
   3 server-side is "Arc, 600 s".

**Counter-measure.** Send a stable per-row identifier instead of
ordinals, and have `calibration_execute` carry the selection explicitly.
Replace the swallowed `.catch` at
[renderer.js:492](../renderer.js#L492) and `:599` with a handler that
clears the optimistic highlight and marks the control failed — the same
swallow leaves a permanent "moving" spinner for moves that never
started ([renderer.js:610-616](../renderer.js#L610-L616)).

## M4 — Access covers only the apex, not the subdomains its own documentation claims

[access-policies.yml:4-7](../deploy/access-policies.yml#L4-L7) says each
application covers "the apex + every instrument subdomain
(`*.<telescope domain>`)". The code does not:
[sync-access-policies.py:95-96](../deploy/sync-access-policies.py#L95-L96)
sets `"domain": domain` and `"self_hosted_domains": [domain]`, and the
dry-run payload confirms a single entry.

Harmless today, because instruments are paths. It becomes a hole the
moment someone adds a hostname believing — on the strength of that
comment — that Access already covers it. The new hostname would serve
with no authentication whatsoever.

**Counter-measure.** Fix the comment to state that Access covers the
apex only and instruments must remain paths. Prefer this over adding
`f"*.{domain}"`: the path-based design is the deliberate one, forced by
the Universal SSL one-level constraint.

## M5 — The documented deploy order leaves an unauthenticated window

[docs/deploy-cloudflare.md](deploy-cloudflare.md) routes DNS at step 4
and applies the Access policy at step 5. Between them the hostname
resolves and serves — the SPA, and under C1 the `.secrets` file — to
anyone on the internet who knows the name. The doc acknowledges this
but keeps the ordering.

**Counter-measure.** Move the policy sync ahead of DNS routing. Access
applications can be created before the hostname resolves, so there is
no reason to accept the window. An ordering that cannot fail open beats
a caveat that depends on the reader.

## M6 — Missing response headers, and grants broader than the job needs

- `server.py` sends only `Cache-Control`. No CSP, no
  `X-Content-Type-Options`, no `X-Frame-Options`, no `Referrer-Policy`.
  The SPA loads zero external resources, so a strict CSP costs nothing;
  the policy in Appendix A was verified against the running app, and
  its `connect-src` also mitigates H1.
- `*@carnegiescience.edu` grants telescope control to **every**
  employee account, including those with no observing role and any that
  are compromised. A telescope control surface is a reasonable place to
  enumerate operators explicitly.
- `hreggiani@gmail.com`
  ([access-policies.yml:49](../deploy/access-policies.yml#L49)) is a
  personal Gmail address with standing access to `sbs`. It is also
  **uncommitted** in the working tree — an unreviewed access grant, in a
  file whose own documentation says to review every change like code.
  Personal accounts carry weaker assurance and no organisational MFA
  guarantee. Remove it when the current test concludes; the YAML
  comment already says the test telescope should not outlive its test.

## M7 — Widening a policy produces byte-identical console output

[sync-access-policies.py:128](../deploy/sync-access-policies.py#L128)
prints only the rule *count*:

```python
print(f"{name}: updated app {app['id']} ({len(rules)} rule(s))")
```

Changing `"*@carnegiescience.edu"` to `"*@gmail.com"` prints exactly
the same line — `clay: updated app <id> (1 rule(s))`. The regexes
cannot help: `DOMAIN_WILDCARD` accepts any `x.y`, so `*@gmail.com`
becomes a valid `email_domain` rule granting the entire public Gmail
population control of a telescope. `--dry-run` shows the payload, but
it is a separate opt-in run — the real run is the one with no
visibility.

**Counter-measure.** Print the resolved rules on the mutating path, and
add a `trusted_domains:` allowlist in the YAML requiring an explicit
flag for anything outside it:

```python
for r in rules:
    print(f"  + {next(iter(r))}: {next(iter(r.values()))}")
```

## M8 — Validation happens mid-flight, so a bad entry leaves telescopes half-synced

[sync-access-policies.py:160-162](../deploy/sync-access-policies.py#L160-L162)
loops over telescopes, and `include_rules()` calls `sys.exit()` from
*inside* `sync_telescope` — after earlier telescopes have already been
PUT. Verified against a mock API with `clay` (valid) → `baade`
(`will*@…`, rejected) → `swope` (valid): clay was written to the
server, then the script exited. Swope never synced; baade kept its
older policy. An operator tightening all three sees an error and
reasonably concludes nothing was applied. One was. An unhandled
`HTTPError` produces the same shape.

**Counter-measure.** Validate every telescope up front, then mutate —
and have `include_rules` raise `ValueError` for the caller to aggregate
rather than `sys.exit` from a library function:

```python
plans = [(name, tcfg, include_rules(tcfg.get("allowed") or []))
         for name, tcfg in telescopes.items()]
for name, tcfg, rules in plans:
    push(name, tcfg, rules)
```

## M9 — The existing-app lookup cannot detect a truncated list

[sync-access-policies.py:122-123](../deploy/sync-access-policies.py#L122-L123)
lists Access applications and scans the result, but `api()` returns
`out["result"]` without inspecting `result_info`, and sends no
`page`/`per_page`. Cloudflare's v4 list endpoints paginate.

Once the account holds enough applications that a telescope's app falls
off page 1, `existing` is `None`, the create branch fires, and the
script **creates a second Access application for the same hostname** —
reporting `created` instead of `updated`. The original app and its
original allow-list stay in force, so a tightening sync silently
becomes a no-op against the policy actually being enforced.

**Counter-measure.** Paginate, and treat `created` on a domain already
present in `access-policies.yml` as an error requiring an explicit
`--allow-create`.

---

# Low

- **`HTTPError` is unhandled, discarding Cloudflare's actual message.**
  [sync-access-policies.py:50](../deploy/sync-access-policies.py#L50) —
  `urlopen` raises before the `success` check can run, so the
  `{"errors":[{"code":10000,"message":"…"}]}` body is thrown away and
  operators get a stdlib traceback ending in `HTTP Error 403`, with no
  hint that the token is missing a scope. Catch it, surface the body,
  and add `timeout=30` — no API call currently has one.
- **The email regexes accept trailing junk.**
  [sync-access-policies.py:36-37](../deploy/sync-access-policies.py#L36-L37) —
  `[^*@\s]+` excludes only `*`, `@` and whitespace, so
  `*@carnegiescience.edu.`, `*@CARNEGIESCIENCE.EDU`,
  `*@evil.com,good.edu`, a zero-width space, a NUL, and a Cyrillic-`е`
  homoglyph all pass into the Access rule verbatim. The realistic
  impact is **lockout rather than over-grant** — a mangled domain
  matches nobody, and the empty-list guard does not fire because the
  list is non-empty — except for the homoglyph case, where an attacker
  registering the punycode domain gains a standing grant. Anchor on a
  real hostname grammar, ASCII-only, and normalise with
  `.strip().lower().rstrip(".")`. (`*@*.edu`, `will*@x.edu`, `*@.edu`
  and embedded newlines are already correctly rejected.)
- **urllib forwards `Authorization` across a cross-host redirect.**
  `HTTPRedirectHandler.redirect_request` strips only `content-length`
  and `content-type` and reuses the rest of the headers for the new
  URL; unlike `requests`, there is no same-origin check on the auth
  header. TLS verification makes this a non-passive path and the
  Cloudflare API does not redirect in normal operation, so likelihood
  is low — but failing closed is three lines and the token grants
  policy write.
- **`pip install pyyaml` advice bypasses the lockfile.**
  [sync-access-policies.py:32](../deploy/sync-access-policies.py#L32)
  tells operators to run `python3 -m pip install pyyaml` — an unpinned,
  unhashed install into whatever interpreter is on `PATH`, sidestepping
  the hash-pinned `uv.lock` entirely. Point it at `uv sync`.
- **`server.py` discloses its exact Python patch version** via the
  `Server:` header. Override `version_string()`.
- **Unguarded prototype-chain reads on server-controlled keys.**
  [renderer.js:302](../renderer.js#L302) uses `key in label_map`, which
  walks the prototype chain: a peer sending `constructor` writes
  `"function Object() { [native code] }"` into a readout.
  [renderer.js:293](../renderer.js#L293) hits the same chain and then
  throws `InvalidCharacterError`, leaving an indicator with *no* state
  class — a blank lamp instead of a red one. Contained by the `try/catch`
  in `ws.js`, and a peer could produce the same visual with an unmapped
  value, so impact is low. Fix both with
  `Object.prototype.hasOwnProperty.call(map, key)`; the current code
  reads as a validated lookup and is not one. `diagnostic.js:302` has
  the identical shape.
- **Unbounded growth from a noisy or hostile peer.**
  [ws.js:108](../ws.js#L108) stores every `state` frame's topic in a Map
  with no check against the topics advertised in `hello` — the simplest
  client DoS here; drop frames for unadvertised topics. Log replay
  ([diagnostic.js:231-232](../diagnostic.js#L231-L232)) caps the DOM at
  500 entries but iterates the entire server-supplied array, so one
  frame with a million entries freezes the main thread; `slice(-500)`
  first. List rebuilds `JSON.stringify` all rows on every push and store
  the result in a DOM attribute
  ([renderer.js:481,546,551](../renderer.js#L481)) — keep the signature
  in a closure or `WeakMap` and cap rendered rows. Reconnect is a fixed
  2 s with no backoff ([ws.js:151-153](../ws.js#L151-L153)), which
  self-DoSes an already-unhealthy instrument Mac.
- **A non-numeric `port` blanks the whole SPA.**
  [ws.js:29](../ws.js#L29) — `parseInt("abc", 10)` → `NaN` →
  `ws://obs1:NaN/`, which throws `Invalid URL` from the `WebSocket`
  constructor. `connect()` runs at module top level, so the throw aborts
  module evaluation and the page renders empty. Reachable by crafted
  link. Validate 1–65535 and wrap `connect()`.
- **`subscribe(...msg.topics)` spreads a server array into arguments**
  ([diagnostic.js:330](../diagnostic.js#L330)) — a large array raises
  `RangeError`. Caught by `fire()`, so it degrades rather than crashes.
  Pass the array directly.
- **Server-controlled CSS class in the log pane.**
  [diagnostic.js:131](../diagnostic.js#L131) builds
  `log-level-${level}` from unvalidated data reaching `className`. Not
  XSS — `className` is never HTML-parsed — and the peer already controls
  its own rows' visibility, so this is cosmetic. Clamp `level` to the
  known set, which also bounds the string length.

---

# What is clean

Worth recording so future reviews need not re-litigate it:

- **No DOM XSS.** All twelve `innerHTML` sites are `= ""` literals. No
  `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval`,
  `new Function`, `srcdoc`, or `.src`/`.href` assignment from data.
  Every `createElement()` passes a hard-coded tag — never derived from
  `el.kind` or WS data. All WS-derived text lands on `textContent` or
  `.value`. `CSS.escape()` is correctly applied at `renderer.js:613`.
- **No prototype pollution writes.** No `Object.assign`, no
  spread-merge, no `for...in` over parsed JSON, no bracket-assignment
  with a WS-derived key. `ws.js` uses a `Map`, which is immune.
- **`localStorage` is validated, not trusted.** `lco_view` is checked
  against an allowlist; `lco_ui_scale` is coerced, range-checked and
  clamped before reaching `style.setProperty`, so no CSS injection;
  `lco_window_tab__*` is honoured only if it matches a manifest tab id.
- **No secrets in git.** Nothing matching credential patterns appears
  in any tracked file, and none ever entered the history — the
  `.gitignore` entries have held. The remote is SSH with no embedded
  credentials.
- **The loopback bind is real** in all three instrument apps, with a
  deliberate comment explaining why, plus a 64 KB inbound frame cap.
- **No external resource loading** — no CDN scripts, fonts, or
  analytics. Nothing to compromise upstream, and a strict CSP is
  therefore trivial to adopt.
- **Only GET and HEAD are served**; POST, PUT, DELETE, OPTIONS, TRACE
  and PATCH all return 501.
- **XML parsing in `xib2ir` is not exploitable.**
  `parser.py:13` uses stdlib `xml.etree.ElementTree`. Tested on both
  interpreters: XXE via `SYSTEM "file:///…"` raises `ParseError`; an
  external DTD is parsed with no fetch attempted; billion-laughs raises
  `ParseError: undefined entity` in 0.00 s, because ElementTree does
  not expand internal general entities at all — the class is
  structurally unreachable, not merely rate-limited. 5000-deep nesting
  parses without error.
- **No path traversal from a malicious `.xib`.** No XIB-derived string
  reaches any filesystem path; every path is argv-only.
- **No `eval`, `exec`, `pickle`, unsafe YAML, or `subprocess`** across
  all nine tracked Python files. Both YAML call sites use
  `yaml.safe_load`.
- **TLS is verified** on the Cloudflare API calls —
  `verify_mode=CERT_REQUIRED`, `check_hostname=True`, no
  `PYTHONHTTPSVERIFY` override.
- **The API token does not leak into tracebacks.** Verified against a
  mock 403: the traceback contains `HTTP Error 403: Forbidden` and not
  the token, because CPython prints source lines rather than locals and
  the raising frame is the `urlopen` call, not the header construction.
  The token is never in argv, never printed, and never in the dry-run
  payload.
- **Log injection is escaped by the stdlib.**
  `BaseHTTPRequestHandler.log_message` applies `_control_char_table`;
  CRLF-forged request lines, raw ANSI escapes and embedded newlines all
  came out escaped, with no forged log line produced.
- **The dependency tree is minimal and hash-pinned.** One real
  dependency, `pyyaml 6.0.3`, with zero transitive deps, sdist and all
  wheels sha256-pinned in `uv.lock`. No known CVEs (the `FullLoader`
  RCEs were fixed in 5.4, and this code uses `safe_load` regardless).
  No `requirements.txt`, no npm, no vendored JS.
- **Layout JSON and `bindings.yml` are not an injection vector**, since
  they are fetched from the page origin — anyone who can modify them
  already controls the page's JS. Worth knowing they *would* be
  CSS-injection sinks if that ever changed
  ([renderer.js:43-46,193,787-794](../renderer.js#L43-L46) interpolate
  geometry into `style` with no numeric coercion). Keep them
  origin-served.

---

# Suggested order of work

1. **Rotate** the Cloudflare token and the Google OAuth client secret
   (C1). Everything else can wait; this cannot.
2. **Harden `server.py`** — allowlist handler, `127.0.0.1` bind,
   `ThreadingHTTPServer` with a connection timeout (C1, H4). One file,
   and it closes the credential leak and the LAN denial of service
   together. Move `.secrets` out of the repo directory.
3. **Purge and exclude `.history/`** workspace-wide; rotate every
   credential it exposed (C2). This is the largest cleanup and reaches
   well beyond this repo.
4. **Validate `ws_path`/`host` and allowlist `hello.app`** (H1, M1) —
   small, self-contained, and together they close the
   phishing-link-to-attacker-WebSocket route.
5. **Drop `xib2ir` from `dependencies`** (H5) — a one-line change that
   removes a latent code-execution path.
6. **Check the `CF_Authorization` cookie's `SameSite` attribute**, then
   add the `Origin` check in `WSServer` (H3).
7. **Make the sync script fail safe** — validate before mutating, print
   resolved rules, paginate, handle `HTTPError` (M7, M8, M9).
8. **Enforce interlocks server-side** (M2). The largest piece of work
   and the one that matters most for physical safety, so it wants a
   proper design pass rather than a quick patch.
9. The rest, at leisure.

---

# Appendix A — verified hardened handler

Tested against the running repository: every path in the C1 table
returns 404 with an empty body, `..` and `%2e%2e` traversal return 404,
and all twelve SPA assets still return 200.

```python
ALLOWED_TOP = {
    "index.html", "app.html", "app.js", "ws.js", "renderer.js",
    "window-host.js", "diagnostic.js", "style.css", "favicon.svg",
    "generated", "instruments",
}


class AppHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self'; connect-src 'self' ws: wss:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        super().end_headers()

    def list_directory(self, path):
        self.send_error(404, "Not Found")
        return None

    def _permitted(self) -> bool:
        full = Path(self.translate_path(self.path)).resolve()
        if full == ROOT:
            return True
        try:
            rel = full.relative_to(ROOT)
        except ValueError:
            return False                      # escaped the served root
        parts = rel.parts
        if parts[0] not in ALLOWED_TOP:
            return False
        return not any(p.startswith(".") for p in parts)

    def send_head(self):
        if not self._permitted():
            self.send_error(404, "Not Found")
            return None
        return super().send_head()
```

Two notes on the implementation:

- The deny must live in `send_head`, not `translate_path`. Routing
  rejected paths to `os.devnull` returns a **200 with an empty body**,
  because `/dev/null` is a readable file. That variant was tried first
  and failed the test.
- Tighten `connect-src` to the actual telescope origins once the
  hostname is known — `ws: wss:` is permissive here so local
  development keeps working, but a per-deployment origin list is what
  makes the CSP mitigate H1.
