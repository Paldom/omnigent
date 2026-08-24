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
const sayToBot = vi.fn();
const getScreen = vi.fn();
const setWheel = vi.fn();
const reactToMessage = vi.fn();

vi.mock("@/lib/botsApi", async () => ({
  listBots: (...args: unknown[]) => listBots(...args),
  getBot: (...args: unknown[]) => getBot(...args),
  answerApproval: (...args: unknown[]) => answerApproval(...args),
  signOwnerRequest: (...args: unknown[]) => signOwnerRequest(...args),
  adoptDraft: (...args: unknown[]) => adoptDraft(...args),
  getWorkspace: (...args: unknown[]) => getWorkspace(...args),
  sayToBot: (...args: unknown[]) => sayToBot(...args),
  getScreen: (...args: unknown[]) => getScreen(...args),
  setWheel: (...args: unknown[]) => setWheel(...args),
  reactToMessage: (...args: unknown[]) => reactToMessage(...args),
  // Not a mock: the vocabulary is the point of the feature, and a test that
  // invented its own would not notice a tick being added to the real one.
  MARKS: ((await vi.importActual("@/lib/botsApi")) as { MARKS: unknown }).MARKS,
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
    lineage: {
      depth: 0,
      maxDepth: 2,
      maxFanout: 3,
      parent: null,
      siblings: [],
      children: [],
      cascades: false,
    },
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
    driving: {},
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
  sayToBot.mockResolvedValue({ ok: true });
  setWheel.mockResolvedValue({ ok: true });
  reactToMessage.mockResolvedValue({ ok: true });
  getScreen.mockResolvedValue({
    ok: true,
    dataUrl: "data:image/jpeg;base64,AAAA",
    url: "https://www.kraken.com/features/fee-schedule",
    title: "Fee schedule",
  });
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

  it("shows the chain a bot sits in, and the caps that bound it", async () => {
    getBot.mockResolvedValue(
      detail({
        pending: [],
        lineage: {
          depth: 1,
          maxDepth: 2,
          maxFanout: 3,
          parent: { slug: "scout", status: "running", depth: 0, remaining: 63 },
          siblings: [{ slug: "sibling-a", status: "scheduled", depth: 1, remaining: 10 }],
          children: [{ slug: "grandchild", status: "manual", depth: 2, remaining: 5 }],
          cascades: true,
        },
      }),
    );
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Lineage" }));

    expect(await screen.findByText("parent")).toBeInTheDocument();
    expect(screen.getByText("grandchild")).toBeInTheDocument();
    expect(screen.getByText("sibling")).toBeInTheDocument();
    expect(screen.getByText(/1 of 2/)).toBeInTheDocument();
    // The one action with a blast radius larger than the row you clicked.
    expect(screen.getByText(/Retiring this retires its child/)).toBeInTheDocument();
  });

  it("says a bot at the depth cap cannot spawn again", async () => {
    getBot.mockResolvedValue(
      detail({
        pending: [],
        lineage: {
          depth: 2,
          maxDepth: 2,
          maxFanout: 3,
          parent: { slug: "scout", status: "running", depth: 1, remaining: 5 },
          siblings: [],
          children: [],
          cascades: false,
        },
      }),
    );
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Lineage" }));
    expect(await screen.findByText(/it cannot spawn again/)).toBeInTheDocument();
  });

  it("says plainly when a bot is in no chain at all", async () => {
    getBot.mockResolvedValue(detail({ pending: [] }));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Lineage" }));
    expect(await screen.findByText(/A person created it directly/)).toBeInTheDocument();
  });

  it("offsets the status disc only where the text is stacked", async () => {
    // The disc used to carry `mt-[7px]` itself. That is what it needs beside
    // the first line of a two-line roster row, and 3.5px of wrongness inside
    // the centred titlebar — the disc sat visibly below the bot's name.
    renderPage();
    await screen.findByText("Merge the candidate patch?");

    const discs = Array.from(document.querySelectorAll("span[aria-hidden]")).filter((el) =>
      el.className.includes("rounded-full"),
    );
    const inRoster = discs.filter((el) => el.closest("button[aria-current]"));
    expect(inRoster.length).toBeGreaterThan(0);
    for (const disc of inRoster) expect(disc.className).toContain("mt-[7px]");

    // The titlebar's disc is a sibling of the h1's parent row, not inside it.
    const bar = screen.getByRole("heading", { level: 1 }).parentElement!;
    const inTitlebar = Array.from(bar.querySelectorAll("span[aria-hidden]")).filter((el) =>
      el.className.includes("rounded-full"),
    );
    expect(inTitlebar.length).toBe(1);
    expect(inTitlebar[0].className).not.toContain("mt-[");
  });

  it("lets you say something to a bot, which is not answering it", async () => {
    // HITL was approve-or-deny and nothing else, which makes a bot a vending
    // machine: you may accept what it offers or refuse it, and you may not ask
    // it a question or correct a wrong assumption.
    renderPage();
    const box = await screen.findByPlaceholderText(/Say something to scout/);
    fireEvent.change(box, { target: { value: "the fee is per side, not round trip" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(sayToBot).toHaveBeenCalled());
    expect(sayToBot.mock.calls[0][0]).toEqual({
      bot: "scout",
      text: "the fee is per side, not round trip",
    });
    // The open question is untouched: talking is not approving.
    expect(answerApproval).not.toHaveBeenCalled();
    expect(screen.getByText("Merge the candidate patch?")).toBeInTheDocument();
  });

  it("says where the message will land, because that differs", async () => {
    getBot.mockResolvedValue(detail({ status: "running", pending: [] }));
    renderPage();
    expect(await screen.findByText(/reaches the running iteration/)).toBeInTheDocument();

    getBot.mockResolvedValue(detail({ status: "backing_off", pending: [] }));
    renderPage();
    expect(await screen.findAllByText(/reads this first when it next wakes/)).not.toHaveLength(0);
  });

  it("Enter sends and Shift+Enter does not", async () => {
    // A paragraph of correction is common here, and losing one to a stray
    // Enter is the thing people would remember.
    renderPage();
    const box = await screen.findByPlaceholderText(/Say something to scout/);
    fireEvent.change(box, { target: { value: "line one" } });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(sayToBot).not.toHaveBeenCalled();

    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(sayToBot).toHaveBeenCalled());
  });

  it("offers no composer for a draft, which has no channel yet", async () => {
    listBots.mockResolvedValue(fleet({ drafts: [botDraft()] }));
    renderPage();
    await screen.findByText(/Nobody is indexing/);
    expect(screen.queryByPlaceholderText(/Say something/)).not.toBeInTheDocument();
  });

  it("shows the browser the bot is actually driving", async () => {
    getBot.mockResolvedValue(detail({ pending: [] }));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Screen" }));

    const shot = await screen.findByRole("img", { name: /scout's browser/ });
    expect(shot).toHaveAttribute("src", "data:image/jpeg;base64,AAAA");
    expect(screen.getByText(/kraken.com\/features\/fee-schedule/)).toBeInTheDocument();
  });

  it("says plainly that the screen is not a network monitor", async () => {
    // A vendor CLI can fetch a URL with its own tooling and nothing here sees
    // it. Implying otherwise would be the convincing fake worth avoiding.
    getBot.mockResolvedValue(detail({ pending: [] }));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Screen" }));
    expect(await screen.findByText(/not a network monitor/)).toBeInTheDocument();
  });

  it("takes the wheel, and says the bot is refused rather than queued", async () => {
    getBot.mockResolvedValue(detail({ pending: [] }));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Screen" }));
    fireEvent.click(await screen.findByRole("button", { name: "Take the wheel" }));

    await waitFor(() => expect(setWheel).toHaveBeenCalled());
    expect(setWheel.mock.calls[0][0]).toEqual({ bot: "scout", take: true });
  });

  it("offers to hand back a wheel somebody already holds", async () => {
    listBots.mockResolvedValue(
      fleet({
        bots: [bot("scout", { status: "running", needsHuman: false })],
        driving: {
          scout: { driver: "human:channel", since: 0, until: 9e9, reason: null },
        },
      }),
    );
    getBot.mockResolvedValue(detail({ pending: [] }));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Screen" }));

    expect(await screen.findByRole("button", { name: "Hand back" })).toBeInTheDocument();
    expect(screen.getByText(/refused — not queued/)).toBeInTheDocument();
  });

  it("marks a message, and says the mark is not an approval", async () => {
    // The whole reason this feature is safe: an approval binds to an action
    // hash, a policy version and a run version. Acknowledgement binds to none
    // of them, and the row has to say so — a control whose meaning must be
    // inferred is one that will be misread.
    getBot.mockResolvedValue(
      detail({
        pending: [],
        channel: [
          {
            id: "d".repeat(32),
            seq: 1,
            kind: "report",
            author: "scout",
            body: "Tier 1 spot: 0.40% / 0.80%.",
            at: Date.now() / 1000 - 60,
            thread: null,
            marks: [],
          },
        ],
      }),
    );
    renderPage();
    await screen.findByText("Tier 1 spot: 0.40% / 0.80%.");

    fireEvent.click(screen.getByRole("button", { name: /I have read this/ }));

    await waitFor(() => expect(reactToMessage).toHaveBeenCalled());
    expect(reactToMessage.mock.calls[0][0]).toEqual({ message: "d".repeat(32), mark: "seen" });
    expect(screen.getByRole("button", { name: /I have read this/ })).toHaveAccessibleName(
      /not an approval/,
    );
  });

  it("offers no mark that reads as a tick", async () => {
    // Beside a pending question, a tick is a verdict to everyone who has ever
    // used chat software. The vocabulary is the safety property.
    getBot.mockResolvedValue(
      detail({
        pending: [],
        channel: [
          {
            id: "d".repeat(32),
            seq: 1,
            kind: "report",
            author: "scout",
            body: "a claim",
            at: 0,
            thread: null,
            marks: [],
          },
        ],
      }),
    );
    renderPage();
    await screen.findByText("a claim");

    for (const forbidden of [/approve/i, /accept/i, /^ok$/i, /confirm/i]) {
      expect(screen.queryByRole("button", { name: forbidden })).not.toBeInTheDocument();
    }
  });

  it("shows a mark somebody already made, labelled as acknowledgement", async () => {
    getBot.mockResolvedValue(
      detail({
        pending: [],
        channel: [
          {
            id: "d".repeat(32),
            seq: 1,
            kind: "report",
            author: "scout",
            body: "a claim",
            at: 0,
            thread: null,
            marks: ["concern"],
          },
        ],
      }),
    );
    renderPage();

    expect(await screen.findByText(/acknowledged — not an approval/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /uneasy/ })).toHaveAttribute("aria-pressed", "true");
  });

  it("shows a running bot working, with a way into the session", async () => {
    // A channel that only shows finished messages goes silent for the minutes
    // an iteration takes, and silence reads as broken.
    listBots.mockResolvedValue(
      fleet({ bots: [bot("scout", { status: "running", needsHuman: false })] }),
    );
    getBot.mockResolvedValue(
      detail({
        pending: [],
        status: "running",
        runs: [
          {
            id: "be088b426a71bbbb",
            state: "collecting",
            outcome: null,
            reason: null,
            at: Date.now() / 1000 - 45,
            sessionId: "conv_live",
          },
        ],
      }),
    );
    renderPage();

    expect(await screen.findByText("working")).toBeInTheDocument();
    expect(screen.getByText("be088b426a71")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /watch it/ })).toHaveAttribute("href", "/c/conv_live");
  });

  it("does not claim a bot is working when it is not", async () => {
    getBot.mockResolvedValue(detail({ pending: [], status: "waiting_human" }));
    renderPage();

    await screen.findByText(/Merge the candidate patch|Watch/);
    expect(screen.queryByText("working")).not.toBeInTheDocument();
  });

  it("shows what the bot made its browser do, newest first", async () => {
    // A frame says where the browser is. Watching a bot work means seeing the
    // steps — and a wrong action is only explicable if the attempt was written
    // down at all.
    getScreen.mockResolvedValue({
      ok: true,
      dataUrl: "data:image/jpeg;base64,AAAA",
      url: "https://www.kraken.com/features/fee-schedule",
      title: "Fee schedule",
      trail: [
        {
          action: "navigate",
          target: "https://www.kraken.com/features/fee-schedule",
          ok: true,
          url: "",
          error: "",
        },
        { action: "click", target: "ref 35", ok: true, url: "", error: "" },
      ],
    });
    getBot.mockResolvedValue(detail({ pending: [] }));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Screen" }));

    const steps = await screen.findAllByRole("listitem");
    expect(steps[0]).toHaveTextContent("click");
    expect(steps[1]).toHaveTextContent("navigate");
  });

  it("shows a failed browser action rather than dropping it", async () => {
    getScreen.mockResolvedValue({
      ok: true,
      dataUrl: "data:image/jpeg;base64,AAAA",
      url: "https://example.com",
      title: "Example",
      trail: [
        {
          action: "click",
          target: "ref 3",
          ok: false,
          url: "",
          error: "click failed: no such element",
        },
      ],
    });
    getBot.mockResolvedValue(detail({ pending: [] }));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Screen" }));

    expect(await screen.findByText(/no such element/)).toBeInTheDocument();
  });

  it("never shows the text a bot typed into a page", async () => {
    // The trail is rendered in a UI and screenshotted into tickets. A password
    // that reaches it is a password in a screenshot.
    getScreen.mockResolvedValue({
      ok: true,
      dataUrl: "data:image/jpeg;base64,AAAA",
      url: "https://example.com/login",
      title: "Sign in",
      trail: [{ action: "type", target: "ref 7 (text withheld)", ok: true, url: "", error: "" }],
    });
    getBot.mockResolvedValue(detail({ pending: [] }));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Screen" }));

    expect(await screen.findByText(/text withheld/)).toBeInTheDocument();
  });

  it("says a bot with no browser has not opened one, rather than erroring", async () => {
    getScreen.mockResolvedValue({ ok: false, dataUrl: null, url: "", title: "" });
    getBot.mockResolvedValue(detail({ pending: [] }));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Screen" }));
    expect(await screen.findByText(/has not opened a browser/)).toBeInTheDocument();
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
