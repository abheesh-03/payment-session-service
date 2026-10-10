// Runs the browser suite against servers this script starts and owns.
//
//   npm run test:e2e [-- <playwright args>]
//
// 1. Refuses to start if 8020 or 3020 is already in use (never kills or
//    reuses an unknown server).
// 2. Builds the frontend (skip with E2E_SKIP_BUILD=1).
// 3. Starts the FastAPI backend (ENABLE_MOCK_FAILURES=true for this process
//    only) and `next start`, each in its own process group.
// 4. Runs Playwright, then always stops exactly those process groups.
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, openSync } from "node:fs";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const REPO = path.resolve(FRONTEND, "..");
const BACKEND = path.join(REPO, "backend");
const UVICORN = path.join(BACKEND, ".venv", "bin", "uvicorn");
const NEXT = path.join(FRONTEND, "node_modules", ".bin", "next");
const PLAYWRIGHT = path.join(FRONTEND, "node_modules", ".bin", "playwright");
const LOG_DIR = path.join(FRONTEND, "e2e-artifacts", "server-logs");
const PORTS = { backend: 8020, frontend: 3020 };
const STARTUP_TIMEOUT_MS = 60_000;
const STOP_TIMEOUT_MS = 10_000;

const children = [];

function fail(message) {
  console.error(`\n[e2e] ${message}\n`);
  process.exitCode = 1;
}

/** True if nothing listens on host:port (checked by trying to bind it). */
function portFree(port, host) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once("error", (err) => resolve(err.code === "EADDRNOTAVAIL")); // no IPv6 loopback: fine
    server.once("listening", () => server.close(() => resolve(true)));
    server.listen({ port, host, exclusive: true });
  });
}

/** True if something accepts TCP connections on host:port. */
function accepting(port, host) {
  return new Promise((resolve) => {
    const socket = net.connect({ port, host });
    socket.setTimeout(1000);
    socket.once("connect", () => {
      socket.destroy();
      resolve(true);
    });
    socket.once("timeout", () => {
      socket.destroy();
      resolve(true); // something is there but not answering: treat as in use
    });
    socket.once("error", () => resolve(false));
  });
}

async function assertPortsFree() {
  for (const [name, port] of Object.entries(PORTS)) {
    // Bind test plus connect probe: SO_REUSEADDR can let a bind succeed next
    // to a wildcard listener, so a refused connection is also required.
    const free =
      (await portFree(port, "127.0.0.1")) &&
      (await portFree(port, "::1")) &&
      !(await accepting(port, "127.0.0.1")) &&
      !(await accepting(port, "::1"));
    if (!free) {
      throw new Error(
        `Port ${port} (${name}) is already in use. Stop whatever is listening there ` +
          `(check with: lsof -nP -iTCP:${port} -sTCP:LISTEN) and rerun. ` +
          `The test runner never stops or reuses servers it did not start.`,
      );
    }
  }
}

function start(name, command, args, options) {
  mkdirSync(LOG_DIR, { recursive: true });
  const log = openSync(path.join(LOG_DIR, `${name}.log`), "w");
  const child = spawn(command, args, {
    ...options,
    detached: true, // own process group, so the whole tree can be stopped
    stdio: ["ignore", log, log],
  });
  children.push({ name, child });
  console.log(`[e2e] started ${name} (pid ${child.pid}); log: ${path.relative(FRONTEND, LOG_DIR)}/${name}.log`);
  return child;
}

async function waitForHttp(name, url, child) {
  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`${name} exited during startup (see its log).`);
    try {
      const res = await fetch(url);
      if (res.ok) return;
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`${name} did not become ready at ${url} within ${STARTUP_TIMEOUT_MS / 1000}s.`);
}

async function stopAll() {
  for (const { name, child } of children.reverse()) {
    if (child.exitCode !== null || child.signalCode !== null) continue;
    const exited = new Promise((r) => child.once("exit", r));
    try {
      process.kill(-child.pid, "SIGTERM"); // only this child's process group
    } catch {
      continue;
    }
    const timedOut = await Promise.race([
      exited.then(() => false),
      new Promise((r) => setTimeout(() => r(true), STOP_TIMEOUT_MS)),
    ]);
    if (timedOut) {
      try {
        process.kill(-child.pid, "SIGKILL");
      } catch {
        // already gone
      }
    }
    console.log(`[e2e] stopped ${name} (pid ${child.pid})`);
  }
  children.length = 0;
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, async () => {
    await stopAll();
    process.exit(130);
  });
}

try {
  if (!existsSync(UVICORN)) {
    throw new Error("backend/.venv is missing. See README: create it and install backend/requirements.txt.");
  }
  await assertPortsFree();

  if (process.env.E2E_SKIP_BUILD !== "1") {
    console.log("[e2e] building frontend (set E2E_SKIP_BUILD=1 to skip)…");
    const build = spawnSync(NEXT, ["build"], { cwd: FRONTEND, stdio: "inherit" });
    if (build.status !== 0) throw new Error("next build failed.");
  }

  const backend = start("backend", UVICORN, ["app.main:app", "--host", "127.0.0.1", "--port", String(PORTS.backend)], {
    cwd: BACKEND,
    env: { ...process.env, ENABLE_MOCK_FAILURES: "true" },
  });
  const frontend = start("frontend", NEXT, ["start", "--hostname", "127.0.0.1", "--port", String(PORTS.frontend)], {
    cwd: FRONTEND,
    env: process.env,
  });
  await waitForHttp("backend", `http://127.0.0.1:${PORTS.backend}/health`, backend);
  await waitForHttp("frontend", `http://127.0.0.1:${PORTS.frontend}/`, frontend);

  const result = spawnSync(PLAYWRIGHT, ["test", ...process.argv.slice(2)], {
    cwd: FRONTEND,
    stdio: "inherit",
    env: { ...process.env, E2E_SERVERS_STARTED_BY_RUNNER: "1" },
  });
  process.exitCode = result.status ?? 1;
} catch (error) {
  fail(error instanceof Error ? error.message : String(error));
} finally {
  await stopAll();
}
