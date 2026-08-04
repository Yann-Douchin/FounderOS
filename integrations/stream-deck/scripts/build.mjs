import { rollup } from "rollup";

import configuration from "../rollup.config.mjs";

console.log("Starting the Rollup build.");
const bundle = await rollup(configuration);
console.log("Writing the Stream Deck bundle.");
await bundle.write(configuration.output);
console.log("Closing Rollup.");
await bundle.close();
console.log("Stream Deck bundle built.");
process.exit(0);
