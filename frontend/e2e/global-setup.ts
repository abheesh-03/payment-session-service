import { API_URL } from "./support/env";

export default async function globalSetup(): Promise<void> {
  if (process.env.E2E_SERVERS_STARTED_BY_RUNNER !== "1") {
    throw new Error(
      "Run the browser suite with `npm run test:e2e`; it starts and stops the test servers.",
    );
  }
  const res = await fetch(`${API_URL}/health`);
  if (!res.ok) throw new Error(`Backend /health returned ${res.status}; both PostgreSQL and Redis must be up.`);
}
