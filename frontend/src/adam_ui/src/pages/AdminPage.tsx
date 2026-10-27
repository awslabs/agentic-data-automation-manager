//  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
//  SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from "react";
import {
  Accordion, ActionIcon, Badge, Button, Code, Group, Loader, NumberInput, Paper, PasswordInput, Popover, SegmentedControl,
  Select, Stack, Table, Text, TextInput, Title, Tooltip,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconBan, IconPencil, IconPlus, IconRadar, IconVersions } from "@tabler/icons-react";
import {
  addScanTarget, addScanTargetVersion, disableScanTarget, errorText, SOURCE_ENGINES, SourceEngine, DEFAULT_ROW_CAP,
  DEFAULT_STATEMENT_TIMEOUT_SECONDS, getAdminBestPractices, listScanTargets, MAX_ROW_CAP, MAX_STATEMENT_TIMEOUT_SECONDS,
  RegisteredScanTarget, ScannerSecretInput, startScan, updateScanTarget,
} from "../services/localApi";

type LoginSource = "arn" | "credentials";
/** The form registers a new database, edits a version's settings, or publishes a new version of one. */
type FormMode = "register" | "edit" | "version";

const VERSIONS = "Where a database is and how it is reached (host, port, database, TLS) are fixed for each version, "
  + "so users' passwords only go where they entered them for. To change them, publish a new version: users "
  + "connect to it with their own logins, and stay on older versions until you disable those.";
const DISABLE_STEPS = "Disabling a version refuses it to users at once and for good, then deletes its schedule, the "
  + "scanner secret if this stack created it (a secret you brought is left as it is), every user's saved login "
  + "for it, and the search index entries this stack created for it, unless another version in use is at the same "
  + "place. The version stays listed, with who disabled it.";

/** One database's versions together, newest first; databases by name. */
const byDatabase = (targets: RegisteredScanTarget[]): RegisteredScanTarget[] => {
  const nameOf = new Map<string, string>();
  for (const t of targets) if (t.name) nameOf.set(t.lineageId, t.name);
  return [...targets].sort((a, b) =>
    (nameOf.get(a.lineageId) ?? a.lineageId).localeCompare(nameOf.get(b.lineageId) ?? b.lineageId)
    || a.lineageId.localeCompare(b.lineageId) || b.version - a.version);
};
type ScheduleMode = "on-demand" | "scheduled";

const isWholeInRange = (value: number | string, max: number) =>
  typeof value === "number" && Number.isInteger(value) && value >= 1 && value <= max;

/** The registered engines this page knows how to show, in its own order. */
const offered = (registered: string[]): SourceEngine[] =>
  (Object.keys(SOURCE_ENGINES) as SourceEngine[]).filter((e) => registered.includes(e));

/** Each registered engine's account-setup advice, as its provider writes it. */
function AdminBestPractices({ engines }: { engines: SourceEngine[] }) {
  const [guidance, setGuidance] = useState<Partial<Record<SourceEngine, string>>>({});
  const [failed, setFailed] = useState<Partial<Record<SourceEngine, string>>>({});

  useEffect(() => {
    for (const engine of engines) {
      getAdminBestPractices(engine)
        .then((text) => setGuidance((g) => ({ ...g, [engine]: text })))
        .catch((err) => setFailed((f) => ({ ...f, [engine]: errorText(err) })));
    }
  }, [engines]);

  return (
    <Paper withBorder p="md" radius="md">
      <Text fw={500} mb="xs">Account guidance</Text>
      <Text size="xs" c="dimmed" mb="sm">
        How to set up the accounts users register and the scanner account, for each engine. Advice for the administrator:
        the stack does not check an account against it.
      </Text>
      <Accordion variant="contained" multiple>
        {engines.map((engine) => (
          <Accordion.Item key={engine} value={engine}>
            <Accordion.Control>{SOURCE_ENGINES[engine].label}</Accordion.Control>
            <Accordion.Panel>
              {failed[engine] ? <Text size="sm" c="red">Could not load: {failed[engine]}</Text>
                : guidance[engine] === undefined ? <Loader size="xs" />
                  : <Code block style={{ whiteSpace: "pre-wrap" }}>{guidance[engine].trim()}</Code>}
            </Accordion.Panel>
          </Accordion.Item>
        ))}
      </Accordion>
    </Paper>
  );
}

const POLL_MS = 5000;

export function AdminPage() {
  const [targets, setTargets] = useState<RegisteredScanTarget[]>([]);
  const [scannerPath, setScannerPath] = useState<string | null>(null);
  const [engines, setEngines] = useState<SourceEngine[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [scanning, setScanning] = useState<string | null>(null);
  const [refreshes, setRefreshes] = useState(0);

  // The form registers a new database, or acts on `editing` as `mode` says.
  const [mode, setMode] = useState<FormMode>("register");
  const [editing, setEditing] = useState<RegisteredScanTarget | null>(null);
  const [name, setName] = useState("");
  const [engine, setEngine] = useState<SourceEngine>("postgres");
  const [host, setHost] = useState("");
  const [port, setPort] = useState(SOURCE_ENGINES.postgres.defaultPort);
  const [database, setDatabase] = useState("");
  const [loginSource, setLoginSource] = useState<LoginSource>("arn");
  const [scannerSecretArn, setScannerSecretArn] = useState("");
  const [scannerUsername, setScannerUsername] = useState("");
  const [scannerPassword, setScannerPassword] = useState("");
  const [scheduleMode, setScheduleMode] = useState<ScheduleMode>("on-demand");
  const [schedule, setSchedule] = useState("");
  const [statementTimeoutSeconds, setStatementTimeoutSeconds] = useState<number | string>(
    DEFAULT_STATEMENT_TIMEOUT_SECONDS);
  const [rowCap, setRowCap] = useState<number | string>(DEFAULT_ROW_CAP);

  // State is set only in promise callbacks, so the mount effect below never sets it synchronously.
  const refresh = (): Promise<void> => listScanTargets()
    .then((listed) => {
      setTargets(listed.targets);
      setScannerPath(listed.scannerSecretPath);
      setEngines((current) => {
        const next = offered(listed.engines);
        // The same list keeps its identity, so the guidance is not fetched again on every refresh.
        return next.join() === current.join() ? current : next;
      });
    })
    .catch((err) => {
      notifications.show({ title: "Could not load scan targets", message: errorText(err), color: "red" });
    })
    .finally(() => {
      setLoading(false);
      setRefreshes((n) => n + 1);
    });
  useEffect(() => { void refresh(); }, []);
  // Scans run in the background; while one is in progress, the list is re-read to show how it went.
  // Each refresh, failed or not, schedules the next, so one failure doesn't stop the polling.
  const inProgress = targets.some((t) => t.scanStatus === "queued" || t.scanStatus === "scanning");
  useEffect(() => {
    if (!inProgress) return;
    const timer = setTimeout(() => { void refresh(); }, POLL_MS);
    return () => clearTimeout(timer);
  }, [inProgress, refreshes]);

  const handleEngineChange = (value: string | null) => {
    if (!value) return;
    const next = value as SourceEngine;
    setEngine(next);
    setPort(SOURCE_ENGINES[next].defaultPort);
  };

  const clearSecret = () => {
    setScannerSecretArn("");
    setScannerUsername("");
    setScannerPassword("");
  };

  const resetForm = () => {
    setMode("register");
    setEditing(null);
    setName("");
    setEngine("postgres");
    setHost("");
    setPort(SOURCE_ENGINES.postgres.defaultPort);
    setDatabase("");
    setLoginSource("arn");
    clearSecret();
    setScheduleMode("on-demand");
    setSchedule("");
    setStatementTimeoutSeconds(DEFAULT_STATEMENT_TIMEOUT_SECONDS);
    setRowCap(DEFAULT_ROW_CAP);
  };

  // The password is never sent back by the server, so an edit starts with the secret blank.
  const startForm = (t: RegisteredScanTarget, next: FormMode) => {
    setMode(next);
    setEditing(t);
    setName(t.name ?? "");
    setEngine(t.engine);
    setHost(t.host);
    setPort(t.port || SOURCE_ENGINES[t.engine].defaultPort);
    setDatabase(t.database);
    setLoginSource(t.scannerSecretManaged ? "credentials" : "arn");
    clearSecret();
    // A new version has its own schedule and scanner login: it starts on demand, with a secret to give.
    setScheduleMode(next === "edit" && t.schedule ? "scheduled" : "on-demand");
    setSchedule(next === "edit" ? t.schedule ?? "" : "");
    setStatementTimeoutSeconds(t.statementTimeoutSeconds ?? DEFAULT_STATEMENT_TIMEOUT_SECONDS);
    setRowCap(t.rowCap ?? DEFAULT_ROW_CAP);
  };

  const run = async (work: () => Promise<void>, failure: string) => {
    setBusy(true);
    try {
      await work();
    } catch (err) {
      notifications.show({ title: failure, message: errorText(err), color: "red", autoClose: 15000 });
    } finally {
      setBusy(false);
    }
  };

  const showWarnings = (warnings: string[] | undefined, title = "Saved, with a problem to fix") => {
    for (const message of warnings ?? []) {
      notifications.show({ title, message, color: "orange", autoClose: false });
    }
  };

  const pinnedMessage = (fingerprint: string) =>
    `TLS trusts this database's own certificate, and only it. Check its SHA-256 fingerprint matches the `
    + `certificate on the database: ${fingerprint}`;

  const showPinned = (target: RegisteredScanTarget) => {
    if (target.tlsFingerprint) {
      notifications.show({ title: "TLS with the database's own certificate", message: pinnedMessage(target.tlsFingerprint),
        color: "blue", autoClose: false });
    }
  };

  const secretInput = (): ScannerSecretInput => {
    if (loginSource === "arn") {
      return scannerSecretArn.trim() ? { scannerSecretArn: scannerSecretArn.trim() } : {};
    }
    return scannerUsername.trim() || scannerPassword
      ? { scannerUsername: scannerUsername.trim(), scannerPassword }
      : {};
  };

  const saveTitles: Record<FormMode, [string, string]> = {
    register: ["Database registered", "Could not register the database"],
    edit: ["Settings saved", "Could not save the settings"],
    version: ["New version published", "Could not publish the new version"],
  };

  const handleSave = () => run(async () => {
    const location = { host: host.trim(), port: port.trim(), database: database.trim() };
    const scheduleValue = scheduleMode === "scheduled" ? schedule.trim() : null;
    const trimmedName = name.trim();
    const limits = { statementTimeoutSeconds: Number(statementTimeoutSeconds), rowCap: Number(rowCap) };
    const saved = mode === "edit" && editing
      ? await updateScanTarget(editing.targetId, {
        ...(trimmedName ? { name: trimmedName } : {}), schedule: scheduleValue, ...limits, ...secretInput(),
      })
      : mode === "version" && editing
        ? await addScanTargetVersion(editing.targetId, { ...location, schedule: scheduleValue, ...secretInput() })
        : await addScanTarget({
          name: trimmedName, engine, ...location, schedule: scheduleValue, ...limits, ...secretInput(),
        });
    notifications.show({
      title: saveTitles[mode][0], color: "green",
      message: `${saved.identity} (version ${saved.version})`
        + (saved.scanStatus === "queued" ? ": scanning in the background." : ""),
    });
    showPinned(saved);
    showWarnings([...(saved.tlsWarning ? [saved.tlsWarning] : []), ...(saved.warnings ?? [])]);
    resetForm();
    await refresh();
  }, saveTitles[mode][1]);

  const handleDisable = (t: RegisteredScanTarget) => {
    if (!window.confirm(`Disable version ${t.version} of ${t.name ?? t.identity}?\n\n${DISABLE_STEPS}`)) return;
    return run(async () => {
      try {
        const disabled = await disableScanTarget(t.targetId);
        notifications.show({
          title: "Version disabled", color: "green",
          message: `${disabled.identity} (version ${disabled.version}): `
            + (disabled.sharedWithAnotherVersion
              ? "its index entries are kept for the version in use at the same place."
              : `${disabled.indexEntriesRemoved} index entries removed`
                + (disabled.indexEntriesKept ? `, ${disabled.indexEntriesKept} not created by this stack kept.` : ".")),
        });
        if (editing?.targetId === t.targetId) resetForm();
      } finally {
        await refresh();   // a disabling that stopped part-way is listed, and can be run again
      }
    }, "Could not finish disabling the version");
  };

  // Waits only for the login check; the scan's progress then shows in the table.
  const handleScan = (targetId: string) => run(async () => {
    setScanning(targetId);
    try {
      const started = await startScan(targetId);
      showPinned(started);
      showWarnings(started.tlsWarning ? [started.tlsWarning] : [], "Scanning, with a problem to fix");
    } finally {
      setScanning(null);
      await refresh();
    }
  }, "Could not start the scan");

  const statusBadge = (t: RegisteredScanTarget) => {
    const when = t.scanUpdatedAt ? ` (${new Date(t.scanUpdatedAt).toLocaleString()})` : "";
    switch (t.scanStatus) {
      case null:
        return <Badge color="gray" variant="light">never scanned</Badge>;
      case "queued":
      case "scanning":
        return <Badge color="blue" variant="light" leftSection={<Loader size={10} color="blue" />}>{t.scanStatus}</Badge>;
      case "scanned":
        return <Tooltip label={`${t.scanMessage ?? ""}${when}`}><Badge color="green" variant="light">scanned</Badge></Tooltip>;
      case "failed":
        return <Tooltip label={`Failed${when}`}><Badge color="red" variant="light">failed</Badge></Tooltip>;
    }
  };

  const countBadge = (t: RegisteredScanTarget) =>
    t.tableCount === null ? <Badge color="gray" variant="light">unknown</Badge>
      : t.tableCount === 0 ? <Badge color="yellow" variant="light">not scanned</Badge>
        : <Badge color="green" variant="light">{t.tableCount}</Badge>;

  const secretCell = (t: RegisteredScanTarget) =>
    !t.scannerSecretArn ? <Text size="xs" c="orange">none</Text>
      : t.scannerSecretManaged ? <Text size="xs">Created by the stack</Text>
        : <Tooltip label={t.scannerSecretArn} multiline maw={420}><Text size="xs">Existing secret</Text></Tooltip>;

  const limitsCell = (t: RegisteredScanTarget) =>
    t.statementTimeoutSeconds === null || t.rowCap === null
      ? <Text size="xs" c="orange">Not set: users' statements are refused until you edit it and set them.</Text>
      : <Text size="xs">{t.statementTimeoutSeconds} s · {t.rowCap} rows</Text>;

  // Only an edit keeps the version's scanner login: a new database or version needs one of its own.
  const keepsSecret = mode === "edit" && !!editing?.scannerSecretArn;
  const secretGiven = loginSource === "arn" ? !!scannerSecretArn.trim() : !!(scannerUsername.trim() && scannerPassword);
  // A name is required to register, and to edit a target registered before names existed.
  const nameGiven = !!name.trim() || (!!editing && editing.name !== null);
  const locationFixed = mode === "edit";
  const settingsCarried = mode === "version";
  const canSave = !busy && nameGiven && !!host && !!port && !!database
    && (secretGiven || keepsSecret)
    && (scheduleMode === "on-demand" || !!schedule.trim())
    && isWholeInRange(statementTimeoutSeconds, MAX_STATEMENT_TIMEOUT_SECONDS) && isWholeInRange(rowCap, MAX_ROW_CAP);

  const versionBadge = (t: RegisteredScanTarget) =>
    t.disabled ? <Badge color="gray" variant="light">v{t.version} disabled</Badge>
      : t.latest ? <Badge color="green" variant="light">v{t.version} newest</Badge>
        : <Tooltip label="Its users' connections still work; new connections go to the newest version.">
          <Badge color="yellow" variant="light">v{t.version} older, in use</Badge></Tooltip>;

  const who = (by: string | null, at: string | null) =>
    `${by ?? "unknown"}${at ? `, ${new Date(at).toLocaleString()}` : ""}`;

  return (
    <Stack gap="md" maw={1000}>
      <Title order={3}>Schema scanning</Title>

      <Paper withBorder p="md" radius="md">
        <Text fw={500} mb="xs">Registered scan targets</Text>
        <Text size="xs" c="dimmed" mb="sm">
          A database can be scanned only if it is listed here, with its scanner secret. Scanning reads table
          structure with the scanner account's credential; it never reads data. Tables is how many of its tables
          and views are in the search index now. {VERSIONS}
        </Text>
        {loading ? <Loader size="sm" /> : targets.length === 0 ? (
          <Text size="sm" c="dimmed">No targets registered.</Text>
        ) : (
          <Table>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Database</Table.Th>
                <Table.Th w={150}>Scanner secret</Table.Th>
                <Table.Th w={170}>Schedule (UTC)</Table.Th>
                <Table.Th w={140}>Limits</Table.Th>
                <Table.Th w={120}>Last scan</Table.Th>
                <Table.Th w={100}>Tables</Table.Th>
                <Table.Th w={100} />
                <Table.Th w={70} />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {byDatabase(targets).map((t) => (
                <Table.Tr key={t.targetId} opacity={t.disabled ? 0.6 : 1}>
                  <Table.Td>
                    <Group gap={6}>
                      {t.name ?? (
                        <Text size="xs" c="orange">No name: users can't choose it until you edit it and give one.</Text>
                      )}
                      {versionBadge(t)}
                    </Group>
                    <Text size="xs" c="dimmed">{t.identity}</Text>
                    <Text size="xs" c="dimmed">Created by {who(t.createdBy, t.createdAt)}</Text>
                    {t.updatedBy && <Text size="xs" c="dimmed">Settings changed by {who(t.updatedBy, t.updatedAt)}</Text>}
                    {t.disabled && (
                      <Text size="xs" c="dimmed">
                        Disabled by {who(t.disabledBy, t.disabledAt)}. If disabling reported a problem, disable it
                        again to finish.
                      </Text>
                    )}
                    {t.scanStatus === "failed" && <Text size="xs" c="red">{t.scanMessage}</Text>}
                  </Table.Td>
                  <Table.Td>{secretCell(t)}</Table.Td>
                  <Table.Td>
                    {t.schedule
                      ? <Text size="xs" ff="monospace">{t.schedule}</Text>
                      : <Badge color="gray" variant="light">on demand</Badge>}
                  </Table.Td>
                  <Table.Td>{limitsCell(t)}</Table.Td>
                  <Table.Td>{statusBadge(t)}</Table.Td>
                  <Table.Td>{countBadge(t)}</Table.Td>
                  <Table.Td>
                    <Button size="xs" variant="light" leftSection={<IconRadar size={14} />}
                      loading={scanning === t.targetId}
                      disabled={busy || t.disabled || t.scanStatus === "queued" || t.scanStatus === "scanning"}
                      onClick={() => void handleScan(t.targetId)}>
                      Scan
                    </Button>
                  </Table.Td>
                  <Table.Td>
                    <Group gap={4} wrap="nowrap">
                      {t.tlsFingerprint && (
                        <Popover width={360} position="bottom-end" withArrow shadow="md" trapFocus>
                          <Popover.Target>
                            <Button size="compact-xs" variant="light" color="blue"
                              aria-label="TLS with the database's own certificate">🔒</Button>
                          </Popover.Target>
                          <Popover.Dropdown>
                            <Text size="sm">{pinnedMessage(t.tlsFingerprint)}</Text>
                          </Popover.Dropdown>
                        </Popover>
                      )}
                      {t.tlsWarning && (
                        <Popover width={360} position="bottom-end" withArrow shadow="md" trapFocus>
                          <Popover.Target>
                            <Button size="compact-xs" variant="light" color="orange"
                              aria-label="TLS is not enabled: why">⚠️🔓</Button>
                          </Popover.Target>
                          <Popover.Dropdown>
                            <Text size="sm">{t.tlsWarning}</Text>
                          </Popover.Dropdown>
                        </Popover>
                      )}
                      <Tooltip label="Edit settings">
                        <ActionIcon variant="subtle" color="blue" aria-label="Edit settings"
                          disabled={busy || t.disabled} onClick={() => startForm(t, "edit")}>
                          <IconPencil size={16} />
                        </ActionIcon>
                      </Tooltip>
                      <Tooltip label="New version">
                        <ActionIcon variant="subtle" color="teal" aria-label="New version"
                          disabled={busy} onClick={() => startForm(t, "version")}>
                          <IconVersions size={16} />
                        </ActionIcon>
                      </Tooltip>
                      <Tooltip label={t.disabled ? "Disable again to finish" : "Disable version"}>
                        <ActionIcon variant="subtle" color="red" aria-label="Disable version"
                          disabled={busy} onClick={() => void handleDisable(t)}>
                          <IconBan size={16} />
                        </ActionIcon>
                      </Tooltip>
                    </Group>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
      </Paper>

      <Paper withBorder p="md" radius="md">
        <Text fw={500} mb="xs">
          {mode === "edit" && editing ? `Settings of ${editing.name ?? editing.identity}, version ${editing.version}`
            : mode === "version" && editing ? `New version of ${editing.name ?? editing.identity}`
              : "Register a database"}
        </Text>
        <Text size="xs" c="dimmed" mb="sm">
          {mode === "edit"
            ? "The name and limits apply to every version of this database; the schedule and scanner login to this "
              + "version. Where it is can't be edited: publish a new version for that."
            : <>Use the host users connect with: the index is keyed on where the database is, not on any name.
              Saving logs in as the scanner first, and nothing is saved if that fails. The scan then runs in the
              background, and its progress shows in the table.
              {mode === "version" && " The new version keeps the database's name and limits, needs its own scanner "
                + "login, and is where new connections go. Users already connected stay on their version until "
                + "you disable it."}</>}
        </Text>
        <Stack gap="xs">
          <TextInput label="Name" required maxLength={64} value={name} disabled={settingsCarried}
            description="What users choose on the Connections page."
            onChange={(e) => setName(e.currentTarget.value)} />
          <Group grow align="flex-start">
            <Select label="Engine" data={engines.map((value) => ({ value, label: SOURCE_ENGINES[value].label }))}
              value={engines.includes(engine) || editing ? engine : null} onChange={handleEngineChange}
              allowDeselect={false} disabled={!!editing}
              error={!loading && engines.length === 0
                ? "No database provider has registered with this deployment yet." : undefined} />
            <TextInput label="Host" value={host} disabled={locationFixed} onChange={(e) => setHost(e.currentTarget.value)} />
            <TextInput label="Port" value={port} disabled={locationFixed} onChange={(e) => setPort(e.currentTarget.value)} />
            <TextInput label={SOURCE_ENGINES[engine].databaseLabel} placeholder={SOURCE_ENGINES[engine].databasePlaceholder}
              value={database} disabled={locationFixed} onChange={(e) => setDatabase(e.currentTarget.value)} />
          </Group>
          <Group grow align="flex-start">
            <NumberInput label="Statement timeout (seconds)" required min={1} max={MAX_STATEMENT_TIMEOUT_SECONDS}
              allowDecimal={false} allowNegative={false} value={statementTimeoutSeconds} disabled={settingsCarried}
              description="Each statement users run is cancelled after this." onChange={setStatementTimeoutSeconds} />
            <NumberInput label="Row cap" required min={1} max={MAX_ROW_CAP}
              allowDecimal={false} allowNegative={false} value={rowCap} disabled={settingsCarried}
              description="At most this many rows are returned per statement." onChange={setRowCap} />
          </Group>

          <Text size="sm" fw={500} mt="xs">Scanner secret</Text>
          <SegmentedControl value={loginSource} onChange={(v) => { setLoginSource(v as LoginSource); clearSecret(); }}
            data={[{ value: "arn", label: "Use an existing secret" }, { value: "credentials", label: "Enter credentials" }]} />
          {loginSource === "arn" && (
            <TextInput label="Secret ARN" value={scannerSecretArn}
              placeholder={keepsSecret ? "Leave blank to keep the stored secret" : "arn:aws:secretsmanager:..."}
              description={"A secret in this account and region holding the scanner account's username and "
                + "password, created under the path below: the scanners may read no other secret. Saving checks "
                + "the scanner can read it and log in. If it uses a customer-managed KMS key, that key must allow "
                + "kms:Decrypt for the engine's scanner function role."}
              onChange={(e) => setScannerSecretArn(e.currentTarget.value)} />
          )}
          {loginSource === "arn" && (scannerPath ? (
            <Text size="xs">
              Secret name starts with <Code>{scannerPath}</Code>
            </Text>
          ) : !loading && <Text size="xs" c="orange">The path this deployment requires could not be loaded.</Text>)}
          {loginSource === "credentials" && (
            <>
              <Text size="xs" c="dimmed">
                The scanner stores these in a new secret under that path, which only the scanners may read. The password is never
                shown again.{keepsSecret && " Leave both blank to keep the stored secret."}
              </Text>
              <Group grow align="flex-start">
                <TextInput label="Scanner username" value={scannerUsername} autoComplete="off"
                  onChange={(e) => setScannerUsername(e.currentTarget.value)} />
                <PasswordInput label="Scanner password" value={scannerPassword} autoComplete="new-password"
                  onChange={(e) => setScannerPassword(e.currentTarget.value)} />
              </Group>
            </>
          )}

          <Text size="sm" fw={500} mt="xs">Schedule</Text>
          <SegmentedControl value={scheduleMode} onChange={(v) => setScheduleMode(v as ScheduleMode)}
            data={[{ value: "on-demand", label: "On demand only" }, { value: "scheduled", label: "On a schedule" }]} />
          {scheduleMode === "scheduled" && (
            <TextInput label="Schedule expression" value={schedule} placeholder="rate(1 day)"
              description="rate(<n> minutes|hours|days), or cron(minutes hours day-of-month month day-of-week year), e.g. cron(0 2 * * ? *) for 02:00 UTC daily."
              onChange={(e) => setSchedule(e.currentTarget.value)} />
          )}

          <Group mt="xs">
            <Button leftSection={mode === "edit" ? <IconPencil size={16} />
              : mode === "version" ? <IconVersions size={16} /> : <IconPlus size={16} />}
              onClick={() => void handleSave()} loading={busy} disabled={!canSave}>
              {mode === "edit" ? "Save settings" : mode === "version" ? "Publish new version" : "Register database"}
            </Button>
            {editing && <Button variant="subtle" onClick={resetForm} disabled={busy}>Cancel</Button>}
          </Group>
        </Stack>
      </Paper>

      <AdminBestPractices engines={engines} />
    </Stack>
  );
}
