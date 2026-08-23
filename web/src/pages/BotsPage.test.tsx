/**
 * Bots page — what the operator must be able to see and do.
 *
 * The rules worth pinning are the ones a redesign would quietly break: what
 * needs a person comes first, an owner-only verb offers no approve button, and
 * a refused verdict says why instead of appearing to succeed.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { BotsPage } from "./BotsPage";

const listBots = vi.fn();
const getBot = vi.fn();
const answerApproval = vi.fn();

vi.mock("@/lib/botsApi", () => ({
  listBots: (...args: unknown[]) => listBots(...args),
  getBot: (...args: unknown[]) => getBot(...args),
  answerApproval: (...args: unknown[]) => answerApproval(...args),
}));

function bot(slug: string, overrides: Record<string, unknown> = {}) {
  return {
    slug,
    title: null,
    mission: `${slug} does a thing`,
    status: "scheduled",
    dueIn: 60,
    idleStreak: 0,
    errorStreak: 0,
    lastOutcome: null,
    pausedReason: null,
    runId: null,
    harness: null,
    needsHuman: false,
    blockedUntil: null,
    wakeKind: "continuous",
    ...overrides,
  };
}

function approval(overrides: Record<string, unknown> = {}) {
  return {
    id: "a".repeat(32),
    question: "Merge the candidate patch?",
    options: ["merge", "iterate"],
    evidence: { recall: "0.78" },
    verb: "iteration_gate",
    actionHash: "9f3a12c7dead",
    policyVersion: "v4",
    runId: "r".repeat(32),
    runVersion: 7,
    requiresOwner: false,
    expiresAt: null,
    createdAt: Math.floor(Date.now() / 1000),
    ...overrides,
  };
}

function detail(overrides: Record<string, unknown> = {}) {
  return {
    running: true,
    slug: "scout",
    displayName: "scout",
    title: "Research Analyst",
    mission: "Watch the eval set",
    persona: "careful",
    workload: "army.bots.workloads.heartbeat:HeartbeatWorkload",
    harness: null,
    wake: { kind: "continuous" },
    status: "waiting_human",
    dueIn: null,
    idleStreak: 0,
    errorStreak: 0,
    pausedReason: null,
    workspace: null,
    browserProfile: null,
    runs: [],
    channel: [],
    pending: [approval()],
    ...overrides,
  };
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <BotsPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  listBots.mockResolvedValue({
    running: true,
    counts: { waiting_human: 1, scheduled: 1 },
    bots: [bot("quiet"), bot("scout", { status: "waiting_human", needsHuman: true, dueIn: null })],
    pending: [],
  });
  getBot.mockResolvedValue(detail());
  answerApproval.mockResolvedValue({ ok: true });
});

describe("BotsPage", () => {
  it("says the loop is not running rather than failing", async () => {
    listBots.mockResolvedValue({
      running: false,
      reason: "Bot mode has not been started on this machine.",
      counts: {},
      bots: [],
      pending: [],
    });
    renderPage();
    expect(
      await screen.findByText("Bot mode has not been started on this machine."),
    ).toBeInTheDocument();
    // And it says what to type, because "not started" is a first-run state.
    expect(screen.getByText(/army bots example/)).toBeInTheDocument();
  });

  it("puts what needs a person above everything else", async () => {
    renderPage();
    await screen.findByText("Needs you");
    const needsYou = screen.getByText("Needs you");
    const fleet = screen.getByText("Bots", { selector: "p" });
    expect(needsYou.compareDocumentPosition(fleet)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  });

  it("selects the bot that needs a person, not the first row", async () => {
    renderPage();
    // scout is second in the roster and the only one waiting on someone.
    await waitFor(() => expect(getBot).toHaveBeenCalledWith("scout"));
  });

  it("shows what the verdict is bound to, not only the question", async () => {
    renderPage();
    expect(await screen.findByText("Merge the candidate patch?")).toBeInTheDocument();
    // The evidence the hash covers, and the hash itself.
    expect(screen.getByText("recall")).toBeInTheDocument();
    expect(screen.getByText("0.78")).toBeInTheDocument();
    expect(screen.getByText(/action 9f3a12c7/)).toBeInTheDocument();
  });

  it("answers through the bound path with the option that was clicked", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "merge" }));
    await waitFor(() => expect(answerApproval).toHaveBeenCalled());
    expect(answerApproval.mock.calls[0][0]).toEqual({
      approval: "a".repeat(32),
      choice: "merge",
      approved: true,
    });
  });

  it("says why a verdict was refused instead of appearing to succeed", async () => {
    answerApproval.mockResolvedValue({
      ok: false,
      reason: "run moved from version 7 to 8 while this sat waiting. Re-asking.",
    });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "merge" }));
    expect(await screen.findByText(/run moved from version 7 to 8/)).toBeInTheDocument();
  });

  it("offers no approve button for an owner-only verb", async () => {
    getBot.mockResolvedValue(
      detail({
        pending: [
          approval({
            verb: "spend",
            requiresOwner: true,
            options: ["pay"],
            question: "Top up market data?",
          }),
        ],
      }),
    );
    renderPage();
    expect(await screen.findByText("Sent to the owner")).toBeInTheDocument();
    // A button guaranteed to be refused teaches that the buttons are advisory.
    expect(screen.queryByRole("button", { name: "pay" })).not.toBeInTheDocument();
    expect(screen.getByText(/never satisfies one/)).toBeInTheDocument();
  });

  it("says why the system paused a bot, where the roster shows it", async () => {
    listBots.mockResolvedValue({
      running: true,
      counts: { inactive: 1 },
      bots: [
        bot("stopped", {
          status: "inactive",
          dueIn: null,
          pausedReason: "budget exhausted — refill it with `army bots budget stopped --grant N`",
        }),
      ],
      pending: [],
    });
    renderPage();
    expect(await screen.findByText(/budget exhausted/)).toBeInTheDocument();
  });
});
