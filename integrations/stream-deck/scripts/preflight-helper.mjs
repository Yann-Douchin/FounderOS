import { DeployedHelperBridgeClient } from "../.test-build/src/helper-client.js";

const context = await new DeployedHelperBridgeClient().getContext();
console.log(`FounderOS helper ready, protocol ${context.bridgeVersion}.`);
