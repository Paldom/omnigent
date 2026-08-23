// TanStack Query hooks over the `/v1/bots` client: the fleet, one bot, and the
// mutation that answers a question. Mirrors `useScheduledTasks.ts`
// (invalidate-on-success).
//
// POLLING CONTRACT — read before reusing. The loop is a separate process with
// no push stream into this server, so the roster is polled. Both intervals
// only run while a component holding the hook is mounted, and the Bots page is
// route-scoped, so nothing polls in the background from elsewhere in the app.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { answerApproval, getBot, listBots, type BotDetail, type BotFleet } from "@/lib/botsApi";

/** Query key for the fleet. */
export const BOTS_KEY = ["bots"] as const;

/** Query key for one bot. */
export function botKey(slug: string): readonly unknown[] {
  return ["bot", slug];
}

/**
 * The fleet.
 *
 * Five seconds, not thirty: this is the surface an operator watches while
 * something is in flight, and a roster that lags a tick shows a bot as
 * `running` after it has already asked them a question.
 */
export function useBots() {
  return useQuery<BotFleet>({
    queryKey: BOTS_KEY,
    queryFn: listBots,
    refetchInterval: 5_000,
    staleTime: 2_000,
  });
}

/** One bot's definition, ledger, channel and open question. */
export function useBot(slug: string | null) {
  return useQuery<BotDetail & { running: boolean; reason?: string }>({
    queryKey: botKey(slug ?? ""),
    queryFn: () => getBot(slug as string),
    enabled: Boolean(slug),
    refetchInterval: 5_000,
    staleTime: 2_000,
  });
}

/**
 * How many bots are waiting on a person, for the sidebar badge.
 *
 * A slower poll than the page's, and deliberately so: the sidebar is mounted
 * everywhere, so this runs for the whole session. Thirty seconds is soon
 * enough for a badge and cheap enough to leave on. A failed request counts as
 * zero rather than surfacing — a nav badge is not the place to learn that a
 * background process is down; the page says so properly.
 */
export function useBotsNeedingYou(): number {
  const { data } = useQuery<BotFleet>({
    queryKey: BOTS_KEY,
    queryFn: listBots,
    refetchInterval: 30_000,
    staleTime: 15_000,
    retry: false,
  });
  if (!data?.running) return 0;
  return data.bots.filter((bot) => bot.needsHuman).length;
}

/**
 * Answer a question.
 *
 * Invalidates both the fleet and the bot, because a verdict moves two things:
 * the run it settles and the bot's next wake, written in the same transaction.
 */
export function useAnswerApproval(slug: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: answerApproval,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: BOTS_KEY });
      if (slug) void queryClient.invalidateQueries({ queryKey: botKey(slug) });
    },
  });
}
