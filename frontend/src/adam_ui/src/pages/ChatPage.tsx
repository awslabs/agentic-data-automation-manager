//  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
//  SPDX-License-Identifier: Apache-2.0

import { TurnRouting } from "../services/turnRouting";
import { useEffect, useRef, useState } from "react";
import {
  Badge, Button, Code, Collapse, Group, Loader, Modal, Paper, ScrollArea, Select, Stack, Text, TextInput, UnstyledButton,
} from "@mantine/core";
import { signOut } from "aws-amplify/auth";
import { IconChevronDown, IconChevronRight, IconSend } from "@tabler/icons-react";
import { MarkdownWithTables } from "../components/MarkdownTableGrid";
import { DataChart } from "../components/DataChart";
import { detectChartableData } from "../components/chartData";
import { extractMarkdownTables } from "../components/markdownTables";
import {
  conversationMessages, errorText, listModels, sendChat, signInHasGatewayScope, TurnInactiveError, type ChatEvent,
  type ChatModel,
} from "../services/localApi";

// The last model the user chose; used only while the server still offers it.
const MODEL_KEY = "chatModel";

interface ToolStep { id?: string; tool: string; input?: Record<string, unknown>; output?: string }

interface Message {
  id: string;
  role: "user" | "assistant" | "error";
  content: string;
  steps: ToolStep[];
}

/** Gateway tool names carry their target: "adam___search_schema". */
const shortName = (tool: string) => tool.split("___").pop() || tool;

/** Folds one event of a turn into the conversation. Live events and a reopened
 * conversation's stored events go through the same function, so both look the same. */
function apply(messages: Message[], evt: ChatEvent, turnId: string): Message[] {
  const answerId = `${turnId}-answer`;
  const answer = messages.find((m) => m.id === answerId);
  const withAnswer = (update: (m: Message) => Message) =>
    answer
      ? messages.map((m) => (m.id === answerId ? update(m) : m))
      : [...messages, update({ id: answerId, role: "assistant", content: "", steps: [] })];
  // Parallel calls finish in any order, and a call's events can reach the browser out
  // of order too, so each event goes to the call it names, which whichever event comes
  // first creates. Turns stored before events carried a toolUseId go to the last call.
  const patchStep = (m: Message, id: string | undefined, patch: Partial<ToolStep>) => {
    const i = id ? m.steps.findIndex((s) => s.id === id) : m.steps.length - 1;
    if (i >= 0) return { ...m, steps: m.steps.map((s, j) => (j === i ? { ...s, ...patch } : s)) };
    return id ? { ...m, steps: [...m.steps, { id, tool: "", ...patch }] } : m;
  };

  switch (evt.event) {
    case "user":
      return [...messages, { id: `${turnId}-user`, role: "user", content: evt.data.content ?? "", steps: [] }];
    case "tool_use":
      return withAnswer((m) => evt.data.toolUseId
        ? patchStep(m, evt.data.toolUseId, { tool: evt.data.tool ?? "" })
        : { ...m, steps: [...m.steps, { tool: evt.data.tool ?? "" }] });
    case "tool_input":
      return withAnswer((m) => patchStep(m, evt.data.toolUseId, { input: evt.data.input }));
    case "tool_result":
      return withAnswer((m) => patchStep(m, evt.data.toolUseId, { output: evt.data.output }));
    case "output":
      return withAnswer((m) => ({ ...m, content: evt.data.content ?? "" }));
    case "error":
      return [...messages, { id: `${turnId}-error`, role: "error", content: evt.data.content ?? "The agent failed.", steps: [] }];
    default:
      return messages;
  }
}

function Answer({ content }: { content: string }) {
  const chartable = extractMarkdownTables(content).map((s) => s.table).find((t) => detectChartableData(t) !== null);
  return (
    <>
      <MarkdownWithTables markdown={content} />
      {chartable && <DataChart table={chartable} />}
    </>
  );
}

function ToolSteps({ steps, running }: { steps: ToolStep[]; running: boolean }) {
  const [open, setOpen] = useState(false);
  const shown = open || running;
  return (
    <>
      <UnstyledButton display="block" mb="xs" aria-expanded={shown} onClick={() => setOpen((o) => !o)}>
        <Group gap={4}>
          {shown ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
          {running && <Loader size={12} />}
          <Text size="xs" c="dimmed">{steps.length} tool{steps.length > 1 ? "s" : ""}{running ? " running" : " used"}</Text>
          {steps.map((s, i) => (
            <Badge key={i} size="xs" variant={s.output === undefined ? "filled" : "outline"}>{shortName(s.tool)}</Badge>
          ))}
        </Group>
      </UnstyledButton>
      <Collapse in={shown}>
        <Stack gap={4} mb="sm">
          {steps.map((s, i) => (
            <Paper key={i} p="xs" radius="sm" withBorder>
              <Badge size="xs" mb={4}>{shortName(s.tool)}</Badge>
              {s.input && (
                <Code block style={{ fontSize: 11 }}>
                  {typeof s.input.statement === "string" ? s.input.statement : JSON.stringify(s.input, null, 2)}
                </Code>
              )}
              {s.output !== undefined && (
                <Code block mt={4} style={{ fontSize: 11, maxHeight: 200, overflow: "auto" }}>{s.output}</Code>
              )}
            </Paper>
          ))}
        </Stack>
      </Collapse>
    </>
  );
}

interface Props {
  conversationId: string | null;
  /** Called with the new id when the first prompt of a new conversation is sent. */
  onConversationStarted: (id: string) => void;
  /** Called when the server has taken a prompt and when its turn ends, so the sidebar can refresh. */
  onConversationsChanged: () => void;
  /** Called with the conversation a turn is running in, and with null when it ends: that one
   * conversation may not be deleted meanwhile. Switching to any other stays free. */
  onTurnRunning: (conversationId: string | null) => void;
}

export function ChatPage({ conversationId, onConversationStarted, onConversationsChanged, onTurnRunning }: Props) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  // The conversation a turn is running in; one turn runs at a time.
  const [running, setRunning] = useState<string | null>(null);
  const routing = useRef(new TurnRouting());
  const busy = running !== null;
  // The running turn's progress shows only with its own conversation.
  const busyHere = busy && running === conversationId;
  // The gateway refused a sign-in that lacks its scope: only signing in again fixes that.
  const [signInStale, setSignInStale] = useState(false);
  const [models, setModels] = useState<ChatModel[] | null>(null);
  const [modelProblem, setModelProblem] = useState<string | null>(null);
  const [model, setModel] = useState<string | null>(null);
  const viewport = useRef<HTMLDivElement>(null);
  // The conversation this page started itself: its events are already on screen.
  const started = useRef<string | null>(null);
  // Leaving the chat tab unmounts the page; a turn that then goes quiet must not reload the site.
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    listModels()
      .then((served) => {
        if (served.length === 0) throw new Error("The server offers no models to chat with.");
        const saved = localStorage.getItem(MODEL_KEY);
        setModels(served);
        setModel(served.some((m) => m.id === saved) ? saved : served[0].id);
      })
      .catch((err: unknown) => setModelProblem(`Could not load the models: ${errorText(err)}`));
  }, []);

  useEffect(() => {
    routing.current.view(conversationId);
    if (!conversationId) { setMessages([]); return; }
    if (conversationId === started.current) { started.current = null; return; }
    // A slower load of a conversation opened earlier must not replace this one.
    let current = true;
    conversationMessages(conversationId)
      .then((stored) => { if (current) setMessages(stored.reduce((acc, e) => apply(acc, e, e.turn), [] as Message[])); })
      .catch((err: unknown) => { if (current) setMessages([{ id: "load", role: "error", content: errorText(err), steps: [] }]); });
    return () => { current = false; };
  }, [conversationId]);

  useEffect(() => {
    viewport.current?.scrollTo({ top: viewport.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  const send = async () => {
    const prompt = input.trim();
    if (!prompt || busy || !model) return;
    const id = conversationId ?? crypto.randomUUID();
    if (!conversationId) {
      started.current = id;
      routing.current.view(id);
      onConversationStarted(id);
    }
    const turnId = crypto.randomUUID();
    setInput("");
    routing.current.start(id);
    setRunning(id);
    onTurnRunning(id);
    setMessages((prev) => apply(prev, { event: "user", data: { content: prompt } }, turnId));
    // A live event goes on screen only while its conversation is the one shown.
    const show = (update: (prev: Message[]) => Message[]) => {
      if (routing.current.showsLiveEvents()) setMessages(update);
    };
    try {
      await sendChat(prompt, id, model, (evt) => {
        if (evt.event === "error" && evt.data.code === "token_refused") {
          // Signing in again fixes it only if this sign-in lacks the scope a new one requests;
          // a token that has it and is still refused is a configuration problem.
          void signInHasGatewayScope().then((hasScope) => {
            if (!hasScope) setSignInStale(true);
            const content = hasScope
              ? `${evt.data.content} Your sign-in carries the required scope, so this is a configuration ` +
                "problem for the administrator, not something signing in again fixes."
              : `${evt.data.content} Your sign-in predates a change to this site.`;
            show((prev) => apply(prev, { ...evt, data: { ...evt.data, content } }, turnId));
          });
          return;
        }
        show((prev) => apply(prev, evt, turnId));
      }, onConversationsChanged);
    } catch (err) {
      // The turn went quiet: a reload shows what the server stored of it and resets the page.
      if (err instanceof TurnInactiveError) {
        if (mounted.current) window.location.reload();
        return;
      }
      show((prev) => apply(prev, { event: "error", data: { content: errorText(err) } }, turnId));
    } finally {
      // Back on its conversation after switching away: what arrived meanwhile is stored, not shown.
      if (routing.current.finish()) {
        conversationMessages(id)
          .then((stored) => {
            if (routing.current.shows(id)) setMessages(stored.reduce((acc, e) => apply(acc, e, e.turn), [] as Message[]));
          })
          .catch((err: unknown) => {
            if (routing.current.shows(id)) {
              setMessages((prev) => [...prev, { id: "reload", role: "error", content: errorText(err), steps: [] }]);
            }
          });
      }
      setRunning(null);
      onTurnRunning(null);
      onConversationsChanged();
    }
  };

  return (
    <Stack h="calc(100vh - 88px)" gap="sm">
      <Group justify="flex-end">
        {modelProblem && <Text size="xs" c="red">{modelProblem}</Text>}
        {models && (
          <Select size="xs" w={220} aria-label="Model" data={models.map((m) => ({ value: m.id, label: m.label }))}
            value={model} allowDeselect={false}
            onChange={(v) => { if (v) { setModel(v); localStorage.setItem(MODEL_KEY, v); } }} />
        )}
      </Group>
      <ScrollArea flex={1} viewportRef={viewport}>
        <Stack gap="sm" p="xs">
          {messages.map((m, i) => (
            <Paper key={m.id} p="sm" radius="md" withBorder={m.role === "assistant"}
              bg={m.role === "user" ? "blue.9" : m.role === "error" ? "red.9" : undefined}
              c={m.role === "assistant" ? undefined : "white"}
              style={{ alignSelf: m.role === "user" ? "flex-end" : "flex-start", maxWidth: m.role === "user" ? "70%" : "95%" }}>
              {m.steps.length > 0 && <ToolSteps steps={m.steps} running={busyHere && i === messages.length - 1 && !m.content} />}
              {m.role === "assistant"
                ? m.content && <Answer content={m.content} />
                : <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>{m.content}</Text>}
            </Paper>
          ))}
          {busyHere && !messages.some((m, i) => i === messages.length - 1 && m.role === "assistant") && (
            <Group gap="xs"><Loader size="xs" /><Text size="sm" c="dimmed">Working…</Text></Group>
          )}
        </Stack>
      </ScrollArea>
      <Group gap="sm">
        <TextInput flex={1} size="md" placeholder="Ask about your databases"
          value={input} onChange={(e) => setInput(e.currentTarget.value)}
          // Enter that ends an IME composition picks the characters; it isn't a send.
          onKeyDown={(e) => { if (e.key === "Enter" && !e.nativeEvent.isComposing) void send(); }}
          disabled={busy || !model} />
        <Button size="md" leftSection={<IconSend size={16} />} onClick={() => void send()} loading={busyHere}
          // One turn runs at a time; another conversation can be read meanwhile.
          disabled={busy || !model}>Ask</Button>
      </Group>
      <Modal opened={signInStale} onClose={() => setSignInStale(false)} title="Sign in again" centered>
        <Stack gap="md">
          <Text size="sm">
            Your sign-in was made before a change to this site, so it can't be used to reach your data
            sources. Sign out, then sign in again to continue.
          </Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setSignInStale(false)}>Not now</Button>
            <Button onClick={() => void signOut().then(() => window.location.reload())}>Sign out</Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
