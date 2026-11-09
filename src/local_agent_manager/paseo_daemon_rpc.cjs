"use strict";

const fs = require("node:fs");
const { randomUUID } = require("node:crypto");
const { createRequire } = require("node:module");
const { join } = require("node:path");
const { pathToFileURL } = require("node:url");

async function main() {
  const archive = process.argv[2];
  const request = JSON.parse(fs.readFileSync(0, "utf8"));
  const fromBundle = createRequire(join(archive, "node_modules", "@getpaseo", "cli", "package.json"));
  const clientVersion = JSON.parse(fs.readFileSync(join(archive, "node_modules", "@getpaseo", "client", "package.json"), "utf8")).version;
  if (clientVersion !== request.version) throw new Error();
  const { createHostClient } = await import(pathToFileURL(fromBundle.resolve("@getpaseo/client/node")).href);
  const { readLocalCredentialForTarget } = await import(pathToFileURL(fromBundle.resolve("@getpaseo/server/daemon-control")).href);
  const client = createHostClient(request.endpoint, {
    clientId: randomUUID(),
    clientType: "cli",
    appVersion: clientVersion,
    localCredential: () => readLocalCredentialForTarget(request.root, request.endpoint) ?? undefined,
    reconnect: { enabled: false },
  });
  try {
    await client.connect();
    const server = client.getLastServerInfoMessage();
    if (server?.serverId !== request.server_id || server.version !== request.version) throw new Error();
    const findExact = async () => {
      const result = await client.fetchAgent({ agentId: request.agent_id });
      if (result?.agent?.id !== request.agent_id) return null;
      if (result.agent.provider !== request.provider) throw new Error();
      return result.agent;
    };
    let agent = await findExact();
    if (request.verb === "delete") {
      if (!agent) throw new Error();
      // The CLI resolves identifiers with title/prefix fallbacks. The delete
      // RPC itself takes the exact storage key on this verified connection.
      await client.deleteAgent(request.agent_id);
      agent = await findExact();
    } else if (request.verb !== "probe") {
      throw new Error();
    }
    process.stdout.write(JSON.stringify(Boolean(agent)));
  } finally {
    await client.close();
  }
}

main().catch(() => { process.exitCode = 1; });
