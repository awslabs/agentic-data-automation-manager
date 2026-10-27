#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""boto3's Lambda client, routed in-process to the real handlers (Lambda has no local equivalent)."""

import io
import json
import os
import threading
from contextlib import contextmanager


class LambdaRouter:
    """boto3's lambda client, routed in-process to the real handlers.

    `environments` optionally gives a function its own environment variables for the
    length of its invocation, as each Lambda has its own; invocations that use one are
    serialised, because the process has a single environment."""

    _lock = threading.RLock()

    def __init__(self, routes, environments=None):
        self.routes = routes
        self.environments = environments or {}
        self.invoked = []

    @contextmanager
    def _environment(self, function_name):
        env = self.environments.get(function_name)
        if env is None:
            yield
            return
        with self._lock:
            saved = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                yield
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def invoke(self, FunctionName, InvocationType, Payload):
        self.invoked.append(FunctionName)
        with self._environment(FunctionName):
            return self._invoke(FunctionName, InvocationType, Payload)

    def _invoke(self, FunctionName, InvocationType, Payload):
        if InvocationType == "Event":
            # Lambda accepts an async invoke at once; the handler's outcome never reaches the caller.
            try:
                self.routes[FunctionName](json.loads(Payload), None)
            except Exception:  # noqa: BLE001, S110 - an async invoke's failure is the function's alone
                pass
            return {"StatusCode": 202}
        try:
            result = self.routes[FunctionName](json.loads(Payload), None)
        except Exception as e:  # noqa: BLE001 - mimics Lambda, which turns any unhandled exception into a FunctionError
            return {"FunctionError": "Unhandled",
                    "Payload": io.BytesIO(json.dumps({"errorMessage": str(e),
                                                      "errorType": type(e).__name__}).encode())}
        return {"Payload": io.BytesIO(json.dumps(result).encode())}
