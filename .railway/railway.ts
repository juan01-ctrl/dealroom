import { defineRailway, postgres, project, service } from "railway/iac";

export default defineRailway((ctx) => {
  const db = postgres("postgres");
  const shared = {
    DATABASE_URL: db.env.DATABASE_URL,
    APP_ENV: "production",
    MODEL_MODE: "live",
    MODEL_PROVIDER: ctx.shared.MODEL_PROVIDER,
    MODEL_NAME: ctx.shared.MODEL_NAME,
  };

  // Sources are connected in Railway once the deployment repository is selected.
  const api = service("api", {
    preDeploy: "python -m app.migrate",
    healthcheck: "/ready",
    healthcheckTimeout: 120,
    env: {
      ...shared,
      COOKIE_SECURE: "1",
      API_PROXY_SECRET: ctx.shared.API_PROXY_SECRET,
      OPERATIONS_TOKEN: ctx.shared.OPERATIONS_TOKEN,
    },
  });

  function worker(lane: "negotiation" | "evaluation") {
    return service(`worker-${lane}`, {
      preDeploy: "python -m app.migrate",
      start: `python -m app.worker --lane ${lane}`,
      // Store the matching provider credential on each worker service.
      env: {
        ...shared,
        LIVE_DAILY_MODEL_CALLS: ctx.shared.LIVE_DAILY_MODEL_CALLS,
      },
    });
  }

  return project("dealroom", { resources: [db, api, worker("negotiation"), worker("evaluation")] });
});
