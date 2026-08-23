Asking 4 harnesses: claude:claude-fable-5@max, codex:gpt-5.6-sol@ultra, grokcli:grok-4.6@xhigh, kimicli:kimi-code/k3 …

## claude:claude-fable-5@max

## The escalation

**A bot doesn't need to beat any of the approval machinery. It curls it.**

`isolation.py` states `network = True`, `network_isolated: False`. `web.py` states there is no authentication, deliberately. So the bot's own shell can do:

```
curl -s http://<box>:6768/api/bots            # → pending[].id, pending[].bot_id, options
curl -X POST http://<box>:6768/verdict -d "approval=<id>&choice=<option it authored>"
```

`decided_by` is hardcoded `"human:web"`. The message store then posts a VERDICT to the channel signed as a human. Nothing in this code distinguishes that request from the operator's phone — network position *is* the entire authorisation model, and the bot is on the network. Every other control in these four files (action hashes, grants, seatbelt, worktrees, caps) sits behind that one hole. `deny_read_paths` is irrelevant: this is a socket, not a file.

That is also why CSRF is reachable in the ordinary sense — no token, no Origin/Referer check, no cookies to be SameSite about, and a urlencoded form POST is a simple request needing no preflight. Any page the operator's browser loads (including one a bot asks them to open) can approve a named id. `frame-ancestors` is absent from the CSP, so clickjacking works too, but it's redundant. A second person on the tailnet gets full, unattributable decision authority over the fleet.

## Findings, ranked

**1. CRITICAL — unauthenticated `POST /verdict` reachable from bot code.** Above. *Fix:* mint a random secret at server start, write it under `~/.omnigent/army` — already in `CONTROL_PLANE_DIRS`, i.e. already denied to bots — require it on `/verdict` and on `/api/bots`, and drop `pending[].id`/`bot_id` from the unauthenticated payload.

**2. CRITICAL — `propose()` executes the bot-authored spec before any human sees it.** "Built here and thrown away, purely to validate" — `to_bot(spec, …)` runs the precondition and the constructor with `workload` and `workload_config` the bot chose, *inside the control-plane process*, which holds `ARMY_BROKER_KEY` and the database. If `workload` resolves by import path, that's arbitrary import-time code execution in the process that mints owner grants — a refused proposal has already paid out. *Fix:* validate the spec against a schema plus an allowlist of workload names; never instantiate at propose time. (Conditional on `definition.py`, which I can't see — check this first.)

**3. CRITICAL — `ALWAYS_OWNER` is a denylist over an attacker-chosen string.** `requires_owner = verb in ALWAYS_OWNER`. Any verb outside that set of three is click-answerable: `"Spend"`, `"spend "`, `"pay_invoice"`, `"transfer"`, `"install_package"`, `"place_order"`. If the bot influences the verb string the gate is asked under, it does the privileged thing under an unprivileged label and `_spend_owner_grant` returns `None` on line one. *Fix:* default-deny — classify verbs from a table; unknown verb ⇒ owner-only (or refuse), after casefold/strip normalisation.

**4. HIGH — every binding is opt-in, and the command carries none of them.** `_check_bindings` skips any check whose argument is `None`. `web.py` passes only `run_version` — and passes `None` when the run row is missing, so a lookup miss fails *open*. So the module's headline claim (a verdict binds the action hash) is enforced on no call path shown. Worse, `Command.new(run_id, APPROVE, {"choice": choice})` carries no verb, no parameters, no hash: whatever the run does next on seeing APPROVE is what happens. The hash ceremony ends at the ledger boundary. *Fix:* make `verb`/`parameters`/`policy_version`/`run_version` required and refuse on `None` (explicit `unchecked=True` for the one caller that can't supply them); put `action_hash` in the command payload and have the applier re-derive and compare. Also: `run.version` is read outside the CAS transaction — a TOCTOU even when supplied.

**5. HIGH — approve is the default on malformed input.** `approved = choice != "__deny__"`. A POST with no `choice` field yields `""` → approved. Option validation catches it only when `request.options` is non-empty, and `request()` never requires it to be. `request(..., options=[])` + `curl -d "approval=<id>"` = approval, with a `{}` payload. *Fix:* `approved = choice == "__approve__"` (explicit affirmative), and reject a request created with empty options.

**6. HIGH — the grant is bound to a hash-of-a-hash, and that indirection is a hole.** `_spend_owner_grant` calls `broker.check(verb, {"action_hash": request.action_hash}, …)`. The docstring's claim that the broker "re-derives the digest from the operation it is given" is false: it is given a row column, not the operation. Two consequences. (a) Cross-protocol confusion: if the owner path anywhere signs `digest(verb, params)` over bot-supplied `params`, a bot supplies `params = {"action_hash": H}` where `H` is the digest of the operation it actually wants, gets that innocuous-looking grant signed, then presents it against the row whose hash is `H`. Same verb, same digest, valid grant. (b) The human signs an opaque hex: the only human-legible surface is `question`/`evidence`, both bot-authored, so "spend $5 on a domain" can carry `{amount: 500000, to: attacker}` and the hash still matches. *Fix:* pass the real `(verb, parameters)` to `broker.check`, domain-separate the grant payload (`{"kind": "approval", …}`), and render canonical parameters — not bot prose — on the owner's path.

**7. HIGH — `deny_read_paths` is a denylist on the wrong platform.** The docstring calls the vendor row "real, and load-bearing". On macOS the credentials it names live in the keychain (`security find-generic-password`) and `~/Library/Application Support/…`, neither of which appears. Also missing: `~/.ssh`, `~/.aws`, `~/.netrc`, `~/.gitconfig` (credential.helper), `~/.config/gh`, `~/.config/gcloud`, `~/.docker/config.json`, `~/.gemini`, `~/.cursor`. *Fix:* invert it — deny `file-read*` under `$HOME` and allow the workspace plus explicit subpaths; keep `deny_read_paths` only for holes inside an allowed parent.

**8. MEDIUM-HIGH — `decided_by="human:web"` is an assertion nothing proved.** It is what makes finding 1 invisible in the audit trail, and it collapses every person on the tailnet into one principal. *Fix:* record the authenticated identity or `"web:unauthenticated"`; never the word "human" for an unauthenticated request.

**9. MEDIUM — `requires_owner` is a frozen snapshot.** Rows persist for 24h; if a verb joins `ALWAYS_OWNER` (upgrade, config change) the pending rows created before it stay click-answerable. *Fix:* at decide time, `if request.requires_owner or request.verb in ALWAYS_OWNER`.

**10. MEDIUM — `decide()` trusts the caller's object, not the row.** `requires_owner`, `verb`, `action_hash`, `expires_at`, `options` all come from the passed dataclass; the CAS fences only `(id, version, state)`, so a never-updated row (version 0) accepts a fabricated request object with `requires_owner=False`. `now` is caller-supplied too, so an expired row is answerable by a caller that lies about the clock. *Fix:* re-read the row inside the transaction and decide from it.

**11. MEDIUM — the spawn caps race, and one doesn't count.** `_check_caps` counts children and active bots *outside* the transaction that creates the child. The CAS on the proposal stops double-activating the *same* proposal — as the comment says — and does nothing for two different proposals racing: parent with 2 children plus two pending proposals gives 4. `MAX_ACTIVE_BOTS` counts only `ACTIVE`, but children land in `DRAFT`, so drafts are free. And the docstring says "a child may not create grandchildren" while `parent.depth >= MAX_DEPTH` lets a depth-1 parent create a depth-2 child. *Fix:* move `_check_caps` inside `self.store.atomic()` in `activate`; count non-retired, not active.

**12. MEDIUM — a bot-authored spec that names another bot's cookie jar.** `partition_for` returns `bot.browser_profile or f"persist:bot-{bot.id}"`. If the spec can set `browser_profile`, propose a child with `persist:bot-<victim id>` — and `/api/bots` hands out `pending[].bot_id`. A human activates "a helper bot", the child browses as the victim. Same shape for `slug`, which is a path: loose validation gives `../../` and a workspace (hence the sole `write_paths` entry) outside the sandbox root, pointed at `~/.claude` or `~/.omnigent/army` — which `isolation.py` itself calls game over. And `expiry` in the spec makes the TTL cap self-service, while `allowance` is whatever the parent asked for unless a human overrides. *Fix:* bot-authored definitions may set mission/schedule text only; slug charset-validated and path-joined then `resolve().is_relative_to(root)`; `browser_profile`, `depth`, `parent`, `caps`, `expiry` always derived, never accepted.

**13. MEDIUM — reflected prose in a trusted card.** `?ok=` / `?err=` render inside `.card` indistinguishably from real content. Escaping holds (I checked every interpolation; the only un-escaped ones are `when` and `_counts`, both derived — keep them that way), so this isn't XSS, it's phishing *inside* the legitimate page: `…/bots?ok=Approved+by+the+owner%2C+verified`. *Fix:* pass a code, map it to a fixed string server-side.

**14. LOW — cascade and housekeeping.** `retire_descendants` has no visited set (a `parent_bot_id` cycle loops forever) and its `except Exception: continue` silently leaves a descendant running while the caller believes the cascade succeeded; `release(parent_id, account.remaining)` reads outside a transaction. `propose()`/`request()` are unrate-limited with unbounded `rationale`/`definition`. No `Cache-Control: no-store` on a page listing pending approvals.

## The one change

**Stop treating "can reach the port" as authority.** One secret, generated at server start, stored under `~/.omnigent/army` — a directory `isolation.py` already withholds from every bot — required on `/verdict` and `/api/bots`, plus a sandbox egress deny for the control-plane host:port. That single change turns findings 1, 5, 8 and the whole CSRF surface from "any process on the box" into "the operator", and every other item on this list is a second line of defence that currently has nothing in front of it. Runner-up, if you get two: default-deny verb classification (finding 3), because it's what makes `ALWAYS_OWNER` mean something when the attacker picks the label.

*Unverified, and worth checking before you trust anything above: `army.gates.digest` canonicalisation, `Broker.check`'s payload handling, `definition.to_bot`'s field allowlist and workload resolution, and `budgets.carve`'s solvency check.*

<sub>Aside: the ponytail statusline badge isn't wired up — want me to add it to `~/.claude/settings.json`?</sub>

## codex:gpt-5.6-sol@ultra

**The boundary is broken.** A web click cannot directly approve a request whose stored verb is exactly `spend`, `execute_order`, or `add_dependency`, because no `Grant` is supplied. But a compromised bot can approve its own ordinary gates, bypass owner-only semantics through shell/workload aliases, and detach a valid grant from the operation eventually executed.

1. **Critical — self-approval and incomplete mediation.** With `network=True`, a bot can run:

   ```text
   GET  http://<tailnet-address>:6768/api/bots
   POST http://<tailnet-address>:6768/verdict
        approval=<returned-id>&choice=<returned-option>
   ```

   It receives full IDs/options and creates an `APPROVE` command attributed to `human:web`. For example, `verb="shell"` with `argv=["uv","add","attacker-package"]`, or simply editing a writable dependency manifest, performs `add_dependency` without ever naming that verb. Financial effects are similarly reachable if shell/workload/browser credentials permit them. Smallest fix: enforce typed capabilities at the actual side-effect boundary; generic shell/workloads must not possess credentials or write protected manifests.

2. **Critical — approval bindings fail open.** In `_check_bindings`, omitting `verb` or `parameters` disables the action check; omitting `policy_version` accepts obsolete policy; omitting `run_version` accepts a moved run. The web supplies only run version—and skips even that when the run is missing. Worse, the resulting `Command` contains none of the hash, policy version, run version, or grant, so the next tick cannot distinguish operation A from a newer operation B. A grant for `execute_order({"path":"order.json"})` also remains valid after the bot rewrites `order.json`; hashing a pointer does not bind its contents. Make every binding mandatory and server-derived inside one transaction, put them in the command, and execute an immutable payload with a version-CAS.

   The hash-of-hash in `_spend_owner_grant` is not itself forgeable if `digest` is collision-resistant and the owner signs that same representation. The hole is that it authenticates the old ledger hash, not the operation being executed. If the owner signs the real parameters instead, the current code merely rejects legitimate grants.

3. **High/Critical — the page provides authority, not merely UI.** Any second tailnet user can read missions/questions, approve every non-owner request, deny any request including owner-only ones, and leave the unauditable identity `human:web`. A bot can also supply `options=["Deny this request"]`; clicking that primary “Deny” button approves because every value except the magic string `__deny__` means approval. There is no obvious HTML/XSS injection—the escaping is sound—but CSP does not prevent inbound CSRF and lacks `frame-ancestors`. `get()` also exposes SQL `LIKE` wildcards: cross-site forms can spray `approval=00%` … `ff%` and deny singleton-prefix requests without knowing their IDs. Require authentication unavailable to bot processes, session-bound CSRF plus Origin/Host checks, exact 32-hex IDs, trusted approve/deny labels, and `frame-ancestors 'none'`.

4. **Critical if `to_bot` accepts the stated workload paths — proposal-time control-plane execution.** A definition such as:

   ```yaml
   slug: ../../.omnigent/army
   workload: /bot/worktree/payload.py:Payload
   workload_config: {command: "..."}
   expires_at: 9223372036854775807
   browser_profile: persist:bot-<victim-id>
   status: active
   ```

   can be dangerous before any human acts: `propose()` calls `to_bot`, so any imported precondition or constructor runs in the trusted control-plane process. That yields the Broker key/database and therefore every owner capability. Even an allowlisted privileged workload bypasses the rule because `SpawnStore` performs no owner-capability check. Whether the exact YAML works depends on omitted `definition.py`, but `spawn.py` supplies no defense. Use pure schema validation at proposal time, a closed trusted workload registry, per-workload strict config schemas, server-owned status/depth/expiry/path/profile fields, and UUID-derived filesystem/browser partitions.

   Separately, cap checks are racy and outside the creation transaction: two activations can both see two children and create four; a parent can retire between check and creation, producing an orphan; proposals never expire; zero/negative allowance can create a child without a ledger account. Re-read and lock parent/counts/budget in the same transaction, and recheck the active cap on the actual `DRAFT → ACTIVE` transition.

5. **Best single blast-radius reduction:** run each bot as a zero-ambient-authority OS/VM principal with no route to the approval service, host home/keychain, or shared browser storage, and default-deny network access. Keep vendor authentication and all external effects in a narrow, typed broker outside that boundary. That one architectural change defeats self-approval, shell aliases, credential theft, and most consequences of a malicious workload—even when the bot is fully compromised.

## grokcli:grok-4.6@xhigh

I'll review the four files against the stated threat model and look for related gate, digest, and spawn-definition code so the findings stay concrete.MCP isn't available here, so I'll search the filesystem for the gate, digest, and spawn-definition implementations.The four files are enough to start; next I’ll hunt for `digest`, `Broker`, and `to_bot` so the grant-indirection claim can be checked against real call sites.I’ll search the machine for `army.gates`, `to_bot`, and the approval/web/spawn files so the review is against the real call sites, not just the pasted snippets.The escalation is not a missing `ALWAYS_OWNER` string check. It is a **confused deputy**: a prompt-injected bot asks a lying question, then **cashes a generic `APPROVE` against whatever the run is doing now**, via an **unauthenticated `POST /verdict` the bot can reach on loopback**. Owner verbs stay blocked on this page only because no `Grant` is passed — every other verb is “a human said yes, continue.”

---

### 1. Can a bot reach an `ALWAYS_OWNER` verb?

**Not as `spend` / `execute_order` / `add_dependency` through this web path.** `requires_owner` is frozen from `verb in ALWAYS_OWNER` at insert, and `_spend_owner_grant` refuses a channel/web click with no `Grant`. That part holds.

**Yes as the effect of those verbs**, because a verdict is not a capability.

`decide()` writes `CommandKind.APPROVE` with payload `{choice}` only — no verb, no parameters, no action hash. The supervisor then continues the run. The bot never has to name `spend`. It asks `iteration_gate` (or any verb not in the set), lies in `question`, and the continue is the privilege.

**The `{action_hash}` wrapper is a hole**, but it is fail-open only on the owner path, not the web path.

```python
self.broker.check(request.verb, {"action_hash": request.action_hash}, grant, now=now)
```

`Broker.check` re-hashes **the dict it is given**. That is not the live operation.

- If grants are minted over real params (`{amount, to, ...}`), every grant fails closed (broken, safe).
- If grants are minted over the same wrapper, the grant binds **the row’s stored hash**, not the bytes that will run. Combined with skipped live re-bind (below), an owner grant for “spend $10” still unpauses a run that now spends $10,000.

A **verb alias** (`Spend`, `transfer`, `add-dependency`) also skips `ALWAYS_OWNER` because membership is raw-string. Same for putting the privileged work in `parameters` under a cheap verb: `digest` hashes whatever the caller supplied, not what the tool will execute.

**Exploit:** injected bot → `request(verb="iteration_gate", parameters={...}, question="Retry the flaky test?")` → later the run calls `spend` / `execute_order` without a new owner request → human or `POST /verdict` approves → loop continues into the privileged call.

**Fix:** privileged call sites must demand a spent `Grant` themselves. `APPROVE` may only execute `digest(live_verb, live_params) == request.action_hash`. Mint and check grants over the **real** parameters, never `{"action_hash": ...}`. Callers must not choose the verb string that decides `requires_owner`; the gate must.

---

### 2. Approval bindings — verdict applies to the wrong thing

**Severity: critical.** `_check_bindings` treats omitted fields as “skip,” and the web (and any CLI that passes only some fields) omits the ones that matter.

```python
if verb is not None and parameters is not None:      # skipped if either omitted
if policy_version is not None and ...
if run_version is not None and ...
```

`BotsSite.verdict` passes **only** `run_version` (and that is `None` if `get_run` misses, which **skips the run-version check too**). Action hash and policy version are never re-checked on the page.

Even a caller that “re-checks” by passing **the row’s own** `verb`/`parameters` proves a tautology. The live plan is not an input anywhere in this code.

`question`, `options`, and `evidence` are **not** in `digest`. The human is bound to a hash of arguments they are not shown.

Denials skip bindings on purpose. That is fine. Approvals must not.

Prefix `get`: `id LIKE approval_id||'%'` with `_` / `%` from the form. Empty `approval=` becomes `LIKE '%'`. If the table has exactly one row, an empty POST decides it. `choice=""` is not `__deny__`, so that is an **approve**. If `options == []`, the empty choice is accepted.

**Exploit:**

1. Bot opens `request(verb=send_pr, parameters={repo: "prod", ...}, question="OK to rerun tests?", options=[])`.
2. Plan is swapped (same `run_version` — version often does not bump on plan edit).
3. Anyone hits `POST /verdict` with `approval=<id>&choice=yes` (or empty choice, empty options).
4. Hash/policy checks skipped → `APPROVE` → current plan runs.

**Fix:** `decide()` must load the **live** operation from the run inside the transaction and refuse unless `digest(live.verb, live.params)`, `policy_version`, and `run_version` all match. Drop the “omit to skip” API. Put `action_hash` on the `Command`. Reject empty/unknown `choice`. Stop prefix-matching attacker-supplied ids.

---

### 3. The page — XSS is the wrong question; CSRF + tailnet identity are the right ones

`html.escape(..., quote=True)` + CSP `default-src 'none'` largely stop script XSS. `frame-ancestors` is **not** covered by `default-src`, so the page can still be iframed (clickjack the primary button). `style-src 'unsafe-inline'` is unused by bot strings.

**What a second tailnet node (or the bot) gets:** full confused-deputy console. `GET /api/bots` and `GET /` have no auth. They see missions, pause reasons, questions, **approval ids**. `POST /verdict` has no cookie, no CSRF token, no Origin/Referer check. `form-action 'self'` does not stop other sites posting **here**. `decided_by` is hard-coded `"human:web"`.

**CSRF is reachable.** Classic form POST does not need CORS.

**Exploit A — the bot approves itself (this is the escalation):**

Seatbelt: `network: True`. Control-plane port is not in `deny_read_paths`.

```http
GET http://127.0.0.1:6768/api/bots
POST http://127.0.0.1:6768/verdict
Content-Type: application/x-www-form-urlencoded

approval=<id>&choice=<first option>
```

Attacker-controlled webpage/issue → bot runs that curl → audit says a human approved.

**Exploit B — roommate / other tailnet device:** same, no bot needed.

**Exploit C — browser CSRF / clickjack:** owner visits attacker HTML; form posts to `http://<tailnet>:6768/verdict`. Practical if the attacker already has the id (tailnet peer) or can frame the UI.

**Display binding:** the card shows `question` and `options` (first option is `primary`). It does **not** show `verb`, `parameters`, `evidence`, or `action_hash`. Owner-only rows still render Approve buttons (click then errors). The bot chooses every string.

**Fix:** do not listen on any address the sandbox can route to (loopback + seatbelt `deny` that port, or Unix socket with mode bits). If a phone UI is required, auth it (Tailscale identity / signed cookie) and CSRF-bind. Check `Origin`. `frame-ancestors 'none'`. Render verb + hashed parameters + evidence. Do not emit Approve for `requires_owner`. Remove `/api/bots` from the unauthenticated surface.

---

### 4. Spawn path — the bot authors the object that will get a shell

Caps (depth/fan-out/active) are real and re-checked at activate; create stays `DRAFT`. That does **not** constrain the **definition dict**.

Without `to_bot` here, treat every spec key as attacker-controlled:

| Field | What the bot puts | What it buys |
|---|---|---|
| `slug` | `../../.omnigent/army` or another bot’s name | workspace/path traversal; `write_paths=(workspace,)`; homograph of a trusted bot |
| `workload` / `workload_config` | host path or ctor/import | runs outside the worktree; config that feeds a precondition/constructor is unpickle/import-gadget territory |
| `browser_profile` | `persist:bot-<victim>` or the human profile | cookie-jar theft once partitioning exists |
| `depth` / `parent` / `address` | `depth: 0`, victim parent | skip grandchild cap; inherit someone else’s budget/caps |
| `expiry` / TTL | missing or far future | child never auto-retires |
| `extra_read` / harness | vendor dirs, `allow_authenticated` | if deny is not strictly higher priority than `extra_read`, vendor logins come back |
| `allowance` | huge | `max(0, allowance)` has **no** parent-remaining cap at propose |

`propose(parent)` uses a **caller-supplied in-memory** `parent`. Stale or forged `parent.depth` / id bypasses caps until activate; activate re-reads from DB (good) but still feeds `request.definition` unchanged into `to_bot`.

`MAX_ACTIVE_BOTS` counts only `ACTIVE`. Activate creates `DRAFT`, so fleet size at switch-on is a human rubber-stamp. Fan-out does count non-`RETIRED` (including DRAFT).

**Exploit:** injected bot proposes `slug: "ci-helper"`, `rationale: "run unit tests"`, `workload: /Users/you/.omnigent/owner-runner`, `browser_profile: persist:bot-<owner-bot>`. Human activates on rationale. Child is DRAFT with a privileged ctor; switch-on is a second, smaller click.

**Fix:** allowlist spec keys. Force `parent_id`, `depth`, `created_by`, harness, write/read paths from the DB parent. Slug `^[a-z0-9][a-z0-9-]{0,62}$`. Workload from a **registry of names**, not filesystem paths. Drop constructor-shaped `workload_config`. Cap `allowance` to parent remaining. Show the **canonicalized** definition, not the rationale, before activate.

---

### 5. The one change

**Do not accept verdicts on any socket a bot can open, and do not treat `APPROVE` as “continue the run.”**

A fully compromised bot already has a shell and **shared network**. The unauthenticated Tailscale page turns that into a human. Closing `/verdict` (Unix socket / Tailscale identity that the seatbelt cannot reach) plus making the supervisor execute **only** the hashed live operation cuts the self-approve path and the blank permission slip together. Owner grants stay on the signed owner channel, minted over real parameters.

Until then, `ALWAYS_OWNER` is a label on a row, not a boundary.
4/4 harnesses answered.


## kimicli:kimi-code/k3

**The escalation: a fully compromised bot never needs to forge a grant — it just needs the human to click "yes" to a question the bot wrote, about parameters the page never displays.** The cryptography binds the verdict to the parameters; the UI binds the human to the bot's prose. That gap, plus the web path silently dropping two of the three bindings, is the attack. Details per question:

## 1. Reaching ALWAYS_OWNER

**Medium — the `verb in ALWAYS_OWNER` check is exact-string and fails open.** `request()` sets `requires_owner=verb in ALWAYS_OWNER` (approvals.py, in `ApprovalStore.request`). If any caller constructs the verb from bot-influenced text — `"spend "`, `"Spend"`, `"spend\n"` — the request becomes an ordinary, channel-answerable approval. The fix is trivial: canonicalize verbs at the gate boundary and fail closed on any verb not in a known registry, rather than special-casing three.

**The `{"action_hash": ...}` indirection is a real hole, for a subtler reason.** `broker.check(request.verb, {"action_hash": request.action_hash}, grant)` means the owner's signature covers a hash of a hash. The broker verifies the grant matches the *row* — it can never verify the grant matches the *operation*, because it never sees the real parameters. The chain of custody becomes: bot picks `parameters` → row stores `digest(verb, parameters)` → owner signs that digest → broker checks signature against the row. At no point does any trusted component re-derive the digest from the operation actually about to execute. Whether this is exploitable depends entirely on whether the owner-minting CLI (not shown) renders the real parameters and re-computes the digest itself. If it copies `action_hash` out of the approval row, the owner is signing blind — fix by having the mint path take the real operation, digest it, and refuse if that doesn't equal the row's hash.

## 2. Approval bindings

**High — every binding check is opt-in, and the web page opts out of two of them.** `_check_bindings` only checks what's passed. `web.py`'s `verdict()` passes only `run_version` (and passes `None` if the run was deleted — silently skipping even that). So a verdict from the page skips the action-hash re-check and the policy check entirely. Concrete sequence: bot asks approval for op A (hash H), question reads innocuously → human waits a day → policy tightens or the bot's pending plan shifts to op B → human clicks approve on the page → `run_version` still matches if the run hasn't ticked → verdict lands. The docstring's whole promise ("a mismatch on any of them re-asks") is not honored on the page path.

**High — the human never sees the parameters.** The approval card renders `question` (bot-authored prose), `options`, policy version, run id. It does not render `verb`, `parameters`, or `evidence`. The hash faithfully binds the verdict to `{"to": "attacker", "amount": 50000}` while the human reads "May I re-run the linter?". Also, `options` are bot-authored and the page styles `options[0]` as the primary button — a bot can offer `["Looks fine — continue", "Continue with notes"]`, where every prominent choice approves and only the small "deny" button refuses. Smallest fix: render the canonical `verb` + `parameters` JSON as the primary content of the card, make `decide()` require all four bindings (non-optional, fail closed), and give approve/deny fixed, equal-weight chrome independent of bot-supplied option text.

## 3. The page

XSS looks genuinely closed: `_esc` is applied consistently and `default-src 'none'` kills script execution even if a string slips through. CSS is static; bot strings never reach a style context. I don't see an injection escalation.

**High — CSRF is fully reachable via DNS rebinding.** No auth, no Origin/Host check, no CSRF token, plain form POST. `form-action 'self'` in CSP constrains forms *on this page*, not submissions *to* it. An attacker site the owner visits can blind-POST to `http://<tailscale-ip>:6768/verdict` (needs the approval id — UUID, unguessable), but with DNS rebinding the attacker's page *reads* `/api/bots` (unauthenticated JSON listing every pending approval id) and then approves all of them from the owner's browser. Second person on the tailnet gets the same with plain `curl`: enumerate pending approvals, approve or deny any of them. The "the tailnet is the auth" posture makes this by-design, but it should be named plainly: one compromised tailnet device — or one malicious page in the owner's browser — holds approval authority over the fleet. Fix: a per-session bearer token in the URL plus `SameSite=Strict` cookie, and check `Origin`/`Host` on POST. That's ~20 lines here and downgrades rebinding and rogue-tailnet-device to nothing.

Minor: `notice`/`err` are reflected from the query string (escaped — fine), and the 303 redirect is safe. No issues there.

## 4. Spawn path

**Critical-if-true — bot-authored YAML reaching constructors in the control-plane process.** `to_bot(spec, ...)` runs at proposal *and* activation time, in the trusted process, and `workload_config` feeds "a precondition and a constructor." If the workload is an import path, class name, or command string the bot controls, then activation — the human's "yes, this bot may exist" click — executes attacker-authored code outside the sandbox, with access to vendor credentials and the control-plane DB that the Seatbelt profile exists to deny. I can't see `to_bot`/`definition.py`, so I can't confirm; the fix regardless: `workload` must be a key into a closed, server-side registry of known workload types, and `workload_config` validated as pure data (no eval, no imports, no path resolution). Treat the definition as hostile input to a parser, never as a program.

**High — `slug` as path and partition key.** Slug flows into the workspace path (and browser profile fallback). If `to_bot` doesn't enforce `^[a-z0-9-]+$`, `"../.."`-style slugs escape the workspace root and the sandbox's one-write-path guarantee. Cheap fix: whitelist charset, reject separators and dots.

**Medium — caps race and allowance arithmetic.** `_check_caps` reads state, then the transaction claims the proposal — two concurrent activations from the same parent both pass fan-out and fleet caps (TOCTOU). And `activate` accepts a human-supplied `allowance` override with no clamp against the parent's remaining budget; if `budgets.carve` doesn't enforce non-negativity, "capacity divides" silently becomes "capacity multiplies." Fix: re-check caps inside the transaction and clamp `share ≤ parent.remaining` at carve time, raising otherwise.

## 5. The one change

**Make the bindings mandatory in `decide()` — fail closed, no `None` defaults.** Every other finding is a variant of "a trusted surface accepted bot-controlled input without re-deriving what it was deciding on": the page drops two bindings, the owner grant signs a hash-of-a-hash, spawn validation may feed constructors. Requiring `verb`, `parameters`, `policy_version`, and `run_version` at every decide call — and rendering the canonical operation as the thing the human approves — collapses the largest class: a compromised bot's only real weapon is getting an honest human to say yes to the wrong question, and this is the single edit that makes that yes mean exactly one operation, everywhere, with no caller able to forget.

**Uncertainty I should flag:** I can't see `army.gates.Broker.check`, `to_bot`/`definition.py`, `budgets.carve`, or the owner-mint CLI. Findings 1b, 4a, and the allowance-clamp issue are conditioned on those implementations; findings 2a (optional bindings), 2b (parameters never rendered), and 3 (rebinding/CSRF on an unauthenticated endpoint) are confirmed from the code shown.
