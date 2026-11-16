// Fail closed before putting a persistent App key into the bridge service.
import { pathToFileURL } from "node:url";
import { appJWT, github } from "./worker.mjs";

export async function auditDispatchApp(env, request = fetch) {
  const jwt = await appJWT(env.BRIDGE_APP_ID, env.BRIDGE_APP_PRIVATE_KEY);
  const app = await github("app", jwt, undefined, request);
  const permissions = app.permissions || {};
  if (permissions.actions !== "write" || Object.keys(permissions).some(k => !["actions", "metadata"].includes(k)))
    throw new Error("dispatch App permissions are not restricted");
  const installations = await github("app/installations?per_page=100", jwt, undefined, request);
  if (!Array.isArray(installations) || installations.length !== 1 ||
      installations[0].account?.login !== "AIOSCompanion" || installations[0].repository_selection !== "selected")
    throw new Error("dispatch App installation is not restricted");
  const token = await github(`app/installations/${installations[0].id}/access_tokens`, jwt,
    { permissions: { actions: "write" } }, request);
  const repositories = await github("installation/repositories?per_page=100", token.token, undefined, request);
  if (repositories.total_count !== 1 || repositories.repositories?.length !== 1 ||
      repositories.repositories[0].full_name !== "AIOSCompanion/companion-builder")
    throw new Error("dispatch App can access other repositories");
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    await auditDispatchApp(process.env);
    console.log("Dispatch App permissions and builder-only installation verified");
  } catch {
    console.error("Dispatch App scope audit failed; no credentials or private responses printed");
    process.exitCode = 1;
  }
}
