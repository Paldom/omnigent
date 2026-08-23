// Hand-written client for the three `/v1/bots` endpoints, mirroring
// `omnigent/server/routes/bots.py`. Same conventions as the sibling
// `scheduledTasksApi.ts`: requests go through the `/v1` proxy, the wire is
// snake_case while the TS surface is camelCase.
//
// Bot state lives in the `army` control plane, in its own process. The server
// forwards to it on loopback and holds the token, so the browser never sees a
// secret and never talks to a second origin. When that loop is not running
// every response carries `running: false` and a sentence saying so — the page
// renders that rather than an error, because "not started" is a normal state
// for a machine that has not been set up yet.

import { authenticatedFetch } from "./identity";

/** What a bot is doing right now, derived at read time — never stored. */
export type BotStatus =
  | "running"
  | "waiting_human"
  | "waiting_resource"
  | "backing_off"
  | "scheduled"
  | "due"
  | "waiting_event"
  | "manual"
  | "blocked"
  | "inactive";

/** How an iteration classified itself. Drives the next wake. */
export type RunOutcome = "work_done" | "no_work" | "rate_limited" | "blocked" | "retryable_error";

/** One row in the roster. */
export interface BotSummary {
  slug: string;
  title: string | null;
  mission: string;
  status: BotStatus;
  /** Seconds until the next wake, or `null` when nothing is scheduling it. */
  dueIn: number | null;
  idleStreak: number;
  errorStreak: number;
  lastOutcome: RunOutcome | null;
  /** Why the *system* paused it, when the system did. */
  pausedReason: string | null;
  runId: string | null;
  harness: string | null;
  /** Whether this row is the operator's problem rather than the fleet's. */
  needsHuman: boolean;
  /** When its vendor lane reopens, when that is what is holding it. */
  blockedUntil: number | null;
  wakeKind: string;
}

/** A question waiting on a person, and everything a verdict is bound to. */
export interface BotApproval {
  id: string;
  question: string;
  options: string[];
  evidence: Record<string, unknown>;
  verb: string;
  actionHash: string;
  policyVersion: string;
  runId: string;
  runVersion: number;
  /** `spend`, `execute_order`, `add_dependency` — a click here never satisfies one. */
  requiresOwner: boolean;
  expiresAt: number | null;
  createdAt: number;
}

/** One settled iteration. */
export interface BotRun {
  id: string;
  state: string;
  outcome: RunOutcome | null;
  reason: string | null;
  at: number;
  /**
   * The Omnigent conversation this iteration ran in.
   *
   * A bot's body is an ordinary session, so this is the whole answer to "what
   * is the harness actually doing" — `/c/{sessionId}` is the normal chat page,
   * with its transcript, files, terminals, browser and subagents. `null` for
   * an iteration that never opened one.
   */
  sessionId: string | null;
}

/** One entry in a bot's workspace. */
export interface WorkspaceEntry {
  name: string;
  /** Relative to the workspace root — the only path shape the API accepts. */
  path: string;
  dir: boolean;
  bytes: number | null;
  modifiedAt: number;
}

/** A directory listing, or one file's text. */
export interface WorkspaceView {
  running: boolean;
  root: string | null;
  path: string;
  entries: WorkspaceEntry[] | null;
  text: string | null;
  bytes: number | null;
  /** Set when the file is not text, or is past the viewer's cap. */
  reason: string | null;
}

/** One line in a bot's channel. */
export interface BotMessage {
  seq: number;
  kind: "human_msg" | "bot_msg" | "bot_to_bot" | "ask" | "verdict" | "report" | "event";
  author: string;
  body: string;
  at: number;
  thread: string | null;
}

/** Everything the middle column and the dock need, in one round trip. */
export interface BotDetail {
  slug: string;
  displayName: string;
  title: string | null;
  mission: string;
  persona: string;
  workload: string;
  harness: string | null;
  wake: Record<string, unknown>;
  status: BotStatus;
  dueIn: number | null;
  idleStreak: number;
  errorStreak: number;
  pausedReason: string | null;
  workspace: string | null;
  browserProfile: string | null;
  runs: BotRun[];
  channel: BotMessage[];
  pending: BotApproval[];
}

/**
 * An owner-only request, and everything a signature would be bound to.
 *
 * Kept apart from `BotApproval` because it is answered on a different path.
 * Rendering one as an ordinary question with an approve button is exactly the
 * confusion the `ALWAYS_OWNER` list exists to prevent.
 */
export interface OwnerRequest {
  id: string;
  botId: string;
  /** The bot's slug, so a row can say who is asking without a second fetch. */
  bot: string;
  verb: string;
  question: string;
  options: string[];
  evidence: Record<string, unknown>;
  actionHash: string;
  /** What the grant is signed over. Signing an unseen fingerprint is signing a blank. */
  digest: string;
  policyVersion: string;
  runId: string;
  runVersion: number;
  expiresAt: number | null;
  createdAt: number;
}

/** A bot that another bot asked for, and the definition it would get. */
export interface BotDraft {
  id: string;
  slug: string;
  /** Who proposed it. */
  parent: string;
  /** Why, in the parent's own words. */
  rationale: string;
  /** Iterations carved from the parent — not added to the pool. */
  allowance: number;
  definition: Record<string, unknown>;
  createdAt: number;
}

/** The fleet, plus whatever is waiting on a person across all of it. */
export interface BotFleet {
  /** `false` when the control plane is not running; `reason` says why. */
  running: boolean;
  reason?: string;
  counts: Record<string, number>;
  bots: BotSummary[];
  pending: { id: string; botId: string; question: string; requiresOwner: boolean }[];
  owner: OwnerRequest[];
  /** Whether a broker key is configured. Without one, no signature is possible. */
  canSign: boolean;
  drafts: BotDraft[];
}

interface WireSummary {
  slug: string;
  title: string | null;
  mission: string;
  status: BotStatus;
  due_in: number | null;
  idle_streak: number;
  error_streak: number;
  last_outcome: RunOutcome | null;
  paused_reason: string | null;
  run_id: string | null;
  harness: string | null;
  needs_human: boolean;
  blocked_until: number | null;
  wake_kind: string;
}

function toSummary(row: WireSummary): BotSummary {
  return {
    slug: row.slug,
    title: row.title,
    mission: row.mission,
    status: row.status,
    dueIn: row.due_in,
    idleStreak: row.idle_streak,
    errorStreak: row.error_streak,
    lastOutcome: row.last_outcome,
    pausedReason: row.paused_reason,
    runId: row.run_id,
    harness: row.harness,
    needsHuman: row.needs_human,
    blockedUntil: row.blocked_until,
    wakeKind: row.wake_kind,
  };
}

/* eslint-disable @typescript-eslint/no-explicit-any */
function toApproval(row: any): BotApproval {
  return {
    id: row.id,
    question: row.question,
    options: row.options ?? [],
    evidence: row.evidence ?? {},
    verb: row.verb,
    actionHash: row.action_hash,
    policyVersion: row.policy_version,
    runId: row.run_id,
    runVersion: row.run_version,
    requiresOwner: Boolean(row.requires_owner),
    expiresAt: row.expires_at ?? null,
    createdAt: row.created_at,
  };
}

function toOwner(row: any): OwnerRequest {
  return {
    id: row.id,
    botId: row.bot_id,
    bot: row.bot,
    verb: row.verb,
    question: row.question,
    options: row.options ?? [],
    evidence: row.evidence ?? {},
    actionHash: row.action_hash,
    digest: row.digest,
    policyVersion: row.policy_version,
    runId: row.run_id,
    runVersion: row.run_version,
    expiresAt: row.expires_at ?? null,
    createdAt: row.created_at,
  };
}

function toDraft(row: any): BotDraft {
  return {
    id: row.id,
    slug: row.slug,
    parent: row.parent,
    rationale: row.rationale,
    allowance: row.allowance,
    definition: row.definition ?? {},
    createdAt: row.created_at,
  };
}

/** Read the fleet. */
export async function listBots(): Promise<BotFleet> {
  const response = await authenticatedFetch("/v1/bots");
  if (!response.ok) throw new Error(`bots: ${response.status}`);
  const body: any = await response.json();
  return {
    running: Boolean(body.running),
    reason: body.reason,
    counts: body.counts ?? {},
    bots: (body.bots ?? []).map(toSummary),
    pending: (body.pending ?? []).map((row: any) => ({
      id: row.id,
      botId: row.bot_id,
      question: row.question,
      requiresOwner: Boolean(row.requires_owner),
    })),
    owner: (body.owner ?? []).map(toOwner),
    canSign: Boolean(body.can_sign),
    drafts: (body.drafts ?? []).map(toDraft),
  };
}

/** Read one bot: definition, ledger, channel, open question. */
export async function getBot(
  slug: string,
): Promise<BotDetail & { running: boolean; reason?: string }> {
  const response = await authenticatedFetch(`/v1/bots/${encodeURIComponent(slug)}`);
  if (!response.ok) throw new Error(`bot ${slug}: ${response.status}`);
  const body: any = await response.json();
  return {
    running: Boolean(body.running),
    reason: body.reason,
    slug: body.slug,
    displayName: body.display_name,
    title: body.title,
    mission: body.mission,
    persona: body.persona,
    workload: body.workload,
    harness: body.harness,
    wake: body.wake ?? {},
    status: body.status,
    dueIn: body.due_in,
    idleStreak: body.idle_streak ?? 0,
    errorStreak: body.error_streak ?? 0,
    pausedReason: body.paused_reason ?? null,
    workspace: body.workspace ?? null,
    browserProfile: body.browser_profile ?? null,
    runs: (body.runs ?? []).map((row: any) => ({
      id: row.id,
      state: row.state,
      outcome: row.outcome,
      reason: row.reason,
      at: row.at,
      sessionId: row.session_id ?? null,
    })),
    channel: (body.channel ?? []) as BotMessage[],
    pending: (body.pending ?? []).map(toApproval),
  };
}

/**
 * Answer a question.
 *
 * Forwarded to the control plane's own bound path rather than reimplemented,
 * so this page cannot become a softer route to a verdict than the CLI: the
 * action hash, policy version and run version are checked once, where they
 * are owned. A refusal comes back as `ok: false` with the reason in the URL
 * the control plane redirected to.
 */
export async function answerApproval(input: {
  approval: string;
  choice: string;
  approved: boolean;
}): Promise<Verdict> {
  return post("/v1/bots/verdict", input, "The verdict was refused.");
}

/**
 * Sign an owner-only verb, or refuse it.
 *
 * A different endpoint from `answerApproval`, matching a different authority.
 * The control plane mints a one-shot grant bound to the operation digest and
 * spends it through `used_grants`, so the same approval cannot pay twice — and
 * `confirmed` must be literally `true`, because the grant stands in for a
 * signature.
 */
export async function signOwnerRequest(input: {
  approval: string;
  choice?: string;
  approved: boolean;
  confirmed: boolean;
}): Promise<Verdict> {
  return post("/v1/bots/owner", input, "The signature was refused.");
}

/**
 * Decide a proposed bot.
 *
 * Three outcomes, because the store draws the line in two places: creating the
 * bot and switching it on are separate acts, so `draft` creates it dormant and
 * `activate` also writes its first wake.
 *
 * The caps that make replication bounded — depth, fan-out, fleet size, and the
 * allowance carved from the parent — are enforced where they are owned, so a
 * refusal here arrives as a sentence rather than as a silently smaller bot.
 */
export async function adoptDraft(input: {
  spawn: string;
  decision: "activate" | "draft" | "refuse";
  because?: string;
}): Promise<Verdict> {
  return post("/v1/bots/adopt", input, "The proposal was not decided.");
}

/**
 * Browse a bot's workspace, or read one file.
 *
 * The path is always relative to the workspace root, and the control plane
 * refuses anything that escapes it — this client never resolves a path, so
 * there is exactly one place that check lives.
 */
export async function getWorkspace(
  slug: string,
  path: string,
  read = false,
): Promise<WorkspaceView> {
  const query = new URLSearchParams({ path, read: read ? "true" : "false" });
  const response = await authenticatedFetch(`/v1/bots/${encodeURIComponent(slug)}/files?${query}`);
  if (!response.ok) throw new Error(`workspace ${slug}: ${response.status}`);
  const body: any = await response.json();
  return {
    running: Boolean(body.running),
    root: body.root ?? null,
    path,
    entries: body.entries
      ? body.entries.map((row: any) => ({
          name: row.name,
          path: row.path,
          dir: Boolean(row.dir),
          bytes: row.bytes ?? null,
          modifiedAt: row.modified_at,
        }))
      : null,
    text: typeof body.text === "string" ? body.text : null,
    bytes: body.bytes ?? null,
    reason: body.error ?? body.reason ?? (body.binary ? "not a text file" : null),
  };
}

/** What every write returns: it worked, or it says why not. */
export interface Verdict {
  ok: boolean;
  reason?: string;
}

/**
 * POST and read the control plane's answer.
 *
 * A refusal comes back as the reason in the URL the control plane redirected
 * to, which is where it puts one — this is the only place that has to know.
 */
async function post(path: string, input: unknown, fallback: string): Promise<Verdict> {
  const response = await authenticatedFetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  const body: any = await response.json();
  if (!body.running) return { ok: false, reason: body.reason };
  if (body.ok) return { ok: true };
  const location = String(body.location ?? "");
  const err = /[?&]err=([^&]*)/.exec(location);
  return { ok: false, reason: err ? decodeURIComponent(err[1]) : fallback };
}
