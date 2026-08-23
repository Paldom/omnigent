/**
 * Bots (`/bots`) — the roster, one bot's channel, and its dock.
 *
 * Follows `plan-2/ui-mock/bots.html`: a roster column that leads with what
 * needs a person, a channel in the middle with pending approvals pinned above
 * the stream, and a right dock over Files / Runs / Setup. The app's own
 * sidebar supplies the nav, so this page is the mock's remaining three
 * columns rather than a second shell.
 *
 * Two conventions from the design system, both load-bearing and both easy to
 * lose: status is an 8px disc plus a word in body colour — never coloured
 * text, never a fill behind type, never a border on one side of a card — and
 * there are exactly two type steps, because that is all `index.css` ships.
 */

import { useEffect, useMemo, useState } from "react";
import { BotIcon, FileTextIcon, FolderIcon, Loader2Icon, TriangleAlertIcon } from "lucide-react";

import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { useAnswerApproval, useBot, useBots } from "@/hooks/useBots";
import type { BotApproval, BotDetail, BotStatus, BotSummary } from "@/lib/botsApi";
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

type DockTab = "files" | "runs" | "setup";

const DOCK_TABS: { value: DockTab; label: string }[] = [
  { value: "files", label: "Files" },
  { value: "runs", label: "Runs" },
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

function RosterRow({
  bot,
  active,
  onSelect,
}: {
  bot: BotSummary;
  active: boolean;
  onSelect: () => void;
}) {
  const when = bot.dueIn == null ? "—" : bot.dueIn === 0 ? "due" : short(bot.dueIn);
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
      <Disc status={bot.status} />
      <span className="min-w-0">
        <span
          className={cn(
            "block truncate leading-5",
            active && "text-[var(--sidebar-active-foreground)]",
          )}
        >
          {bot.slug} <span className="text-muted-foreground text-sm">{bot.status}</span>
        </span>
        <span className="block truncate text-muted-foreground text-sm">{reasonFor(bot)}</span>
      </span>
      <span className="font-mono text-muted-foreground text-sm whitespace-nowrap">{when}</span>
    </button>
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
          <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-sm">
            {evidence.map(([key, value]) => (
              <div key={key} className="contents">
                <dt className="text-muted-foreground">{key}</dt>
                <dd className="m-0 font-mono break-all">{String(value)}</dd>
              </div>
            ))}
          </dl>
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

function Dock({
  bot,
  tab,
  setTab,
}: {
  bot: BotDetail;
  tab: DockTab;
  setTab: (tab: DockTab) => void;
}) {
  // The tab lives on the page, not here. Owning it locally meant the operator's
  // choice was lost every time the poll briefly cleared `detail.data` and this
  // unmounted — a panel that resets itself under you while you read it.
  return (
    <div className="flex min-h-0 flex-col border-l border-border">
      <div className="flex items-center gap-1 border-b border-border px-2 py-1.5">
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

      <div className="min-h-0 flex-1 overflow-y-auto px-2.5 py-2">
        {tab === "files" && (
          <>
            <p className="m-0 font-mono text-muted-foreground text-sm">
              {bot.workspace ?? `~/bots/${bot.slug}`}
            </p>
            {["charter.md", "runbook.md", "lessons.md"].map((name) => (
              <div key={name} className="flex items-center gap-2 py-1">
                <FileTextIcon className="size-3.5 shrink-0 self-center text-muted-foreground" />
                <span className="truncate font-mono text-sm">{name}</span>
              </div>
            ))}
            <div className="flex items-center gap-2 py-1">
              <FolderIcon className="size-3.5 shrink-0 self-center text-muted-foreground" />
              <span className="truncate font-mono text-sm">reports/</span>
            </div>
            <p className="mt-2.5 text-muted-foreground text-sm">
              The content lives in git; <code className="font-mono">bot_docs</code> only indexes it,
              so history and review stay where they already work.
            </p>
          </>
        )}

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
                </span>
              </div>
            ))}
            <p className="mt-2.5 text-muted-foreground text-sm">
              <code className="font-mono">next_due_at</code> is null while blocked, so a waiting bot
              costs nothing. Answering writes the verdict and the successor wake in one transaction.
            </p>
          </>
        )}

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
    </div>
  );
}

export function BotsPage() {
  const fleet = useBots();
  const [selected, setSelected] = useState<string | null>(null);
  const [dockTab, setDockTab] = useState<DockTab>("files");
  const detail = useBot(selected);
  const answer = useAnswerApproval(selected);
  const [refusal, setRefusal] = useState<string | null>(null);

  // Memoised: `?? []` allocates a fresh array on every render, which would
  // re-run the selection effect and the split on every poll tick.
  const rows = useMemo(() => fleet.data?.bots ?? [], [fleet.data]);

  // Select whatever needs a person first, then the first row. Re-running only
  // when the selection is empty keeps a chosen bot chosen while the roster
  // polls underneath it.
  useEffect(() => {
    if (selected || rows.length === 0) return;
    setSelected((rows.find((bot) => bot.needsHuman) ?? rows[0]).slug);
  }, [rows, selected]);

  const [needsYou, quiet] = useMemo(() => {
    const needing = rows.filter((bot) => bot.needsHuman);
    return [needing, rows.filter((bot) => !bot.needsHuman)];
  }, [rows]);

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
    // The three columns scroll independently, so this cannot be a PageScroll —
    // but it owes the same debt: the AppShell header is absolute and would
    // otherwise sit on top of the first row of every column, swallowing clicks
    // on the roster and the dock tabs. Same variables, applied to the grid.
    <div
      className="grid h-full min-h-0 grid-cols-[264px_minmax(0,1fr)] xl:grid-cols-[264px_minmax(0,1fr)_320px]"
      style={{
        paddingTop: "calc(var(--omnigent-header-height) + var(--omnigent-inset-top))",
        paddingBottom: "var(--omnigent-inset-bottom)",
      }}
    >
      {/* ── roster ─────────────────────────────────────────────── */}
      <div className="flex min-h-0 flex-col overflow-y-auto border-r border-border bg-sidebar px-3 pb-3">
        <div className="flex items-baseline justify-between px-2 pt-3.5 pb-1.5">
          <span className="flex items-center gap-2 font-medium">
            <BotIcon className="size-3.5 text-muted-foreground" />
            Bots
          </span>
          <span className="text-muted-foreground text-sm">{rows.length}</span>
        </div>

        {needsYou.length > 0 && (
          <>
            <p className="px-2 pt-2.5 pb-1 text-muted-foreground text-sm">Needs you</p>
            {needsYou.map((bot) => (
              <RosterRow
                key={bot.slug}
                bot={bot}
                active={bot.slug === selected}
                onSelect={() => setSelected(bot.slug)}
              />
            ))}
          </>
        )}

        <p className="px-2 pt-2.5 pb-1 text-muted-foreground text-sm">Bots</p>
        {quiet.map((bot) => (
          <RosterRow
            key={bot.slug}
            bot={bot}
            active={bot.slug === selected}
            onSelect={() => setSelected(bot.slug)}
          />
        ))}
        {rows.length === 0 && (
          <p className="px-2 text-muted-foreground text-sm">
            No bots yet. <span className="font-mono">army bots example</span>
          </p>
        )}
      </div>

      {/* ── channel ────────────────────────────────────────────── */}
      <div className="flex min-h-0 flex-col">
        {detail.data && (
          <>
            <div className="flex items-baseline gap-2 border-b border-border px-4 py-2.5">
              <h1 className="font-medium">{detail.data.slug}</h1>
              <span className="flex items-center gap-1.5 text-muted-foreground text-sm">
                <Disc status={detail.data.status} />
                {detail.data.status.replace(/_/g, " ")}
                {detail.data.dueIn != null && ` · due in ${short(detail.data.dueIn)}`}
              </span>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
              {refusal && (
                <div className="mb-2 rounded-otto-sm border border-[var(--status-red)] bg-card px-2.5 py-2">
                  <span className="flex items-center gap-2">
                    <TriangleAlertIcon className="size-4 text-[var(--status-red)]" />
                    Refused. {refusal}
                  </span>
                </div>
              )}

              {detail.data.pending.length > 0 && (
                <>
                  <h2 className="pb-1.5 text-muted-foreground text-sm">Waiting on you</h2>
                  {detail.data.pending.map((approval) => (
                    <ApprovalCard
                      key={approval.id}
                      approval={approval}
                      busy={answer.isPending}
                      onAnswer={(choice, approved) => void handleAnswer(approval, choice, approved)}
                    />
                  ))}
                </>
              )}

              <p className="pt-2 text-muted-foreground text-sm">{detail.data.mission}</p>

              {detail.data.channel.length === 0 ? (
                <p className="pt-3 text-muted-foreground text-sm">Nothing said yet.</p>
              ) : (
                detail.data.channel.map((message) => <ChannelLine key={message.seq} {...message} />)
              )}
            </div>
          </>
        )}
      </div>

      {/* ── dock ───────────────────────────────────────────────── */}
      <div className="hidden min-h-0 xl:block">
        {detail.data && <Dock bot={detail.data} tab={dockTab} setTab={setDockTab} />}
      </div>
    </div>
  );
}
