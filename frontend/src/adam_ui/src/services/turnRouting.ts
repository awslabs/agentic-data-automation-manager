//  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
//  SPDX-License-Identifier: Apache-2.0

/** Which conversation a running turn's live events belong on screen with.
 *
 * The page shows one conversation while a turn may be running in another: the user can switch
 * away mid-turn. A live event is shown only while its turn's conversation is the one on screen;
 * the server stores every event regardless. Coming back to it mid-turn, the page loads what was
 * stored and keeps showing live events, and when the turn ends it reloads the conversation once
 * more, since events that arrived during the switch were stored but not shown.
 */
export class TurnRouting {
  private viewing: string | null = null;
  private running: string | null = null;
  private missed = false;

  /** The conversation on screen changed. */
  view(conversationId: string | null): void {
    if (this.running !== null && this.viewing === this.running && conversationId !== this.running) {
      this.missed = true;
    }
    this.viewing = conversationId;
  }

  /** A turn started in a conversation; only one runs at a time. */
  start(conversationId: string): void {
    this.running = conversationId;
    this.missed = false;
  }

  /** Whether a conversation is the one on screen. */
  shows(conversationId: string): boolean {
    return this.viewing === conversationId;
  }

  /** Whether a live event of the running turn goes on screen now. */
  showsLiveEvents(): boolean {
    return this.running !== null && this.viewing === this.running;
  }

  /** The turn ended. True when its conversation is on screen but some of its events were not. */
  finish(): boolean {
    const reload = this.missed && this.viewing === this.running;
    this.running = null;
    this.missed = false;
    return reload;
  }
}
