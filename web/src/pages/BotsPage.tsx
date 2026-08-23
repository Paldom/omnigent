/**
 * Bots (`/bots`) — the roster, one bot's channel, and its dock.
 *
 * Follows `plan-2/ui-mock/bots.html`, which draws three states of one section:
 * the fleet, an owner signature, and a bot another bot asked for. They share a
 * roster column, so they are sections in it rather than separate routes — a
 * proposal you have to navigate somewhere else to find is a proposal nobody
 * reads.
 *
 * Two conventions from the design system, both load-bearing and both easy to
 * lose: status is an 8px disc plus a word in body colour — never coloured
 * text, never a fill behind type, never a border on one side of a card — and
 * there are exactly two type steps. The single exception is the amount on a
 * signature card, which the mock sets at 22px, because the number that commits
 * money is the one thing on that card you must not misread.
 */

import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  BotIcon,
  ExternalLinkIcon,
  FileTextIcon,
  FolderIcon,
  Loader2Icon,
  TriangleAlertIcon,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { PageScroll } from "@/components/PageScroll";
import { Link } from "@/lib/routing";
import { Button } from "@/components/ui/button";
import {
  useAdoptDraft,
  useAnswerApproval,
  useBot,
  useBots,
  useSignOwnerRequest,
  useWorkspace,
} from "@/hooks/useBots";
import type {
  BotApproval,
  BotDetail,
  BotDraft,
  BotStatus,
  BotSummary,
  Lineage,
  OwnerRequest,
  Relative,
} from "@/lib/botsApi";
import { cn } from "@/lib/utils";

/** Which disc a status gets. Absent ⇒ the hollow "quiet" disc. */
const DISC: Partial<Record<BotStatus, string>> = {
  waiting_human: "bg-[var(--status-red)]",
  blocked: "bg-[var(--status-red)]",
  running: "bg-[var(--status-green)]",
  waiting_resource: "bg-[var(--status-yellow)]",
  backing_off: "bg-[var(--status-yellow)]",
  due: "bg-[var(--status-yellow)]",
};

type DockTab = "files" | "runs" | "lineage" | "setup";

const DOCK_TABS: { value: DockTab; label: string }[] = [
  { value: "files", label: "Files" },
  { value: "runs", label: "Runs" },
  { value: "lineage", label: "Lineage" },
  { value: "setup", label: "Setup" },
];

/** Render a delay in the least noisy unit that is still honest. */
function short(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86_400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86_400)}d`;
}

function ago(at: number): string {
  return `${short(Date.now() / 1000 - at)} ago`;
}

/**
 * Elide the middle of a long fingerprint.
 *
 * Both ends, never a prefix: a prefix is the part an attacker can grind, and
 * the point of showing a digest is that a person can compare it to another
 * one. A 64-character hash wrapped over two lines is a hash nobody checks.
 */
function elide(value: string, head = 12, tail = 4): string {
  return value.length <= head + tail + 1 ? value : `${value.slice(0, head)}…${value.slice(-tail)}`;
}

/** The line under a bot's name, when there is something worth saying. */
function reasonFor(bot: BotSummary): string {
  if (bot.pausedReason) return bot.pausedReason;
  if (bot.status === "waiting_resource" && bot.blockedUntil) {
    return `${bot.harness ?? "its vendor"} lane cooling for ${short(bot.blockedUntil - Date.now() / 1000)}`;
  }
  if (bot.status === "backing_off") return `nothing to do ${bot.idleStreak}× running`;
  if (bot.status === "blocked") return "no next wake and nothing pending — it needs waking by hand";
  if (bot.status === "running" && bot.runId) return `run ${bot.runId.slice(0, 12)}`;
  if (bot.errorStreak) return `failed ${bot.errorStreak}× running`;
  return bot.mission;
}

/**
 * The strip the AppShell header overlays.
 *
 * `h-14 md:h-12` is the header's own geometry, copied rather than derived from
 * `--omnigent-header-height`: that variable is 3.5rem, the *mobile* height, and
 * using it left eight pixels of dead space under a 48px desktop header — which
 * is precisely the kind of not-quite-aligned that makes a page look wrong
 * without anyone being able to say why.
 *
 * Inside each column, so each column's background reaches the top of the
 * window and the three columns stay flush.
 */
function TopBand() {
  return <div aria-hidden className="h-14 shrink-0 md:h-12" />;
}

/** An 8px disc. Colour lives here and nowhere else. */
function Disc({ status }: { status: BotStatus }) {
  const filled = DISC[status];
  return (
    <span
      aria-hidden
      className={cn(
        "mt-[7px] size-2 shrink-0 rounded-full",
        filled ?? "border-[1.5px] border-[var(--status-gray)]",
      )}
    />
  );
}

/** One roster row. Every section uses this, so they cannot drift apart. */
function Row({
  disc,
  name,
  suffix,
  reason,
  when,
  active,
  onSelect,
}: {
  disc: ReactNode;
  name: string;
  suffix?: string;
  reason: string;
  when: string;
  active: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-current={active ? "page" : undefined}
      className={cn(
        "grid w-full grid-cols-[8px_1fr_auto] items-start gap-x-[9px] rounded-otto-sm px-2 py-1 text-left",
        "hover:bg-muted",
        active && "bg-[var(--sidebar-active)]",
      )}
    >
      {disc}
      <span className="min-w-0">
        <span
          className={cn(
            "block truncate leading-5",
            active && "text-[var(--sidebar-active-foreground)]",
          )}
        >
          {name} {suffix && <span className="text-muted-foreground text-sm">{suffix}</span>}
        </span>
        <span className="block truncate text-muted-foreground text-sm">{reason}</span>
      </span>
      <span className="font-mono text-muted-foreground text-sm whitespace-nowrap">{when}</span>
    </button>
  );
}

function RosterRow({
  bot,
  active,
  onSelect,
}: {
  bot: BotSummary;
  active: boolean;
  onSelect: () => void;
}) {
  return (
    <Row
      disc={<Disc status={bot.status} />}
      name={bot.slug}
      suffix={bot.status}
      reason={reasonFor(bot)}
      when={bot.dueIn == null ? "—" : bot.dueIn === 0 ? "due" : short(bot.dueIn)}
      active={active}
      onSelect={onSelect}
    />
  );
}

/**
 * The approval card. One card species, borrowed from the Alert primitive:
 * uniform hairline, no tint, no shadow, severity carried by the disc alone.
 */
function ApprovalCard({
  approval,
  onAnswer,
  busy,
}: {
  approval: BotApproval;
  onAnswer: (choice: string, approved: boolean) => void;
  busy: boolean;
}) {
  const evidence = Object.entries(approval.evidence ?? {});
  return (
    <section className="mb-2 grid grid-cols-[auto_1fr] gap-x-2 rounded-otto-sm border border-border bg-card px-2.5 py-2">
      <span
        aria-hidden
        className="mt-[7px] size-2 shrink-0 rounded-full bg-[var(--status-yellow)]"
      />
      <div className="min-w-0">
        <h3 className="flex flex-wrap items-baseline gap-1.5 font-medium">
          {approval.requiresOwner ? "Sent to the owner" : "Needs approval"}
          <span className="text-muted-foreground text-sm font-normal">
            ·{" "}
            {approval.requiresOwner
              ? "this channel cannot authorise it"
              : approval.verb.replace(/_/g, " ")}
          </span>
        </h3>
        <p className="mt-0.5">{approval.question}</p>

        {evidence.length > 0 && (
          <Bindings rows={evidence.map(([key, value]) => [key, String(value)])} />
        )}

        {approval.requiresOwner ? (
          <p className="mt-2 text-muted-foreground text-sm">
            <code className="rounded bg-muted px-1 py-px font-mono">{approval.verb}</code> is
            owner-only. The broker signs it against the exact operation digest, once, and spends it
            through <code className="rounded bg-muted px-1 py-px font-mono">used_grants</code> — a
            click here never satisfies one.
          </p>
        ) : (
          <div className="mt-2.5 flex flex-wrap gap-1.5">
            {approval.options.map((option, index) => (
              <Button
                key={option}
                size="sm"
                variant={index === 0 ? "default" : "outline"}
                disabled={busy}
                onClick={() => onAnswer(option, true)}
              >
                {option}
              </Button>
            ))}
            <Button size="sm" variant="outline" disabled={busy} onClick={() => onAnswer("", false)}>
              Deny…
            </Button>
          </div>
        )}

        <p className="mt-2 font-mono text-muted-foreground text-sm">
          action {approval.actionHash.slice(0, 8)} · policy {approval.policyVersion} · run{" "}
          {approval.runId.slice(0, 8)} · rev {approval.runVersion}
          {approval.expiresAt != null &&
            ` · expires in ${short(approval.expiresAt - Date.now() / 1000)}`}
        </p>
      </div>
    </section>
  );
}

/** Key/value evidence, the one shape both cards use to show bindings. */
function Bindings({ rows }: { rows: [string, string][] }) {
  return (
    <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-sm">
      {rows.map(([key, value]) => (
        <div key={key} className="contents">
          <dt className="text-muted-foreground">{key}</dt>
          <dd className="m-0 font-mono break-all">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * The signature card for an owner-only verb.
 *
 * The one place in the product where a click commits money, so it is the one
 * place with an explicit affirmation: the button stays disabled until the
 * person says they are the owner and that they authorise *this* operation. The
 * digest is on the card because a signature over a fingerprint you were never
 * shown is a signature over a blank.
 */
function OwnerCard({
  request,
  canSign,
  onDecide,
  busy,
}: {
  request: OwnerRequest;
  canSign: boolean;
  onDecide: (approved: boolean) => void;
  busy: boolean;
}) {
  const [confirmed, setConfirmed] = useState(false);
  // A fresh question deserves a fresh affirmation. Without this, a tick that
  // swapped the pending request underneath a ticked box would carry the
  // consent from one operation to another.
  useEffect(() => setConfirmed(false), [request.id]);

  const amount = request.evidence.amount ?? request.evidence.usd;
  return (
    <section className="grid grid-cols-[auto_1fr] gap-x-2 rounded-otto-sm border border-border bg-card px-2.5 py-2">
      <span aria-hidden className="mt-[7px] size-2 shrink-0 rounded-full bg-[var(--status-red)]" />
      <div className="min-w-0">
        <h2 className="flex flex-wrap items-baseline gap-1.5 font-medium">
          Needs your signature
          <span className="text-muted-foreground text-sm font-normal">· {request.bot}</span>
        </h2>
        {amount != null && (
          <p className="m-0 mt-1.5 text-[22px] leading-tight font-semibold tracking-[-0.01em]">
            {String(amount)}
          </p>
        )}
        <p className="mt-0.5">{request.question}</p>

        <Bindings
          rows={[
            ["verb", request.verb],
            ...Object.entries(request.evidence)
              .filter(([key]) => key !== "amount" && key !== "usd")
              .map(([key, value]) => [key, String(value)] as [string, string]),
            ["digest", elide(request.digest)],
            ["policy", request.policyVersion],
            ["run", `${request.runId.slice(0, 8)} · rev ${request.runVersion}`],
            [
              "expires",
              request.expiresAt == null
                ? "single use"
                : `in ${short(request.expiresAt - Date.now() / 1000)} · single use`,
            ],
          ]}
        />

        {canSign ? (
          <>
            <label className="mt-3 flex items-center gap-2">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(event) => setConfirmed(event.target.checked)}
                className="size-3.5 shrink-0 accent-[var(--primary)]"
              />
              <span>I am the owner and I authorise this exact operation.</span>
            </label>
            <div className="mt-2.5 flex flex-wrap gap-1.5">
              <Button size="sm" disabled={!confirmed || busy} onClick={() => onDecide(true)}>
                Sign and release
              </Button>
              <Button size="sm" variant="outline" disabled={busy} onClick={() => onDecide(false)}>
                Refuse
              </Button>
            </div>
          </>
        ) : (
          <p className="mt-2.5 text-muted-foreground text-sm">
            No <code className="rounded bg-muted px-1 py-px font-mono">ARMY_BROKER_KEY</code> is
            configured, so nothing here can sign. Owner-only verbs are refused outright rather than
            quietly permitted — set the key on the control plane and this becomes answerable.
          </p>
        )}

        <p className="mt-2 text-muted-foreground text-sm">
          A free-text reply anywhere, including {request.bot}&rsquo;s own channel, can never satisfy
          this. The grant is bound by HMAC to the digest above, spent through{" "}
          <code className="rounded bg-muted px-1 py-px font-mono">used_grants</code> on first use,
          and worthless afterwards.
        </p>
      </div>
    </section>
  );
}

/**
 * A bot another bot asked for.
 *
 * Both halves, never just the first: the rationale is what the parent says it
 * wants, and the definition is what would actually be created. Approving the
 * pitch without reading the definition is the whole attack, so the definition
 * is shown in full and is not collapsed.
 */
function DraftCard({
  draft,
  onDecide,
  busy,
}: {
  draft: BotDraft;
  onDecide: (decision: "activate" | "draft" | "refuse") => void;
  busy: boolean;
}) {
  // Rendered as the YAML it will become, one level deep — a nested `wake`
  // flattened onto one line as raw JSON is the field most worth reading and
  // the one hardest to read that way.
  const definition = Object.entries(draft.definition)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, value]) =>
      value !== null && typeof value === "object" && !Array.isArray(value)
        ? [
            `${key}:`,
            ...Object.entries(value as Record<string, unknown>).map(
              ([inner, own]) => `  ${inner}: ${String(own)}`,
            ),
          ].join("\n")
        : `${key}: ${typeof value === "string" ? value : JSON.stringify(value)}`,
    )
    .join("\n");

  return (
    <section className="grid grid-cols-[auto_1fr] gap-x-2 rounded-otto-sm border border-border bg-card px-2.5 py-2">
      <span
        aria-hidden
        className="mt-[7px] size-2 shrink-0 rounded-full bg-[var(--status-yellow)]"
      />
      <div className="min-w-0">
        <h2 className="flex flex-wrap items-baseline gap-1.5 font-medium">
          {draft.slug}
          <span className="text-muted-foreground text-sm font-normal">
            · proposed by {draft.parent}
          </span>
        </h2>
        <p className="mt-0.5">{draft.rationale}</p>

        <p className="mt-2 text-muted-foreground text-sm">
          A bot is data, so one the fleet invents at 3am is a row like any other — and it arrives as
          a proposal. Activation is a human act, which is what makes runaway replication impossible
          rather than merely discouraged.
        </p>

        <pre className="mt-2 rounded-otto-sm bg-muted px-2 py-1.5 font-mono text-sm break-words whitespace-pre-wrap">
          {definition || "(the proposal carried no definition)"}
        </pre>

        <div className="mt-2.5 flex flex-wrap gap-1.5">
          <Button size="sm" disabled={busy} onClick={() => onDecide("activate")}>
            Activate
          </Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => onDecide("draft")}>
            Keep as draft
          </Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => onDecide("refuse")}>
            Discard
          </Button>
        </div>
        <p className="mt-2 text-muted-foreground text-sm">
          Two decisions, not one: saying this bot should exist is separate from switching it on, so
          approving several proposals in a row has not started several bots.
        </p>

        <Bindings
          rows={[
            ["allowance", `${draft.allowance} iterations carved from ${draft.parent}`],
            ["proposed", ago(draft.createdAt)],
          ]}
        />
        <p className="mt-2 text-muted-foreground text-sm">
          The allowance is carved from {draft.parent}&rsquo;s remaining, not added to the pool. A
          bot cannot create capacity by creating bots; it can only divide what it already had.
        </p>
      </div>
    </section>
  );
}

/** One line in the channel. */
function ChannelLine({ kind, author, body, at }: BotDetail["channel"][number]) {
  const isAsk = kind === "ask";
  return (
    <div className="border-t border-[var(--border-weak)] py-2.5">
      <p className="m-0 flex items-baseline gap-2 text-muted-foreground text-sm">
        <span className={cn("text-foreground", isAsk && "font-medium")}>{author}</span>
        <span>{kind.replace(/_/g, " ")}</span>
        <span className="ml-auto font-mono">{ago(at)}</span>
      </p>
      <p className="m-0 mt-0.5 whitespace-pre-wrap">{body}</p>
    </div>
  );
}

/** Bytes, in the shortest honest unit. */
function bytes(count: number): string {
  if (count < 1024) return `${count} B`;
  if (count < 1024 * 1024) return `${Math.round(count / 1024)} KB`;
  return `${(count / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * The bot's workspace — a real file browser over real files.
 *
 * This was a hardcoded array of three filenames, which looked like a file
 * browser and was a picture of one. `charter.md`, `runbook.md` and
 * `lessons.md` are the documents a person edits to steer a bot, and `reports/`
 * is what it writes back, so they are worth browsing and reading rather than
 * naming.
 */
function WorkspaceBrowser({ slug, workspace }: { slug: string; workspace: string | null }) {
  const [path, setPath] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  // Selecting a bot must not leave you inside the previous bot's `reports/`.
  useEffect(() => {
    setPath("");
    setOpen(null);
  }, [slug]);

  const listing = useWorkspace(slug, path);
  const file = useWorkspace(slug, open ?? "", Boolean(open));

  if (open) {
    const markdown = open.endsWith(".md");
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex shrink-0 items-center gap-2 border-b border-border px-2.5 py-1.5">
          <button
            type="button"
            onClick={() => setOpen(null)}
            className="rounded-otto-button px-1.5 py-0.5 text-muted-foreground hover:bg-muted"
          >
            ← back
          </button>
          <span className="truncate font-mono text-sm">{open}</span>
          {file.data?.bytes != null && (
            <span className="ml-auto shrink-0 font-mono text-muted-foreground text-sm">
              {bytes(file.data.bytes)}
            </span>
          )}
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-2.5 py-2">
          {file.isLoading && <Loader2Icon className="size-4 animate-spin text-muted-foreground" />}
          {file.data?.reason && <p className="text-muted-foreground text-sm">{file.data.reason}</p>}
          {file.data?.text != null &&
            (markdown ? (
              // Same pipeline as the notebook preview, so a charter reads the
              // way every other markdown surface in the product reads.
              <div className="prose dark:prose-invert prose-sm max-w-none">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{file.data.text}</ReactMarkdown>
              </div>
            ) : (
              <pre className="font-mono text-sm break-words whitespace-pre-wrap">
                {file.data.text}
              </pre>
            ))}
        </div>
      </div>
    );
  }

  const up = path === "" ? null : path.split("/").slice(0, -1).join("/");
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b border-border px-2.5 py-1.5">
        {up !== null && (
          <button
            type="button"
            onClick={() => setPath(up)}
            className="rounded-otto-button px-1.5 py-0.5 text-muted-foreground hover:bg-muted"
          >
            ← up
          </button>
        )}
        <span className="truncate font-mono text-muted-foreground text-sm">
          {path || (workspace ?? `~/bots/${slug}`)}
        </span>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-2.5 py-1.5">
        {listing.isLoading && <Loader2Icon className="size-4 animate-spin text-muted-foreground" />}
        {listing.data?.reason && (
          <p className="text-muted-foreground text-sm">{listing.data.reason}</p>
        )}
        {listing.data?.entries?.length === 0 && (
          <p className="text-muted-foreground text-sm">Nothing here.</p>
        )}
        {listing.data?.entries?.map((entry) => (
          <button
            key={entry.path}
            type="button"
            onClick={() => (entry.dir ? setPath(entry.path) : setOpen(entry.path))}
            className="flex w-full items-center gap-2 rounded-otto-sm px-1 py-1 text-left hover:bg-muted"
          >
            {entry.dir ? (
              <FolderIcon className="size-3.5 shrink-0 text-muted-foreground" />
            ) : (
              <FileTextIcon className="size-3.5 shrink-0 text-muted-foreground" />
            )}
            <span className="truncate font-mono text-sm">{entry.name}</span>
            <span className="ml-auto shrink-0 font-mono text-muted-foreground text-sm">
              {entry.dir ? "" : bytes(entry.bytes ?? 0)}
            </span>
          </button>
        ))}
        <p className="mt-2.5 text-muted-foreground text-sm">
          The content lives in git; <code className="font-mono">bot_docs</code> only indexes it, so
          history and review stay where they already work.
        </p>
      </div>
    </div>
  );
}

/**
 * Who a bot came from and what came from it.
 *
 * The chain was already in the data and nothing showed it, so the caps that
 * make replication bounded were invisible exactly when they matter — when you
 * are looking at a proposal and deciding whether to adopt one more bot.
 *
 * Rendered as a chain rather than a table: depth is the cap that stops this
 * going three levels deep, and a list of names does not show depth.
 */
function LineagePanel({
  lineage,
  onSelect,
}: {
  lineage: Lineage;
  onSelect: (slug: string) => void;
}) {
  const relative = (entry: Relative, indent: number, note?: string) => (
    <button
      key={entry.slug}
      type="button"
      onClick={() => onSelect(entry.slug)}
      style={{ paddingLeft: `${4 + indent * 14}px` }}
      className="flex w-full items-baseline gap-2 rounded-otto-sm py-1 pr-1 text-left hover:bg-muted"
    >
      {indent > 0 && <span className="text-muted-foreground">↳</span>}
      <span className="truncate">{entry.slug}</span>
      <span className="truncate text-muted-foreground text-sm">
        {note ?? entry.status.replace(/_/g, " ")}
      </span>
      {entry.remaining != null && (
        <span className="ml-auto shrink-0 font-mono text-muted-foreground text-sm">
          {entry.remaining} left
        </span>
      )}
    </button>
  );

  const alone = !lineage.parent && lineage.children.length === 0;
  return (
    <>
      {alone ? (
        <p className="text-muted-foreground text-sm">
          Nothing depends on this bot and it depends on nothing. A person created it directly.
        </p>
      ) : (
        <>
          {lineage.parent && relative(lineage.parent, 0, "parent")}
          {lineage.siblings.map((entry) => relative(entry, 1, "sibling"))}
          {lineage.children.map((entry) => relative(entry, lineage.parent ? 2 : 1))}
        </>
      )}

      <Bindings
        rows={[
          [
            "depth",
            `${lineage.depth} of ${lineage.maxDepth}` +
              (lineage.depth >= lineage.maxDepth ? " — it cannot spawn again" : ""),
          ],
          [
            "fan-out",
            `${lineage.children.length} of ${lineage.maxFanout}` +
              (lineage.parent ? ` under ${lineage.parent.slug}` : ""),
          ],
        ]}
      />

      <p className="mt-2.5 text-muted-foreground text-sm">
        {lineage.cascades
          ? `Retiring this retires its ${lineage.children.length === 1 ? "child" : `${lineage.children.length} children`} too — the one action here with a blast radius larger than the row you clicked.`
          : "A child's allowance is carved from its parent's remaining, not added to the pool. A bot cannot create capacity by creating bots."}
      </p>
    </>
  );
}

function Dock({
  bot,
  tab,
  setTab,
  onSelect,
}: {
  bot: BotDetail;
  tab: DockTab;
  setTab: (tab: DockTab) => void;
  onSelect: (slug: string) => void;
}) {
  // The tab lives on the page, not here. Owning it locally meant the operator's
  // choice was lost every time the poll briefly cleared `detail.data` and this
  // unmounted — a panel that resets itself under you while you read it.
  return (
    // `h-11` matches the roster and channel headers, so the three columns
    // share one horizontal rule instead of three that nearly line up.
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-11 shrink-0 items-center gap-1 border-b border-border px-3">
        {DOCK_TABS.map((entry) => (
          <button
            key={entry.value}
            type="button"
            onClick={() => setTab(entry.value)}
            aria-current={tab === entry.value ? "page" : undefined}
            className={cn(
              "rounded-otto-button px-2 py-0.5 hover:bg-muted",
              tab === entry.value && "bg-secondary",
            )}
          >
            {entry.label}
          </button>
        ))}
      </div>

      {/* Files owns its own scrolling — it has a sticky breadcrumb and a
          preview that must not scroll with the tree above it. */}
      {tab === "files" && <WorkspaceBrowser slug={bot.slug} workspace={bot.workspace} />}

      {tab !== "files" && (
        <div className="min-h-0 flex-1 overflow-y-auto px-2.5 py-2">
          {tab === "runs" && (
            <>
              {bot.runs.length === 0 && (
                <p className="text-muted-foreground text-sm">It has not run yet.</p>
              )}
              {bot.runs.map((run) => (
                <div
                  key={run.id}
                  className="grid grid-cols-[auto_1fr] gap-x-2 border-t border-[var(--border-weak)] py-1.5 first:border-t-0"
                >
                  <span className="font-mono text-sm">{run.id.slice(0, 8)}</span>
                  <span className="min-w-0">
                    <span className="block truncate">
                      {(run.outcome ?? run.state).replace(/_/g, " ")}
                    </span>
                    <span className="block truncate text-muted-foreground text-sm">
                      {run.reason ?? run.state}
                    </span>
                    {/* The iteration's own session. A bot's body is an ordinary
                      conversation, so this is a link to the real chat page —
                      transcript, tool calls, files, terminals and all. */}
                    {run.sessionId && (
                      <Link
                        to={`/c/${run.sessionId}`}
                        className="mt-0.5 inline-flex items-center gap-1 text-muted-foreground text-sm underline-offset-2 hover:text-foreground hover:underline"
                      >
                        <ExternalLinkIcon className="size-3" />
                        open session
                      </Link>
                    )}
                  </span>
                </div>
              ))}
              <p className="mt-2.5 text-muted-foreground text-sm">
                <code className="font-mono">next_due_at</code> is null while blocked, so a waiting
                bot costs nothing. Answering writes the verdict and the successor wake in one
                transaction.
              </p>
            </>
          )}

          {tab === "lineage" && <LineagePanel lineage={bot.lineage} onSelect={onSelect} />}

          {tab === "setup" && (
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
              {[
                ["workload", bot.workload],
                ["harness", bot.harness ?? "router picks"],
                ["partition", bot.browserProfile ?? "—"],
                ["idle streak", String(bot.idleStreak)],
                ["error streak", String(bot.errorStreak)],
                ...Object.entries(bot.wake).map(
                  ([key, value]) => [key, JSON.stringify(value)] as [string, string],
                ),
              ].map(([key, value]) => (
                <div key={key} className="contents">
                  <dt className="text-muted-foreground">{key}</dt>
                  <dd className="m-0 font-mono break-all">{value}</dd>
                </div>
              ))}
            </dl>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * What the middle column is showing.
 *
 * Three kinds, because the roster holds three kinds of thing. Keeping them one
 * union rather than three independent selections is what makes "exactly one
 * row is highlighted" true by construction.
 */
type Selection =
  { kind: "bot"; slug: string } | { kind: "owner"; id: string } | { kind: "draft"; id: string };

/** The id, when the selection is of that kind. */
function selectedId(selection: Selection | null, kind: "owner" | "draft"): string | null {
  return selection?.kind === kind ? selection.id : null;
}

export function BotsPage() {
  const fleet = useBots();
  const [selection, setSelection] = useState<Selection | null>(null);
  const [dockTab, setDockTab] = useState<DockTab>("files");
  const [refusal, setRefusal] = useState<string | null>(null);

  // Memoised: `?? []` allocates a fresh array on every render, which would
  // re-run the selection effect and the splits on every poll tick.
  const rows = useMemo(() => fleet.data?.bots ?? [], [fleet.data]);
  const owner = useMemo(() => fleet.data?.owner ?? [], [fleet.data]);
  const drafts = useMemo(() => fleet.data?.drafts ?? [], [fleet.data]);

  const [needsYou, quiet] = useMemo(
    () => [rows.filter((bot) => bot.needsHuman), rows.filter((bot) => !bot.needsHuman)],
    [rows],
  );

  const ownerRequest = owner.find((request) => request.id === selectedId(selection, "owner"));
  const draft = drafts.find((entry) => entry.id === selectedId(selection, "draft"));
  // A signature settles a run in the asking bot's channel, so the detail query
  // follows the owner row too — the dock and the stream stay in step with what
  // is being decided.
  const slug = selection?.kind === "bot" ? selection.slug : ownerRequest ? ownerRequest.bot : null;

  const detail = useBot(slug);
  const answer = useAnswerApproval(slug);
  const sign = useSignOwnerRequest(slug);
  const adopt = useAdoptDraft();

  // The most recent iteration that had a body. Newest-first from the store, so
  // the first hit is the one to open — a bot between iterations still links to
  // where it last worked, which is what you want when it just failed.
  const liveSession = detail.data?.runs.find((entry) => entry.sessionId)?.sessionId ?? null;

  // Open on the most consequential thing waiting: a signature commits money and
  // is irreversible, a question only gates an iteration, and a proposal creates
  // nothing until someone says so. Re-running only when nothing is selected
  // keeps a chosen row chosen while the roster polls underneath it.
  useEffect(() => {
    if (selection) return;
    if (owner.length > 0) setSelection({ kind: "owner", id: owner[0].id });
    else if (needsYou.length > 0) setSelection({ kind: "bot", slug: needsYou[0].slug });
    else if (drafts.length > 0) setSelection({ kind: "draft", id: drafts[0].id });
    else if (rows.length > 0) setSelection({ kind: "bot", slug: rows[0].slug });
  }, [selection, owner, needsYou, drafts, rows]);

  // A decided row leaves the roster, and a selection pointing at a row that is
  // gone renders an empty column — which is exactly what answering something
  // produces, so it is the common case rather than the edge one. Clearing puts
  // the effect above back in charge of choosing what is next.
  //
  // Guarded on the fleet having loaded, not on the list being non-empty: an
  // emptied list is precisely when this has to fire.
  const loaded = Boolean(fleet.data);
  const stale =
    (selection?.kind === "owner" && !ownerRequest) || (selection?.kind === "draft" && !draft);
  useEffect(() => {
    if (loaded && stale) setSelection(null);
  }, [loaded, stale]);

  async function run(action: () => Promise<{ ok: boolean; reason?: string }>, fallback: string) {
    setRefusal(null);
    const result = await action();
    if (!result.ok) setRefusal(result.reason ?? fallback);
    else setSelection(null);
  }

  async function handleAnswer(approval: BotApproval, choice: string, approved: boolean) {
    setRefusal(null);
    const result = await answer.mutateAsync({ approval: approval.id, choice, approved });
    if (!result.ok) setRefusal(result.reason ?? "The verdict was refused.");
  }

  if (fleet.isLoading) {
    return (
      <PageScroll className="grid place-items-center">
        <Loader2Icon className="size-4 animate-spin text-muted-foreground" />
      </PageScroll>
    );
  }

  if (fleet.data && !fleet.data.running) {
    return (
      <PageScroll className="mx-auto max-w-xl px-5">
        <h1 className="font-medium">Bots</h1>
        <div className="mt-3 rounded-otto-sm border border-border bg-card px-2.5 py-2">
          <p className="m-0">{fleet.data.reason}</p>
          <p className="m-0 mt-2 font-mono text-muted-foreground text-sm">
            army bots example &gt; heartbeat.yaml
            <br />
            army bots create heartbeat.yaml --activate
            <br />
            army bots run
          </p>
        </div>
      </PageScroll>
    );
  }

  return (
    // The three columns scroll independently, so this cannot be a PageScroll.
    // The AppShell header is an absolute overlay across the top of this whole
    // region, so every column opens with a `TopBand` spacer of exactly its
    // height. Padding the *grid* instead — which is what this did — left the
    // band outside all three columns, so the roster's tint stopped short of
    // the top and the page had a white stripe across it.
    <div
      className="grid h-full min-h-0 grid-cols-[276px_minmax(0,1fr)] xl:grid-cols-[276px_minmax(0,1fr)_340px]"
      style={{ paddingBottom: "var(--omnigent-inset-bottom)" }}
    >
      {/* ── roster ─────────────────────────────────────────────── */}
      <div className="flex min-h-0 flex-col border-r border-border bg-sidebar">
        <TopBand />
        <div className="flex h-11 shrink-0 items-center justify-between border-b border-border px-5">
          <span className="flex items-center gap-2 font-medium">
            <BotIcon className="size-3.5 text-muted-foreground" />
            Bots
          </span>
          <span className="text-muted-foreground text-sm">{rows.length}</span>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-3 pt-1 pb-3">
          {/* Ordered by consequence, not by kind: a signature moves money and
            cannot be taken back, a question only gates an iteration, and a
            proposal has created nothing yet. */}
          {owner.length > 0 && (
            <>
              <p className="px-2 pt-2.5 pb-1 text-muted-foreground text-sm">Owner approvals</p>
              {owner.map((request) => (
                <Row
                  key={request.id}
                  disc={
                    <span
                      aria-hidden
                      className="mt-[7px] size-2 shrink-0 rounded-full bg-[var(--status-red)]"
                    />
                  }
                  name={request.verb.replace(/_/g, " ")}
                  suffix={String(request.evidence.amount ?? request.evidence.usd ?? "")}
                  reason={`${request.bot} · needs your signature`}
                  when={ago(request.createdAt).replace(" ago", "")}
                  active={request.id === selectedId(selection, "owner")}
                  onSelect={() => setSelection({ kind: "owner", id: request.id })}
                />
              ))}
            </>
          )}

          {needsYou.length > 0 && (
            <>
              <p className="px-2 pt-2.5 pb-1 text-muted-foreground text-sm">Needs you</p>
              {needsYou.map((bot) => (
                <RosterRow
                  key={bot.slug}
                  bot={bot}
                  active={selection?.kind === "bot" && bot.slug === selection.slug}
                  onSelect={() => setSelection({ kind: "bot", slug: bot.slug })}
                />
              ))}
            </>
          )}

          {drafts.length > 0 && (
            <>
              <p className="px-2 pt-2.5 pb-1 text-muted-foreground text-sm">Draft</p>
              {drafts.map((entry) => (
                <Row
                  key={entry.id}
                  disc={
                    <span
                      aria-hidden
                      className="mt-[7px] size-2 shrink-0 rounded-full border-[1.5px] border-[var(--status-yellow)]"
                    />
                  }
                  name={entry.slug}
                  suffix="draft"
                  reason={`proposed by ${entry.parent}`}
                  when={ago(entry.createdAt).replace(" ago", "")}
                  active={entry.id === selectedId(selection, "draft")}
                  onSelect={() => setSelection({ kind: "draft", id: entry.id })}
                />
              ))}
            </>
          )}

          <p className="px-2 pt-2.5 pb-1 text-muted-foreground text-sm">Bots</p>
          {quiet.map((bot) => (
            <RosterRow
              key={bot.slug}
              bot={bot}
              active={selection?.kind === "bot" && bot.slug === selection.slug}
              onSelect={() => setSelection({ kind: "bot", slug: bot.slug })}
            />
          ))}
          {rows.length === 0 && (
            <p className="px-2 text-muted-foreground text-sm">
              No bots yet. <span className="font-mono">army bots example</span>
            </p>
          )}
        </div>
      </div>

      {/* ── channel ────────────────────────────────────────────── */}
      <div className="flex min-h-0 flex-col">
        <TopBand />
        <div className="flex h-11 shrink-0 items-center gap-2 border-b border-border px-5">
          <h1 className="shrink-0 font-medium">
            {draft ? draft.slug : ownerRequest ? "Owner approvals" : (detail.data?.slug ?? "Bots")}
          </h1>
          <span className="flex min-w-0 items-center gap-1.5 truncate text-muted-foreground text-sm">
            {draft ? (
              `draft · proposed by ${draft.parent}`
            ) : ownerRequest ? (
              "signed channel"
            ) : detail.data ? (
              <>
                <Disc status={detail.data.status} />
                {detail.data.status.replace(/_/g, " ")}
                {detail.data.dueIn != null && ` · due in ${short(detail.data.dueIn)}`}
              </>
            ) : null}
          </span>
          {/* The live body. A bot without one is between iterations, which is
              the normal state — the link appears when there is something to
              look at, rather than sitting there dead. */}
          {liveSession && (
            <Link
              to={`/c/${liveSession}`}
              className="ml-auto flex shrink-0 items-center gap-1.5 rounded-otto-button border border-border px-2 py-0.5 text-sm hover:bg-muted"
            >
              <ExternalLinkIcon className="size-3" />
              Open session
            </Link>
          )}
        </div>

        {/* Capped at a reading measure. Left alone the channel stretched to
            whatever the window gave it, which on a wide screen is a metre of
            whitespace with a sentence in the corner.
            One wrapper, not a cap on each child: `ch` scales with font-size, so
            per-child caps gave the small-text lines a narrower box and every
            other row a different left edge. */}
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-3">
          <div className="mx-auto max-w-[68ch]">
            {refusal && (
              <div className="mb-2 rounded-otto-sm border border-[var(--status-red)] bg-card px-2.5 py-2">
                <span className="flex items-center gap-2">
                  <TriangleAlertIcon className="size-4 shrink-0 text-[var(--status-red)]" />
                  Refused. {refusal}
                </span>
              </div>
            )}

            {draft && (
              <DraftCard
                draft={draft}
                busy={adopt.isPending}
                onDecide={(decision) =>
                  void run(
                    () => adopt.mutateAsync({ spawn: draft.id, decision }),
                    "The proposal was not decided.",
                  )
                }
              />
            )}

            {ownerRequest && (
              <>
                <p className="pb-2 text-muted-foreground text-sm">
                  Money verbs never resolve in a bot&rsquo;s channel. The same request appears there
                  with no buttons, and that discontinuity is the capability boundary.
                </p>
                <OwnerCard
                  request={ownerRequest}
                  canSign={fleet.data?.canSign ?? false}
                  busy={sign.isPending}
                  onDecide={(approved) =>
                    void run(
                      () =>
                        sign.mutateAsync({
                          approval: ownerRequest.id,
                          approved,
                          confirmed: approved,
                        }),
                      "The signature was refused.",
                    )
                  }
                />
              </>
            )}

            {!draft && detail.data && (
              <>
                {detail.data.pending.length > 0 && !ownerRequest && (
                  <>
                    <h2 className="pb-1.5 text-muted-foreground text-sm">Waiting on you</h2>
                    {detail.data.pending.map((approval) => (
                      <ApprovalCard
                        key={approval.id}
                        approval={approval}
                        busy={answer.isPending}
                        onAnswer={(choice, approved) =>
                          void handleAnswer(approval, choice, approved)
                        }
                      />
                    ))}
                  </>
                )}

                <p className="pt-2 text-muted-foreground text-sm">{detail.data.mission}</p>

                {detail.data.channel.length === 0 ? (
                  <p className="pt-3 text-muted-foreground text-sm">Nothing said yet.</p>
                ) : (
                  detail.data.channel.map((message) => (
                    <ChannelLine key={message.seq} {...message} />
                  ))
                )}
              </>
            )}
          </div>
        </div>
      </div>

      {/* ── dock ───────────────────────────────────────────────── */}
      <div className="hidden min-h-0 flex-col border-l border-border xl:flex">
        <TopBand />
        {detail.data && !draft && (
          <Dock
            bot={detail.data}
            tab={dockTab}
            setTab={setDockTab}
            onSelect={(target) => setSelection({ kind: "bot", slug: target })}
          />
        )}
      </div>
    </div>
  );
}
