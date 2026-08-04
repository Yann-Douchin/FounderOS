import streamDeck from "@elgato/streamdeck";

import {
  AcknowledgeAction,
  AllowAction,
  DenyAction,
  OpenAction,
  PresenceAction,
  SnoozeAction,
  StatusAction
} from "./actions.js";
import { FounderOSCoordinator } from "./coordinator.js";
import { DeployedHelperBridgeClient } from "./helper-client.js";

const bridge = new DeployedHelperBridgeClient();
const coordinator = new FounderOSCoordinator(bridge, 2000, (message) => {
  streamDeck.logger.debug(message);
});

streamDeck.actions.registerAction(new StatusAction(coordinator));
streamDeck.actions.registerAction(new OpenAction(coordinator));
streamDeck.actions.registerAction(new SnoozeAction(coordinator));
streamDeck.actions.registerAction(new AcknowledgeAction(coordinator));
streamDeck.actions.registerAction(new AllowAction(coordinator));
streamDeck.actions.registerAction(new DenyAction(coordinator));
streamDeck.actions.registerAction(new PresenceAction(coordinator));

process.once("SIGTERM", () => coordinator.close());
process.once("SIGINT", () => coordinator.close());

streamDeck.connect();
