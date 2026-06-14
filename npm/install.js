#!/usr/bin/env node
"use strict";

const { spawnSync } = require("child_process");
const { existsSync, readFileSync, writeFileSync } = require("fs");
const { join } = require("path");
const os = require("os");

// ── Opt-out-aware install decision (#123) ────────────────────────────────────
// Honors the SAME opt-outs as the SessionStart hook + Python updater — a static
// `pip install --upgrade` here would otherwise bypass them. Pure + exported so
// the behavior is unit-testable (the previous static-only test gave false
// confidence and missed a whitespace-pin divergence).
//   COZEMPIC_PIN=X.Y.Z       → install exactly that reviewed version, never --upgrade
//   COZEMPIC_PIN=<malformed>  → still an opt-out (drop --upgrade); not used as a spec
//   COZEMPIC_NO_AUTO_UPDATE   → install without --upgrade (don't move an existing install)
function decideInstall(env) {
  const noAutoUpdate = env.COZEMPIC_NO_AUTO_UPDATE;
  const pinRaw = env.COZEMPIC_PIN;
  // Non-empty raw value == pinned, EXACTLY like the hook's `[ -z "$COZEMPIC_PIN" ]`
  // (empty/unset → not pinned; whitespace-only → pinned). "" and undefined are the
  // only falsy strings, so !!pinRaw is precisely "raw is non-empty".
  const pinSet = !!pinRaw;
  // Only a version-shaped pin becomes a pip spec (no spaces/flags → no arg injection).
  const pin = pinSet && /^v?[0-9][A-Za-z0-9.+!-]*$/.test(pinRaw.trim())
    ? pinRaw.trim().replace(/^v/, "") : null;
  const spec = pin ? `cozempic==${pin}` : "cozempic";
  const up = pinSet || noAutoUpdate ? [] : ["--upgrade"];
  return { spec, up, pinSet, pin };
}

if (require.main !== module) {
  module.exports = { decideInstall };
  return;
}

// ── 1. Install or upgrade Python package ─────────────────────────────────────
const noAutoUpdate = process.env.COZEMPIC_NO_AUTO_UPDATE;
const { spec, up } = decideInstall(process.env);

const attempts = [
  ["uv", ["pip", "install", ...up, spec, "--quiet"]],
  ["pip", ["install", ...up, spec, "--quiet", "--disable-pip-version-check"]],
  ["pip3", ["install", ...up, spec, "--quiet", "--disable-pip-version-check"]],
  ["python3", ["-m", "pip", "install", ...up, spec, "--quiet"]],
  ["python", ["-m", "pip", "install", ...up, spec, "--quiet"]],
];

let installed = false;
for (const [cmd, args] of attempts) {
  try {
    const r = spawnSync(cmd, args, { stdio: "pipe", timeout: 60000 });
    if (r.status === 0) { installed = true; break; }
  } catch {}
}

if (!installed) {
  console.log("Cozempic: could not install/upgrade. Run: pip install --upgrade cozempic");
  process.exit(0);
}

// Ping install counter
try {
  const https = require("https");
  https.get("https://api.counterapi.dev/v1/cozempic/installs/up",
    { headers: { "User-Agent": "cozempic-npm" } }, () => {}).on("error", () => {});
} catch {}

// ── 2. Wire global SessionStart hook in ~/.claude/settings.json ──────────────

if (!noAutoUpdate) {
  const claudeDir = join(os.homedir(), ".claude");
  const globalSettingsPath = join(claudeDir, "settings.json");
  const hookCmd = "HOOK_DATA=$(cat); TRANSCRIPT=$(echo \"$HOOK_DATA\" | python3 -c \"import sys,json; print(json.load(sys.stdin).get('transcript_path',''))\" 2>/dev/null); { cozempic guard --daemon ${TRANSCRIPT:+--session $TRANSCRIPT} 2>/dev/null || python3 -m cozempic guard --daemon ${TRANSCRIPT:+--session $TRANSCRIPT} 2>/dev/null; } || true";

  try {
    if (existsSync(claudeDir)) {
      let settings = {};
      if (existsSync(globalSettingsPath)) {
        try { settings = JSON.parse(readFileSync(globalSettingsPath, "utf8")); } catch {}
      }
      settings.hooks = settings.hooks || {};
      settings.hooks.SessionStart = settings.hooks.SessionStart || [];
      const alreadyWired = settings.hooks.SessionStart.some(h =>
        (h.hooks || []).some(hh => hh.command && hh.command.includes("cozempic"))
      );
      if (!alreadyWired) {
        settings.hooks.SessionStart.push({
          hooks: [{ type: "command", command: hookCmd }]
        });
        writeFileSync(globalSettingsPath, JSON.stringify(settings, null, 2));
      }
    }
  } catch {}
}

// ── 3. Auto-configure if inside a Claude Code project ────────────────────────

const cwd = process.env.INIT_CWD || process.cwd();

if (!noAutoUpdate) {
  try {
    if (existsSync(join(cwd, ".claude"))) {
      let r = spawnSync("cozempic", ["init", "--quiet"], { stdio: "pipe", cwd });
      if (r.status !== 0) {
        spawnSync("python3", ["-m", "cozempic", "init", "--quiet"], { stdio: "pipe", cwd });
      }
    }
  } catch {}
}
