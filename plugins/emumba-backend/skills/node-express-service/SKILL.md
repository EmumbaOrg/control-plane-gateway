---
name: node-express-service
description: Node and Express service structure for Emumba — routing, async error handling, validation, configuration, and graceful shutdown. Use when writing or reviewing Node/Express backend code.
version: 0.1.0
author: Emumba
tags: [Backend, Node, Express, TypeScript]
dependencies: []
---

# Node / Express Service Standards

Applies the conventions in `rest-api-conventions` to a Node/Express codebase.

> Draft — pending sign-off by the backend guild.

## When to use this skill

- Adding a route, middleware, or service module to an Express app
- Reviewing Node backend code for error handling or config hygiene
- Diagnosing unhandled rejections, leaked handles, or hung shutdowns

## Layout

```
src/
  routes/      Express routers — parse, validate, delegate
  services/    Business logic, framework-free and unit-testable
  repos/       Data access
  middleware/  Cross-cutting: auth, request id, error handler
  config/      One parsed, validated config object
```

Services must not import `express`. If a service needs the request, pass the
values it needs, not `req`.

## Async error handling

Express 4 does not catch rejected promises from async handlers — the request
hangs until it times out. Wrap every async handler:

```ts
const wrap = (fn: RequestHandler): RequestHandler =>
  (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);

router.get('/orders/:id', wrap(async (req, res) => {
  res.json(await orders.byId(req.params.id));
}));
```

Express 5 forwards rejections automatically — check the major version before
assuming either behaviour.

One terminal error middleware, registered last, produces the standard error
envelope and is the only place that formats errors.

```ts
app.use((err, req, res, _next) => {
  const status = err.status ?? 500;
  if (status >= 500) log.error({ err, requestId: req.id }, 'unhandled');
  res.status(status).json({
    error: { code: err.code ?? 'internal_error', message: publicMessage(err), details: err.details ?? [] },
  });
});
```

Never pass `err.message` straight through on a 500 — it leaks driver and
filesystem detail. Return a correlation id and log the rest.

## Validation

Validate at the boundary with a schema library (Zod, Valibot) and pass the
parsed, typed value inward. Hand-rolled `if (!req.body.x)` chains drift from
the types and miss coercion bugs.

```ts
const CreateOrder = z.object({ sku: z.string().min(1), quantity: z.number().int().positive() });
const body = CreateOrder.parse(req.body);
```

Map schema failures to 422 with per-field `details`.

## Configuration

Parse the environment once at startup into a frozen, validated object, and
fail fast if anything required is missing. Reading `process.env` deep inside a
module makes the failure surface on the first request instead of at boot.

Secrets come from the environment or the secrets manager. Never commit a
`.env` with live values; commit `.env.example` with placeholders.

## Operational basics

- Assign a request id in middleware and include it in every log line and every
  error response.
- Log structured JSON (pino), never `console.log`, and never log request
  bodies containing credentials or PII.
- Handle `SIGTERM`: stop accepting connections, drain in-flight requests, close
  the DB pool, then exit. Without this, deploys drop live requests.
- Set an explicit timeout on every outbound HTTP call. Node's default is no
  timeout, so one slow upstream exhausts the event loop.

## Testing

- Unit-test services directly — they have no framework dependency.
- Integration-test routes with `supertest` against the real app and a
  Testcontainers database.
- Assert on status and error `code`, not on the message text.

## Checklist

- [ ] Every async handler wrapped, or Express 5 confirmed
- [ ] One terminal error middleware, standard envelope, no internals leaked
- [ ] Request bodies schema-validated at the boundary
- [ ] Config parsed and validated once at boot
- [ ] Request id on every log line and error response
- [ ] SIGTERM drains connections; outbound calls have timeouts
