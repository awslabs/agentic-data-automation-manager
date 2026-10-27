//  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
//  SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from "react";
import { ActionIcon, Button, Group, Loader, NavLink, ScrollArea, Stack, Text } from "@mantine/core";
import { IconMessage, IconPlus, IconTrash } from "@tabler/icons-react";
import { deleteConversation, errorText, listConversations, type Conversation } from "../services/localApi";

interface Props {
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  /** Bumped by the page when a prompt is taken and when its turn ends, so a new or renamed conversation shows up. */
  refreshKey: number;
  /** The conversation a turn is running in: it can't be deleted until the turn ends. */
  runningId: string | null;
}

export function ConversationSidebar({ activeId, onSelect, onNew, refreshKey, runningId }: Props) {
  const [conversations, setConversations] = useState<Conversation[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    listConversations()
      .then((list) => { setConversations(list); setProblem(null); })
      .catch((err: unknown) => setProblem(errorText(err)));
  }, [refreshKey]);

  const remove = async (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    try {
      await deleteConversation(id);
      setConversations((prev) => (prev ?? []).filter((c) => c.conversation_id !== id));
      if (activeId === id) onNew();
    } catch (err) {
      setProblem(errorText(err));
    }
  };

  return (
    <Stack h="100%" gap={0}>
      <Group p="sm" justify="space-between">
        <Text size="sm" fw={600} c="dimmed">Conversations</Text>
        <Button size="xs" variant="light" leftSection={<IconPlus size={14} />} onClick={onNew}>New</Button>
      </Group>
      <ScrollArea flex={1} px="xs">
        {problem && <Text size="xs" c="red" px="xs" py="xs">{problem}</Text>}
        {conversations === null && !problem ? (
          <Group justify="center" py="md"><Loader size="sm" /></Group>
        ) : conversations?.length === 0 ? (
          <Text size="xs" c="dimmed" ta="center" py="md">No conversations yet</Text>
        ) : (
          <Stack gap={2}>
            {(conversations ?? []).map((c) => (
              <NavLink
                key={c.conversation_id}
                active={c.conversation_id === activeId}
                label={c.title}
                description={new Date(c.updated_at).toLocaleString()}
                leftSection={<IconMessage size={16} />}
                rightSection={
                  <ActionIcon size="xs" variant="subtle" color="red" aria-label="Delete conversation"
                    disabled={c.conversation_id === runningId} onClick={(e) => void remove(e, c.conversation_id)}>
                    <IconTrash size={12} />
                  </ActionIcon>
                }
                onClick={() => onSelect(c.conversation_id)}
                styles={{ label: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }}
              />
            ))}
          </Stack>
        )}
      </ScrollArea>
    </Stack>
  );
}
