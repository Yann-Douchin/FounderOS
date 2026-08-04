import commonjs from "@rollup/plugin-commonjs";
import { nodeResolve } from "@rollup/plugin-node-resolve";
import typescript from "@rollup/plugin-typescript";

export default {
  input: "src/plugin.ts",
  output: {
    file: "com.yanndouchin.founderos-actions.sdPlugin/bin/plugin.js",
    format: "es",
    sourcemap: false
  },
  external: [/^node:/],
  plugins: [
    nodeResolve({ preferBuiltins: true }),
    commonjs(),
    typescript({
      tsconfig: "./tsconfig.json",
      noEmitOnError: true
    })
  ],
  onwarn(warning, warn) {
    if (warning.code !== "CIRCULAR_DEPENDENCY") {
      warn(warning);
    }
  }
};
