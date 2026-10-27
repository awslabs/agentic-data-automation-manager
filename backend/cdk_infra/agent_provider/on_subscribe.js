//  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
//  SPDX-License-Identifier: Apache-2.0

import { util } from "@aws-appsync/utils";

// Channels are /agent/{user sub}/{session id}. A caller may subscribe only to
// channels under their own sub, so knowing (or guessing) someone else's session
// id gets them nothing. Wildcard subscriptions end in a wildcard segment and
// therefore never equal a sub.
export function onSubscribe(ctx) {
  const segments = ctx.info.channel.path.split("/").filter((s) => s.length > 0);
  const [namespace, owner, session, ...rest] = segments;
  const caller = ctx.identity && ctx.identity.sub;

  if (namespace !== "agent" || !owner || !session || rest.length > 0) {
    util.unauthorized();
  }
  if (!caller || owner !== caller) {
    util.unauthorized();
  }
}
