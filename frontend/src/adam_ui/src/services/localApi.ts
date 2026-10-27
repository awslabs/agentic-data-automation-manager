//  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
//  SPDX-License-Identifier: Apache-2.0

import { fetchAuthSession } from "aws-amplify/auth";
import { events } from "aws-amplify/data";
import { Hub } from "aws-amplify/utils";
import awsConfig from "../aws-exports";
import { runTurn, type ChatEvent } from "./chatTurn";

export type { ChatEvent };
export { TurnInactiveError } from "./chatTurn";

const API_BASE = awsConfig.httpApiUrl.replace(/\/$/, "");
const JSON_HEADERS = { "Content-Type": "application/json" };

async function authHeaders(): Promise<Record<string, string>> {
  const session = await fetchAuthSession();
  // The access token: it carries the gateway scope, which the chat agent needs.
  const token = session.tokens?.accessToken?.toString();
  if (!token) throw new Error("Not signed in: no access token in the current session.");
  return { Authorization: `Bearer ${token}` };
}

async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const headers = { ...(await authHeaders()), ...init?.headers };
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (!response.ok) {
    // Our routes answer { error }; API Gateway's own refusals (401, 429, ...) answer { message }.
    const detail = await readJson<{ error?: string; message?: string }>(response).catch(() => ({ error: undefined, message: undefined }));
    throw new Error(detail.error || detail.message || `Request failed (${response.status})`);
  }
  return response;
}

/** A caught error as the page shows it: its message, without String()'s "Error: " prefix. */
export function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** The response body as JSON, typed as the API documents it. */
async function readJson<T>(response: Response): Promise<T> {
  return (await response.json()) as T;
}

/** Cognito groups of the signed-in user, from the ID token. Display only: the API enforces them. */
export async function callerGroups(): Promise<string[]> {
  const session = await fetchAuthSession();
  const groups = session.tokens?.idToken?.payload["cognito:groups"];
  return Array.isArray(groups) ? groups.map(String) : [];
}

// ── Connections API ──────────────────────────────────────────────────────────

/** Database engines the connection registry supports. */
export type SourceEngine = "oracle" | "postgres" | "mysql" | "mariadb" | "redshift";

/** Per-engine presentation details. The backend keeps no port default, so the
 *  defaults live here -- one place, visible to the user before they submit. */
export const SOURCE_ENGINES: Record<SourceEngine, { label: string; defaultPort: string; databaseLabel: string; databasePlaceholder: string }> = {
  oracle: { label: "Oracle", defaultPort: "1521", databaseLabel: "Service Name", databasePlaceholder: "FREEPDB1" },
  postgres: { label: "PostgreSQL", defaultPort: "5432", databaseLabel: "Database", databasePlaceholder: "anycompany" },
  mysql: { label: "MySQL", defaultPort: "3306", databaseLabel: "Database", databasePlaceholder: "anycompany" },
  mariadb: { label: "MariaDB", defaultPort: "3306", databaseLabel: "Database", databasePlaceholder: "anycompany" },
  redshift: { label: "Amazon Redshift", defaultPort: "5439", databaseLabel: "Database", databasePlaceholder: "dev" },
};

/** A registered database a user may connect to: its newest version. */
export interface RegisteredDatabase {
  databaseId: string;
  name: string;
  engine: SourceEngine;
  version: number;
}

export interface Connection {
  connectionId: string;
  /** null for a connection made before connections named a registered database. */
  databaseId: string | null;
  /** null when the database is no longer registered, or the connection predates names. */
  name: string | null;
  /** The version of the database this connection's login is for; null when it is disabled or gone. */
  version: number | null;
  engine: SourceEngine;
  dsn: string;
  /** Why the connection can no longer be used: its version was disabled, and whether a newer one replaces it. */
  disabled?: string;
  createdAt?: string;
}

/** A login for a registered database: the server holds where the database is. */
export interface ConnectionLogin {
  databaseId: string;
  username: string;
  password: string;
}

/** The registered databases, sorted by name. */
export async function listDatabases(): Promise<RegisteredDatabase[]> {
  const response = await apiFetch(`/api/databases`);
  const data = await readJson<{ databases: RegisteredDatabase[] }>(response);
  return data.databases;
}

export async function listConnections(): Promise<Connection[]> {
  const response = await apiFetch(`/api/connections`);
  const data = await readJson<{ connections: Connection[] }>(response);
  return data.connections;
}

/** How a login test went; message says why when it failed. */
export interface ConnectionTestResult {
  success: boolean;
  message: string;
}

export async function createConnection(input: ConnectionLogin): Promise<Connection> {
  const response = await apiFetch(`/api/connections`, {
    method: "POST", headers: JSON_HEADERS, body: JSON.stringify(input),
  });
  return readJson<Connection>(response);
}

export async function deleteConnection(connectionId: string): Promise<void> {
  await apiFetch(`/api/connections/${encodeURIComponent(connectionId)}`, { method: "DELETE" });
}

export async function testConnection(input: ConnectionLogin): Promise<ConnectionTestResult> {
  const response = await apiFetch(`/api/connections/test`, {
    method: "POST", headers: JSON_HEADERS, body: JSON.stringify(input),
  });
  return readJson<ConnectionTestResult>(response);
}

// Replaces the login, which the server checks again; the database stays the same.
export async function updateConnection(connectionId: string, input: {
  username: string;
  password: string;
}): Promise<Connection> {
  const response = await apiFetch(`/api/connections/${encodeURIComponent(connectionId)}`, {
    method: "PATCH", headers: JSON_HEADERS, body: JSON.stringify(input),
  });
  return readJson<Connection>(response);
}

export async function testExistingConnection(connectionId: string): Promise<ConnectionTestResult> {
  const response = await apiFetch(`/api/connections/${encodeURIComponent(connectionId)}/test`, {
    method: "POST",
  });
  return readJson<ConnectionTestResult>(response);
}

// ── Admin API (members of the admin group) ───────────────────────────────────

/** The scanner secret a save gives: an existing secret's ARN (created under
 *  ScanTargetList.scannerSecretPath, the only place the scanners may read), or a username
 *  and password (the scanner stores them there). On edit, giving neither keeps the stored secret. */
export type ScannerSecretInput =
  | { scannerSecretArn: string }
  | { scannerUsername: string; scannerPassword: string }
  | { scannerSecretArn?: never; scannerUsername?: never; scannerPassword?: never };

/** Where a version of a database is. Fixed for the version: a change is a new version. */
export interface DatabaseLocation {
  host: string;
  port: string;
  database: string;
}

/** rate(...) or cron(...); null means on demand only. */
export type ScanSchedule = { schedule: string | null };

// The backend keeps no default for these limits, so the suggestion lives here.
export const DEFAULT_STATEMENT_TIMEOUT_SECONDS = 30;
export const DEFAULT_ROW_CAP = 1000;
export const MAX_STATEMENT_TIMEOUT_SECONDS = 240;
export const MAX_ROW_CAP = 5000;

/** Limits on each statement users run against the target: whole numbers within the bounds above. */
export interface ScanTargetLimits {
  statementTimeoutSeconds: number;
  rowCap: number;
}

/** The latest scan's progress, written by the ingestion function. */
export type ScanStatus = "queued" | "scanning" | "scanned" | "failed";

/** A registered version of a database, as listed. tableCount is null when the index could not be read. */
export interface RegisteredScanTarget {
  targetId: string;
  /** The database this is a version of: its first version's targetId. */
  lineageId: string;
  version: number;
  /** The newest version not disabled: the one new connections go to. */
  latest: boolean;
  /** What users choose on the Connections page; null for a target registered before names. */
  name: string | null;
  identity: string;
  engine: SourceEngine;
  host: string;
  port: string;
  database: string;
  scannerSecretArn: string;
  /** The stack created this secret from a username and password, and deletes it with the target. */
  scannerSecretManaged: boolean;
  /** Refused to users and no longer scanned. Never deleted; its users' saved logins are. */
  disabled: boolean;
  /** Who made the version, last changed its settings, and disabled it, from their sign-in; with when. */
  createdBy: string | null;
  createdAt: string | null;
  updatedBy: string | null;
  updatedAt: string | null;
  disabledBy: string | null;
  disabledAt: string | null;
  schedule: string | null;
  /** Null for a target registered before limits; users' statements are refused until they are set. */
  statementTimeoutSeconds: number | null;
  rowCap: number | null;
  tableCount: number | null;
  /** All null when the target was never scanned. */
  scanStatus: ScanStatus | null;
  scanMessage: string | null;
  scanUpdatedAt: string | null;
  /** Oracle only: whether its connections use TLS, as the probe found when the target was saved. */
  tls?: boolean;
  /** Oracle only, when tls is false: why, and what to do about it. */
  tlsWarning?: string;
  /** Oracle only: the SHA-256 fingerprint of the database's own certificate, when TLS trusts that
   *  certificate alone (it is self-signed, or from a CA the service does not carry). */
  tlsFingerprint?: string;
}

export interface SavedScanTarget extends RegisteredScanTarget {
  /** Clean-up the save could not finish (e.g. an old secret not deleted), for the admin to act on. */
  warnings?: string[];
}

export interface ScanTargetList {
  targets: RegisteredScanTarget[];
  /** The engines whose database provider is registered with this deployment: the only ones a target may use. */
  engines: string[];
  /** Where a brought scanner secret must be created. */
  scannerSecretPath: string;
}

export interface DisabledScanTarget {
  disabled: string;
  identity: string;
  version: number;
  /** Index entries this deployment had created for the database, now deleted. */
  indexEntriesRemoved: number;
  /** Index entries for it that this deployment did not create, left in place. */
  indexEntriesKept: number;
  /** Another version still in use is at the same place, so its index entries were kept. */
  sharedWithAnotherVersion: boolean;
}

export async function listScanTargets(): Promise<ScanTargetList> {
  const response = await apiFetch(`/api/admin/scan-targets`);
  return readJson<ScanTargetList>(response);
}

/** A new database: its first version. */
export async function addScanTarget(
  target: { engine: SourceEngine; name: string } & ScanTargetLimits & DatabaseLocation & ScanSchedule
    & ScannerSecretInput,
): Promise<SavedScanTarget> {
  const response = await apiFetch(`/api/admin/scan-targets`, {
    method: "POST", headers: JSON_HEADERS, body: JSON.stringify(target),
  });
  return readJson<SavedScanTarget>(response);
}

/** The next version of the database `targetId` is a version of: where it is now, with its own scanner
 *  login. Its name and limits carry over; users stay on the versions they connected to until it is disabled. */
export async function addScanTargetVersion(
  targetId: string, version: DatabaseLocation & ScanSchedule & ScannerSecretInput,
): Promise<SavedScanTarget> {
  const response = await apiFetch(`/api/admin/scan-targets/${encodeURIComponent(targetId)}/versions`, {
    method: "POST", headers: JSON_HEADERS, body: JSON.stringify(version),
  });
  return readJson<SavedScanTarget>(response);
}

// Only what sends nothing anywhere new: the database's name and limits (on every version of it), this
// version's schedule, and its scanner login, checked where the version is. The server refuses anything else.
export async function updateScanTarget(
  targetId: string, changes: { name?: string } & Partial<ScanTargetLimits> & Partial<ScanSchedule> & ScannerSecretInput,
): Promise<SavedScanTarget> {
  const response = await apiFetch(`/api/admin/scan-targets/${encodeURIComponent(targetId)}`, {
    method: "PATCH", headers: JSON_HEADERS, body: JSON.stringify(changes),
  });
  return readJson<SavedScanTarget>(response);
}

/** Refuse the version to users from now on and delete their saved logins for it. Safe to repeat. */
export async function disableScanTarget(targetId: string): Promise<DisabledScanTarget> {
  const response = await apiFetch(`/api/admin/scan-targets/${encodeURIComponent(targetId)}`, {
    method: "DELETE",
  });
  return readJson<DisabledScanTarget>(response);
}

/** Check the scanner login, then start a scan in the background. The server holds the
 * target's connection and scanner secret; the target's scanStatus shows how it went. */
export async function startScan(targetId: string): Promise<RegisteredScanTarget> {
  const response = await apiFetch(`/api/admin/scans`, {
    method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ targetId }),
  });
  return readJson<RegisteredScanTarget>(response);
}

/** How an administrator should set up user and scanner accounts for this engine, from its provider. */
export async function getAdminBestPractices(engine: SourceEngine): Promise<string> {
  const response = await apiFetch(`/api/admin/best-practices/${engine}`);
  return (await readJson<{ guidance: string }>(response)).guidance;
}

// ── Chat (only when the agent runtime is deployed: awsConfig.agentRuntimeArn) ──

export interface Conversation {
  conversation_id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

/** Whether the signed-in user's access token carries the gateway's scope. A sign-in made
 * before the web client requested it doesn't, and only a new sign-in gets one that does. */
export async function signInHasGatewayScope(): Promise<boolean> {
  const scope = (await fetchAuthSession()).tokens?.accessToken?.payload.scope;
  return typeof scope === "string" && scope.split(" ").includes(awsConfig.gatewayScope);
}

export interface StoredChatEvent extends ChatEvent {
  turn: string;
  seq: number;
}

export async function listConversations(): Promise<Conversation[]> {
  const response = await apiFetch(`/api/conversations`);
  return (await readJson<{ conversations: Conversation[] }>(response)).conversations;
}

export async function conversationMessages(conversationId: string): Promise<StoredChatEvent[]> {
  const response = await apiFetch(`/api/conversations/${encodeURIComponent(conversationId)}/messages`);
  return (await readJson<{ messages: StoredChatEvent[] }>(response)).messages;
}

export async function deleteConversation(conversationId: string): Promise<void> {
  await apiFetch(`/api/conversations/${encodeURIComponent(conversationId)}`, { method: "DELETE" });
}

/** A model the chat may use, as the server allows them; the first is the default. */
export interface ChatModel {
  id: string;
  label: string;
}

export async function listModels(): Promise<ChatModel[]> {
  const response = await apiFetch(`/api/models`);
  return (await readJson<{ models: ChatModel[] }>(response)).models;
}

/** Send a prompt and stream its answer on the caller's own channel for this conversation;
 * resolves when the turn ends. The server keeps the conversation's history, so only the prompt goes.
 * `onAccepted` runs once the server has taken the prompt: a new conversation is listed from then. */
export async function sendChat(
  prompt: string,
  conversationId: string,
  model: string,
  onEvent: (evt: ChatEvent) => void,
  onAccepted: () => void,
): Promise<void> {
  const session = await fetchAuthSession();
  const sub = session.tokens?.idToken?.payload.sub;
  if (typeof sub !== "string") throw new Error("Not signed in: no subject in the ID token.");

  const path = `/agent/${sub}/${conversationId}`;
  const channel = await events.connect(path);
  try {
    await runTurn({
      open: ({ next, error, acknowledged }) => {
        // channel.subscribe returns before AppSync's subscribe_success; Amplify reports that only on
        // the Hub "api" channel, as CONTROL_MSG.SUBSCRIPTION_ACK ("Subscription ack") naming the channel.
        const stopListening = Hub.listen("api", ({ payload }) => {
          if (payload.event === "Subscription ack" && (payload.data as { query?: string } | undefined)?.query === path) {
            acknowledged();
          }
        });
        const subscription = channel.subscribe({ next, error });
        return () => { stopListening(); subscription.unsubscribe(); };
      },
    }, () => apiFetch(`/api/chat`, {
      method: "POST", headers: JSON_HEADERS,
      body: JSON.stringify({ prompt, conversation_id: conversationId, model }),
    }).then((response) => { onAccepted(); return response; }), onEvent);
  } finally {
    channel.close();
  }
}
