/**
 * Bots page — what the operator must be able to see and do.
 *
 * The rules worth pinning are the ones a redesign would quietly break: what
 * needs a person comes first, an owner-only verb offers no approve button, and
 * a refused verdict says why instead of appearing to succeed.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { BotsPage } from "./BotsPage";

const listBots = vi.fn();
const getBot = vi.fn();
const answerApproval = vi.fn();
const signOwnerRequest = vi.fn();
const adoptDraft = vi.fn();
const getWorkspace = vi.fn();

vi.mock("@/lib/botsApi", () => ({
  listBots: (...args: unknown[]) => listBots(...args),
  getBot: (...args: unknown[]) => getBot(...args),
  answerApproval: (...args: unknown[]) => answerApproval(...args),
  signOwnerRequest: (...args: unknown[]) => signOwnerRequest(...args),
  adoptDraft: (...args: unknown[]) => adoptDraft(...args),
  getWorkspace: (...args: unknown[]) => getWorkspace(...args),
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

function ownerRequest(overrides: Record<string, unknown> = {}) {
  return {
    id: "b".repeat(32),
    botId: "c".repeat(32),
    bot: "treasurer",
    verb: "spend",
    question: "Market data top-up",
    options: [],
    evidence: { amount: "$40.00", provider: "polygon" },
    actionHash: "71bd04e9c3a5",
    digest: "8f21aa77bbcc",
    policyVersion: "v4",
    runId: "d".repeat(32),
    runVersion: 3,
    expiresAt: null,
    createdAt: Math.floor(Date.now() / 1000) - 60,
    ...overrides,
  };
}

function botDraft(overrides: Record<string, unknown> = {}) {
  return {
    id: "e".repeat(32),
    slug: "prospector",
    parent: "scout",
    rationale: "Nobody is indexing the new retrieval sets.",
    allowance: 40,
    definition: { workload: "workloads.research:Prospect", harness: "claude-sdk" },
    createdAt: Math.floor(Date.now() / 1000) - 300,
    ...overrides,
  };
}

/** A fleet with nothing waiting on anyone, for tests that add one thing. */
function fleet(overrides: Record<string, unknown> = {}) {
  return {
    running: true,
    counts: {},
    bots: [bot("quiet")],
    pending: [],
    owner: [],
    canSign: true,
    drafts: [],
    ...overrides,
  };
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <BotsPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return client;
}

beforeEach(() => {
  vi.clearAllMocks();
  listBots.mockResolvedValue(
    fleet({
      counts: { waiting_human: 1, scheduled: 1 },
      bots: [
        bot("quiet"),
        bot("scout", { status: "waiting_human", needsHuman: true, dueIn: null }),
      ],
    }),
  );
  getBot.mockResolvedValue(detail());
  answerApproval.mockResolvedValue({ ok: true });
  signOwnerRequest.mockResolvedValue({ ok: true });
  adoptDraft.mockResolvedValue({ ok: true });
  getWorkspace.mockResolvedValue({
    running: true,
    root: "/tmp/bots/scout",
    path: "",
    entries: [
      { name: "charter.md", path: "charter.md", dir: false, bytes: 220, modifiedAt: 0 },
      { name: "reports", path: "reports", dir: true, bytes: null, modifiedAt: 0 },
    ],
    text: null,
    bytes: null,
    reason: null,
  });
});

describe("BotsPage", () => {
  it("says the loop is not running rather than failing", async () => {
    listBots.mockResolvedValue(
      fleet({
        running: false,
        reason: "Bot mode has not been started on this machine.",
        bots: [],
      }),
    );
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
    const everyone = screen.getByText("Bots", { selector: "p" });
    expect(needsYou.compareDocumentPosition(everyone)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
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

  it("opens on the signature, because money outranks an iteration gate", async () => {
    listBots.mockResolvedValue(
      fleet({
        bots: [bot("scout", { status: "waiting_human", needsHuman: true })],
        owner: [ownerRequest()],
      }),
    );
    renderPage();
    expect(await screen.findByText("Needs your signature")).toBeInTheDocument();
    // Twice: the roster row so it is scannable, the card so it is unmissable.
    expect(screen.getAllByText("$40.00")).toHaveLength(2);
  });

  it("will not let a signature be given without the owner saying so", async () => {
    listBots.mockResolvedValue(fleet({ owner: [ownerRequest()] }));
    renderPage();
    const button = await screen.findByRole("button", { name: "Sign and release" });
    expect(button).toBeDisabled();

    fireEvent.click(screen.getByRole("checkbox"));
    expect(button).toBeEnabled();
    fireEvent.click(button);

    await waitFor(() => expect(signOwnerRequest).toHaveBeenCalled());
    expect(signOwnerRequest.mock.calls[0][0]).toEqual({
      approval: "b".repeat(32),
      approved: true,
      confirmed: true,
    });
  });

  it("shows the digest a signature would be bound to, elided at both ends", async () => {
    listBots.mockResolvedValue(
      fleet({ owner: [ownerRequest({ digest: "4306257cc972" + "0".repeat(40) + "e30101" })] }),
    );
    renderPage();
    // Signing a fingerprint you were never shown is signing a blank — but a
    // 64-character hash wrapped over two lines is one nobody actually reads.
    // Both ends, so it can be compared; never a prefix alone.
    expect(await screen.findByText("4306257cc972…0101")).toBeInTheDocument();
    expect(screen.getByText("digest")).toBeInTheDocument();
  });

  it("cannot offer a signature with no key to sign it", async () => {
    listBots.mockResolvedValue(fleet({ owner: [ownerRequest()], canSign: false }));
    renderPage();
    expect(await screen.findByText(/ARMY_BROKER_KEY/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign and release" })).not.toBeInTheDocument();
    // Refusing is still possible; it needs no grant.
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("refusing an owner request needs no confirmation, only a signature does", async () => {
    listBots.mockResolvedValue(fleet({ owner: [ownerRequest()] }));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Refuse" }));
    await waitFor(() => expect(signOwnerRequest).toHaveBeenCalled());
    expect(signOwnerRequest.mock.calls[0][0]).toMatchObject({ approved: false, confirmed: false });
  });

  it("shows a proposed bot's definition, not only its pitch", async () => {
    listBots.mockResolvedValue(fleet({ drafts: [botDraft()] }));
    renderPage();
    expect(
      await screen.findByText("Nobody is indexing the new retrieval sets."),
    ).toBeInTheDocument();
    // The definition is what would actually be created, so it is shown in full.
    expect(screen.getByText(/workload: workloads.research:Prospect/)).toBeInTheDocument();
    expect(screen.getByText(/40 iterations carved from scout/)).toBeInTheDocument();
  });

  it("separates creating a bot from switching it on", async () => {
    // The store creates the child in DRAFT even when the proposal is approved:
    // approving ten proposals in a row must not have started ten bots. The two
    // buttons are that distinction, so they cannot send the same thing.
    listBots.mockResolvedValue(fleet({ drafts: [botDraft()] }));
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Keep as draft" }));
    await waitFor(() => expect(adoptDraft).toHaveBeenCalled());
    expect(adoptDraft.mock.calls[0][0]).toEqual({ spawn: "e".repeat(32), decision: "draft" });
  });

  it("activates a proposed bot through the store that enforces the caps", async () => {
    listBots.mockResolvedValue(fleet({ drafts: [botDraft()] }));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Activate" }));
    await waitFor(() => expect(adoptDraft).toHaveBeenCalled());
    expect(adoptDraft.mock.calls[0][0]).toEqual({ spawn: "e".repeat(32), decision: "activate" });
  });

  it("says why an activation was refused rather than looking like it worked", async () => {
    adoptDraft.mockResolvedValue({ ok: false, reason: "scout already has 3 children" });
    listBots.mockResolvedValue(fleet({ drafts: [botDraft()] }));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Activate" }));
    expect(await screen.findByText(/already has 3 children/)).toBeInTheDocument();
  });

  it("moves on when the selected row disappears underneath it", async () => {
    // The roster polls, so a row can vanish without this page having done
    // anything — someone answered from the CLI, or a tick expired it. A
    // selection left pointing at it renders an empty middle column, which
    // reads as the page having broken rather than as the work being done.
    listBots.mockResolvedValue(
      fleet({
        bots: [bot("scout", { status: "waiting_human", needsHuman: true })],
        owner: [ownerRequest()],
      }),
    );
    const client = renderPage();
    await screen.findByText("Needs your signature");

    listBots.mockResolvedValue(
      fleet({ bots: [bot("scout", { status: "waiting_human", needsHuman: true })] }),
    );
    await act(() => client.invalidateQueries({ queryKey: ["bots"] }));

    expect(await screen.findByText("Merge the candidate patch?")).toBeInTheDocument();
    expect(screen.queryByText("Needs your signature")).not.toBeInTheDocument();
  });

  it("browses the real workspace instead of naming files it assumes are there", async () => {
    renderPage();
    // The listing comes from the control plane, which reads the directory. An
    // earlier cut hardcoded charter/runbook/lessons, which looked like a file
    // browser and was a picture of one — it would have shown those three names
    // for a bot whose workspace was empty, or missing.
    expect(await screen.findByText("charter.md")).toBeInTheDocument();
    expect(screen.getByText("reports")).toBeInTheDocument();
    expect(getWorkspace).toHaveBeenCalledWith("scout", "", false);
  });

  it("opens a file from the workspace and previews markdown", async () => {
    renderPage();
    fireEvent.click(await screen.findByText("charter.md"));
    await waitFor(() => expect(getWorkspace).toHaveBeenCalledWith("scout", "charter.md", true));
  });

  it("links an iteration to the session it actually ran in", async () => {
    // "I can't check the running harness behind" — a bot's body is an ordinary
    // conversation, so the ledger links to the real chat page rather than
    // paraphrasing what happened there.
    getBot.mockResolvedValue(
      detail({
        pending: [],
        runs: [
          {
            id: "f".repeat(32),
            state: "continue",
            outcome: "work_done",
            reason: null,
            at: Math.floor(Date.now() / 1000),
            sessionId: "conv_abc123",
          },
        ],
      }),
    );
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Runs" }));
    const link = await screen.findByRole("link", { name: /open session/ });
    expect(link).toHaveAttribute("href", "/c/conv_abc123");
  });

  it("offers the live body from the titlebar, and hides it when there is none", async () => {
    getBot.mockResolvedValue(detail({ pending: [], runs: [] }));
    renderPage();
    await screen.findByText("Watch the eval set");
    expect(screen.queryByRole("link", { name: "Open session" })).not.toBeInTheDocument();
  });

  it("says why the system paused a bot, where the roster shows it", async () => {
    listBots.mockResolvedValue(
      fleet({
        bots: [
          bot("stopped", {
            status: "inactive",
            dueIn: null,
            pausedReason: "budget exhausted — refill it with `army bots budget stopped --grant N`",
          }),
        ],
      }),
    );
    renderPage();
    expect(await screen.findByText(/budget exhausted/)).toBeInTheDocument();
  });
});
