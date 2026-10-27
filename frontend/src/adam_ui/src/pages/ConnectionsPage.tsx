//  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
//  SPDX-License-Identifier: Apache-2.0

import { useState, useEffect } from "react";
import {
  Stack,
  TextInput,
  PasswordInput,
  Button,
  Paper,
  Text,
  Title,
  Group,
  Table,
  ActionIcon,
  Loader,
  Select,
  Badge,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconPlus, IconTrash, IconPlugConnected, IconPencil, IconCheck, IconX } from "@tabler/icons-react";
import {
  listConnections,
  listDatabases,
  createConnection,
  deleteConnection,
  updateConnection,
  testConnection,
  testExistingConnection,
  errorText,
  Connection,
  ConnectionTestResult,
  SOURCE_ENGINES,
  RegisteredDatabase,
} from "../services/localApi";

export function ConnectionsPage() {
  const [connections, setConnections] = useState<Connection[]>([]);
  // Why the list could not be read: then the empty list says nothing about what is saved.
  const [listProblem, setListProblem] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editUsername, setEditUsername] = useState("");
  const [editPassword, setEditPassword] = useState("");
  const [databases, setDatabases] = useState<RegisteredDatabase[]>([]);
  const [databaseId, setDatabaseId] = useState<string | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  const resetForm = () => {
    setDatabaseId(null);
    setUsername("");
    setPassword("");
  };

  // Loads once on mount (loading starts true); state is set only in promise callbacks.
  useEffect(() => {
    void Promise.all([
      listConnections().then(setConnections).catch((err: unknown) => setListProblem(errorText(err))),
      listDatabases().then(setDatabases).catch((err: unknown) => {
        notifications.show({ title: "Could not load the registered databases", message: errorText(err), color: "red" });
      }),
    ]).finally(() => {
      setLoading(false);
    });
  }, []);

  /** Shows how a login test went, or why it could not run. */
  const runTest = async (test: () => Promise<ConnectionTestResult>) => {
    try {
      const result = await test();
      if (!result.success) console.error("Connection test failed:", result.message);
      notifications.show({
        title: result.success ? "Connection Successful" : "Connection Failed",
        message: result.message,
        color: result.success ? "green" : "red",
        autoClose: result.success ? 5000 : 15000,
      });
    } catch (err) {
      console.error("Connection test error:", err);
      notifications.show({ title: "Test Failed", message: errorText(err), color: "red", autoClose: 15000 });
    }
  };

  const handleTest = async () => {
    if (!databaseId || !username || !password) {
      notifications.show({
        title: "Validation Error",
        message: "All fields are required to test.",
        color: "red",
      });
      return;
    }
    setTesting(true);
    await runTest(() => testConnection({ databaseId, username, password }));
    setTesting(false);
  };

  const handleEditStart = (conn: Connection) => {
    setEditingId(conn.connectionId);
    setEditUsername("");
    setEditPassword("");
  };

  // Editing replaces the login; the server checks it before saving.
  const handleEditSave = async (connectionId: string) => {
    if (!editUsername || !editPassword) {
      notifications.show({
        title: "Validation Error",
        message: "Enter the new username and password.",
        color: "red",
      });
      return;
    }
    try {
      const updated = await updateConnection(connectionId, {
        username: editUsername,
        password: editPassword,
      });
      setConnections((prev) =>
        prev.map((c) => (c.connectionId === connectionId ? { ...c, ...updated } : c))
      );
      setEditingId(null);
      notifications.show({ title: "Updated", message: "Connection updated.", color: "green" });
    } catch (err) {
      console.error("Update failed:", err);
      notifications.show({ title: "Update Failed", message: errorText(err), color: "red" });
    }
  };

  const handleTestExisting = async (conn: Connection) => {
    setTestingId(conn.connectionId);
    await runTest(() => testExistingConnection(conn.connectionId));
    setTestingId(null);
  };

  const handleSave = async () => {
    if (!databaseId || !username || !password) {
      notifications.show({
        title: "Validation Error",
        message: "All fields are required.",
        color: "red",
      });
      return;
    }

    try {
      setSaving(true);
      const newConn = await createConnection({ databaseId, username, password });
      setConnections((prev) => [...prev, newConn]);
      setShowForm(false);
      resetForm();
      notifications.show({
        title: "Connection Saved",
        message: newConn.name ?? newConn.dsn,
        color: "green",
      });
    } catch (err) {
      notifications.show({
        title: "Error",
        message: errorText(err),
        color: "red",
      });
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (conn: Connection) => {
    if (!window.confirm(`Remove your connection to ${conn.name ?? conn.dsn}?\n\n`
      + (conn.disabled ? "Its version was disabled, and its saved login already deleted."
        : "The saved login is deleted; you can add the connection again later."))) return;
    const { connectionId } = conn;
    try {
      await deleteConnection(connectionId);
      setConnections((prev) => prev.filter((c) => c.connectionId !== connectionId));
      notifications.show({
        title: "Deleted",
        message: "Connection removed.",
        color: "blue",
      });
    } catch (err) {
      notifications.show({
        title: "Error",
        message: errorText(err),
        color: "red",
      });
    }
  };

  // The newest version of the database a disabled connection was for, if one replaces it.
  const replacement = (conn: Connection) =>
    conn.disabled ? databases.find((db) => db.name === conn.name && db.databaseId !== conn.databaseId) : undefined;

  // Opens the form on the newest version; the user gives their login again, so a password only ever goes
  // where it was entered for.
  const connectToNewVersion = (databaseIdToUse: string) => {
    resetForm();
    setDatabaseId(databaseIdToUse);
    setShowForm(true);
  };

  const databaseCell = (conn: Connection) => {
    if (conn.disabled) {
      const newer = replacement(conn);
      return (
        <>
          {conn.name}
          <Text size="xs" c="orange">{conn.disabled}</Text>
          {newer && (
            <Button size="compact-xs" variant="light" mt={4} onClick={() => connectToNewVersion(newer.databaseId)}>
              Connect to the new version
            </Button>
          )}
        </>
      );
    }
    return conn.name ?? (
      <>
        {conn.dsn}
        <Text size="xs" c="orange">Not a registered database: remove it and add it again.</Text>
      </>
    );
  };

  if (loading) {
    return (
      <Stack align="center" pt="xl">
        <Loader />
      </Stack>
    );
  }

  return (
    <Stack gap="md">
      <Group justify="space-between">
        <Title order={3}>Database Connections</Title>
        <Button
          leftSection={<IconPlus size={16} />}
          onClick={() => setShowForm(!showForm)}
        >
          Add Connection
        </Button>
      </Group>

      {showForm && (
        <Paper p="md" withBorder>
          <Stack gap="sm">
            <Text fw={500}>New Database Connection</Text>
            {databases.length > 0 ? (
              <Select
                label="Database"
                value={databaseId}
                onChange={setDatabaseId}
                allowDeselect={false}
                data={databases.map((db) => ({
                  value: db.databaseId,
                  label: `${db.name} (${SOURCE_ENGINES[db.engine]?.label ?? db.engine})`,
                }))}
              />
            ) : (
              <Text size="sm" c="dimmed">
                No databases are registered yet. An administrator registers them on the Admin page.
              </Text>
            )}
            <TextInput
              label="Username"
              value={username}
              onChange={(e) => setUsername(e.currentTarget.value)}
            />
            <PasswordInput
              label="Password"
              value={password}
              onChange={(e) => setPassword(e.currentTarget.value)}
            />
            <Group>
              <Button onClick={() => void handleSave()} loading={saving}>
                Save
              </Button>
              <Button
                variant="light"
                leftSection={<IconPlugConnected size={16} />}
                onClick={() => void handleTest()}
                loading={testing}
              >
                Test Connection
              </Button>
              <Button variant="subtle" onClick={() => { setShowForm(false); resetForm(); }}>
                Cancel
              </Button>
            </Group>
          </Stack>
        </Paper>
      )}

      {listProblem ? (
        <Text c="red" ta="center" py="xl">Could not load your connections: {listProblem}</Text>
      ) : connections.length > 0 ? (
        <Table>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Database</Table.Th>
              <Table.Th>Engine</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {connections.map((conn) => (
              <Table.Tr key={conn.connectionId}>
                {editingId === conn.connectionId ? (
                  <>
                    <Table.Td>
                      {databaseCell(conn)}
                      <Group gap="xs" mt={4} grow>
                        <TextInput size="xs" placeholder="New username" value={editUsername}
                          onChange={(e) => setEditUsername(e.currentTarget.value)} />
                        <PasswordInput size="xs" placeholder="New password" value={editPassword}
                          onChange={(e) => setEditPassword(e.currentTarget.value)} />
                      </Group>
                    </Table.Td>
                    <Table.Td>
                      {conn.engine && <Badge variant="light">{SOURCE_ENGINES[conn.engine]?.label ?? conn.engine}</Badge>}
                    </Table.Td>
                    <Table.Td>
                      <Group gap="xs">
                        <ActionIcon color="green" variant="subtle" onClick={() => void handleEditSave(conn.connectionId)} title="Save"
                          aria-label="Save login">
                          <IconCheck size={16} />
                        </ActionIcon>
                        <ActionIcon color="gray" variant="subtle" onClick={() => setEditingId(null)} title="Cancel"
                          aria-label="Cancel login change">
                          <IconX size={16} />
                        </ActionIcon>
                      </Group>
                    </Table.Td>
                  </>
                ) : (
                  <>
                    <Table.Td>{databaseCell(conn)}</Table.Td>
                    <Table.Td>
                      {conn.engine && <Badge variant="light">{SOURCE_ENGINES[conn.engine]?.label ?? conn.engine}</Badge>}
                    </Table.Td>
                    <Table.Td>
                      <Group gap="xs">
                        {!conn.disabled && (
                          <>
                            <ActionIcon color="blue" variant="subtle" onClick={() => handleEditStart(conn)} title="Change login"
                              aria-label="Change login">
                              <IconPencil size={16} />
                            </ActionIcon>
                            <ActionIcon
                              color="teal"
                              variant="subtle"
                              loading={testingId === conn.connectionId}
                              onClick={() => void handleTestExisting(conn)}
                              title="Test connection"
                              aria-label="Test connection"
                            >
                              <IconPlugConnected size={16} />
                            </ActionIcon>
                          </>
                        )}
                        <ActionIcon color="red" variant="subtle" onClick={() => void handleDelete(conn)} title="Delete connection"
                          aria-label="Delete connection">
                          <IconTrash size={16} />
                        </ActionIcon>
                      </Group>
                    </Table.Td>
                  </>
                )}
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      ) : (
        <Text c="dimmed" ta="center" py="xl">
          No connections configured. Add one to get started.
        </Text>
      )}
    </Stack>
  );
}
