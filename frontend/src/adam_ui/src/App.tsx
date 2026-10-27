//  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
//  SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from "react";
import { Amplify } from "aws-amplify";
import { getCurrentUser, signInWithRedirect, signOut } from "aws-amplify/auth";
import "aws-amplify/auth/enable-oauth-listener";
import {
  AppShell,
  Group,
  Title,
  ActionIcon,
  useMantineColorScheme,
  ThemeIcon,
  Tabs,
  Button,
  Center,
  Stack,
  Text,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconSun, IconMoon, IconDatabase, IconChartBar, IconLogout, IconMessageCircle, IconPlugConnected, IconRadar } from "@tabler/icons-react";
import { AdminPage } from "./pages/AdminPage";
import { ChatPage } from "./pages/ChatPage";
import { ConversationSidebar } from "./components/ConversationSidebar";
import { ConnectionsPage } from "./pages/ConnectionsPage";
import { callerGroups, errorText } from "./services/localApi";
import awsConfig from "./aws-exports";

Amplify.configure({
  Auth: {
    Cognito: {
      userPoolId: awsConfig.userPoolId,
      userPoolClientId: awsConfig.userPoolClientId,
      // Sign-in through Cognito's OAuth flow (code + PKCE), so the user's token carries
      // the gateway scope the chat agent forwards to the AgentCore Gateway.
      loginWith: {
        oauth: {
          domain: awsConfig.cognitoDomain,
          scopes: ["openid", "email", "profile", awsConfig.gatewayScope],
          redirectSignIn: [`${window.location.origin}/`],
          redirectSignOut: [`${window.location.origin}/`],
          responseType: "code",
        },
      },
    },
  },
  // The chat page's answers stream over AppSync Events.
  API: {
    Events: {
      endpoint: `https://${awsConfig.eventsHttpEndpoint}/event`,
      region: awsConfig.region,
      defaultAuthMode: "userPool",
    },
  },
});

// The backend deploys the chat agent only with ENABLE_CHAT; the page follows it.
const CHAT_ENABLED = awsConfig.agentRuntimeArn !== "";

function AppInner({ signOut }: { signOut: () => void }) {
  const { colorScheme, toggleColorScheme } = useMantineColorScheme();
  const [activeTab, setActiveTab] = useState<string | null>(CHAT_ENABLED ? "chat" : "architecture");
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [historyVersion, setHistoryVersion] = useState(0);
  const [runningConversation, setRunningConversation] = useState<string | null>(null);
  const chatOpen = CHAT_ENABLED && activeTab === "chat";
  // Shows the tab only; the admin routes check the group themselves.
  const [isAdmin, setIsAdmin] = useState(false);
  useEffect(() => {
    callerGroups()
      .then((groups) => setIsAdmin(groups.includes("admin")))
      .catch((err: unknown) => notifications.show({
        title: "Could not read your groups", message: `The Admin tab stays hidden: ${errorText(err)}`, color: "red",
      }));
  }, []);

  return (
    <AppShell
      header={{ height: 56 }}
      navbar={{ width: 260, breakpoint: "sm", collapsed: { mobile: !chatOpen, desktop: !chatOpen } }}
      padding="md"
    >
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group gap="sm">
            <ThemeIcon size="md" variant="gradient" gradient={{ from: "blue", to: "cyan" }}>
              <IconDatabase size={16} />
            </ThemeIcon>
            <Title order={4}>Agentic Data Automation Manager (ADAM)</Title>
            <Tabs value={activeTab} onChange={setActiveTab} variant="pills" ml="lg">
              <Tabs.List>
                {CHAT_ENABLED && (
                  <Tabs.Tab value="chat" leftSection={<IconMessageCircle size={14} />}>
                    Chat
                  </Tabs.Tab>
                )}
                <Tabs.Tab value="architecture" leftSection={<IconChartBar size={14} />}>
                  Architecture
                </Tabs.Tab>
                <Tabs.Tab value="connections" leftSection={<IconPlugConnected size={14} />}>
                  Connections
                </Tabs.Tab>
                {isAdmin && (
                  <Tabs.Tab value="admin" leftSection={<IconRadar size={14} />}>
                    Admin
                  </Tabs.Tab>
                )}
              </Tabs.List>
            </Tabs>
          </Group>
          <Group gap="sm">
            <ActionIcon
              variant="subtle"
              onClick={toggleColorScheme}
              aria-label="Toggle color scheme"
            >
              {colorScheme === "dark" ? <IconSun size={18} /> : <IconMoon size={18} />}
            </ActionIcon>
            <Button
              variant="subtle"
              size="xs"
              leftSection={<IconLogout size={16} />}
              onClick={() => signOut()}
            >
              Sign out
            </Button>
          </Group>
        </Group>
      </AppShell.Header>
      {chatOpen && (
        <AppShell.Navbar>
          <ConversationSidebar
            activeId={conversationId}
            onSelect={setConversationId}
            onNew={() => setConversationId(null)}
            refreshKey={historyVersion}
            runningId={runningConversation}
          />
        </AppShell.Navbar>
      )}
      <AppShell.Main>
        {chatOpen ? (
          <ChatPage
            conversationId={conversationId}
            onConversationStarted={setConversationId}
            onConversationsChanged={() => setHistoryVersion((v) => v + 1)}
            onTurnRunning={setRunningConversation}
          />
        ) : activeTab === "connections" ? (
          <ConnectionsPage />
        ) : activeTab === "admin" && isAdmin ? (
          <AdminPage />
        ) : activeTab === "architecture" ? (
          <a href="/AWS-architecture.png" target="_blank" rel="noreferrer">
            <img
              src="/AWS-architecture.png"
              alt="Architecture diagram"
              style={{ width: "100%", height: "auto" }}
            />
          </a>
        ) : null}
      </AppShell.Main>
    </AppShell>
  );
}

export default function App() {
  const [signedIn, setSignedIn] = useState(false);
  const [signInProblem, setSignInProblem] = useState<string | null>(null);
  useEffect(() => {
    getCurrentUser().then(
      () => setSignedIn(true),
      () => signInWithRedirect().catch((err: unknown) => setSignInProblem(errorText(err))),
    );
  }, []);
  if (signInProblem) {
    return (
      <Center h="100vh">
        <Stack align="center" gap="sm">
          <Text c="red">Could not start signing in: {signInProblem}</Text>
          <Button onClick={() => window.location.reload()}>Try again</Button>
        </Stack>
      </Center>
    );
  }
  return signedIn ? <AppInner signOut={() => void signOut().then(() => window.location.reload())} /> : null;
}
