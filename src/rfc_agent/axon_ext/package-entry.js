/**
 * The /rfc extension when rfc-agent is installed as a Pi package:
 *
 *   axon pi install git:github.com/tata1mg/O1@<tag>
 *
 * Pi clones the repo and loads this file (the root package.json names it). The clone is a full rfc-agent
 * checkout, so it is also the project the pipeline runs from: `uv run --project <clone> rfc …`. This replaces the
 * stub `python -m rfc_agent.axon_agent install` writes; use one or the other, not both.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import make from "./rfc-command.js";

export const PROJECT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

/** uv's path: RFC_AGENT_UV, then PATH, then where its installers put it. Axon may not inherit a login PATH. */
export function findUv(env = process.env, exists = fs.existsSync, home = os.homedir()) {
  if (env.RFC_AGENT_UV) return env.RFC_AGENT_UV;
  const dirs = (env.PATH || "").split(path.delimiter).filter(Boolean);
  dirs.push(path.join(home, ".local", "bin"), path.join(home, ".cargo", "bin"), "/opt/homebrew/bin", "/usr/local/bin");
  for (const d of dirs) if (exists(path.join(d, "uv"))) return path.join(d, "uv");
  return "uv";
}

export default (pi) => make(pi, { project: PROJECT, uv: findUv() });
