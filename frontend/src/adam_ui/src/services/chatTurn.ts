//  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
//  SPDX-License-Identifier: Apache-2.0

// How one chat turn is waited on, apart from Amplify so the tests can drive it.

/** One event of a conversation as the agent runtime streams it. A turn is one prompt
 * ("user", seq 0) and the events of its answer; "output" or "error" ends it. */
export interface ChatEvent {
  event: string;
  data: { content?: string; tool?: string; toolUseId?: string; input?: Record<string, unknown>; output?: string; code?: string;
    reason?: string };
}

/** With no event for this long, the turn is given up on and the page reloads to show what the server stored. */
export const TURN_INACTIVITY_MS = 5 * 60 * 1000;
/** How long AppSync may take to confirm the subscription; past it the prompt is not sent. */
export const SUBSCRIBE_ACK_MS = 15_000;

/** No event arrived for TURN_INACTIVITY_MS: the turn may have ended without telling this page. */
export class TurnInactiveError extends Error {}

/** The caller's channel for the conversation. open() subscribes, calls `acknowledged` once AppSync
 * confirms the subscription, and returns a function that ends it. */
export interface TurnChannel {
  open(handlers: { next: (raw: unknown) => void; error: (err: unknown) => void; acknowledged: () => void }): () => void;
}

/** AppSync's subscription errors arrive as { errors: [{ message }] }, not as an Error. */
function streamErrorText(err: unknown): string {
  if (err instanceof Error) return err.message;
  const messages = (err as { errors?: { message?: string }[] } | null)?.errors?.map((e) => e.message).filter(Boolean);
  return messages?.length ? messages.join("; ") : JSON.stringify(err);
}

/** Subscribe, post only once AppSync has confirmed the subscription (so no event is missed), then
 * pass on the turn's events until "output" or "error" ends it. */
export async function runTurn(
  channel: TurnChannel,
  post: () => Promise<unknown>,
  onEvent: (evt: ChatEvent) => void,
  timing = { ackMs: SUBSCRIBE_ACK_MS, idleMs: TURN_INACTIVITY_MS },
): Promise<void> {
  let acknowledge = () => {};
  let ackFailed: (err: Error) => void = () => {};
  const acknowledged = new Promise<void>((resolve, reject) => { acknowledge = resolve; ackFailed = reject; });
  let settle = () => {};
  let fail: (err: Error) => void = () => {};
  const finished = new Promise<void>((resolve, reject) => { settle = resolve; fail = reject; });
  // Either may reject while the other is awaited; both are awaited below.
  acknowledged.catch(() => {});
  finished.catch(() => {});

  let ackTimer: ReturnType<typeof setTimeout> | undefined;
  let idleTimer: ReturnType<typeof setTimeout> | undefined;
  // Measured from the post, then from each event received.
  const watch = () => {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => fail(new TurnInactiveError(
      `No word from the agent for ${timing.idleMs / 60000} minutes.`)), timing.idleMs);
  };

  const stop = channel.open({
    next: (raw) => {
      const evt = (raw as { event?: ChatEvent } | null)?.event;
      if (!evt?.event) return;
      if (idleTimer !== undefined) watch();
      if (evt.event === "user") return;  // the prompt is already on screen
      onEvent(evt);
      if (evt.event === "output" || evt.event === "error") settle();
    },
    error: (err) => {
      const lost = new Error(`Lost the event stream: ${streamErrorText(err)}`);
      ackFailed(lost);
      fail(lost);
    },
    acknowledged: () => acknowledge(),
  });
  try {
    await Promise.race([acknowledged, new Promise((_, reject) => {
      ackTimer = setTimeout(() => reject(new Error(
        `The event stream did not confirm the subscription within ${timing.ackMs / 1000} s, so the prompt was not sent.`)),
      timing.ackMs);
    })]);
    watch();
    // A 202 means the runtime was slow to acknowledge: the turn may still be starting, so keep waiting.
    await Promise.all([post(), finished]);
  } finally {
    clearTimeout(ackTimer);
    clearTimeout(idleTimer);
    stop();
  }
}
