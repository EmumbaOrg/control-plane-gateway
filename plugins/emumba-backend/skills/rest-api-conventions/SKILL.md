---
name: rest-api-conventions
description: Stack-agnostic REST API conventions for Emumba services — resource naming, status codes, pagination, error envelopes, and versioning. Use when designing, reviewing, or extending an HTTP API.
version: 0.1.0
author: Emumba
tags: [Backend, API, REST, HTTP, Conventions]
dependencies: []
---

# REST API Conventions

Applies to any Emumba service exposing HTTP, regardless of language or framework.

> Draft — pending sign-off by the backend guild. Treat as a proposed standard,
> not an approved one.

## When to use this skill

- Designing a new HTTP endpoint or resource
- Reviewing a PR that adds or changes an API surface
- Reconciling two services that disagree on response shape

## Resource naming

- Plural nouns for collections: `/orders`, not `/order` or `/getOrders`
- Nest only one level deep: `/orders/{id}/items`. Deeper nesting means the
  child deserves its own top-level resource.
- Verbs belong in the method, not the path. The exception is a genuine action
  that is not a state change on one resource: `POST /orders/{id}/refunds`.
- `kebab-case` in paths, `snake_case` or `camelCase` in bodies — pick one per
  service and never mix within it.

## Status codes

| Code | Use for |
|---|---|
| 200 | Successful read or update returning a body |
| 201 | Created, with a `Location` header pointing at the new resource |
| 204 | Successful delete, or update with no body |
| 400 | Malformed request the client can fix by changing the payload |
| 401 | No or invalid credentials |
| 403 | Valid credentials, insufficient permission |
| 404 | Resource absent — also use to hide existence from unauthorized callers |
| 409 | Conflict with current state (duplicate key, version mismatch) |
| 422 | Well-formed but semantically invalid |
| 429 | Rate limited, with `Retry-After` |

Never return 200 with an error body. Clients branch on status first.

## Error envelope

One shape for every non-2xx response:

```json
{
  "error": {
    "code": "order_not_found",
    "message": "No order with that id.",
    "details": []
  }
}
```

- `code` is stable, `snake_case`, and safe to branch on in client code.
- `message` is for humans and may change without a version bump.
- `details` carries per-field validation failures: `[{"field": "quantity", "code": "min_value"}]`.
- Never leak stack traces, SQL, internal hostnames, or upstream vendor errors
  into `message`. Log those; return a correlation id instead.

## Pagination

Cursor-based by default:

```
GET /orders?limit=50&cursor=eyJpZCI6MTIzfQ
```

```json
{ "data": [], "next_cursor": "eyJpZCI6MTczfQ" }
```

`next_cursor` absent means the end. Offset pagination is acceptable only for
small, stable admin listings — it drifts under concurrent writes and degrades
on large tables.

Cap `limit` server-side (50 default, 200 max) and clamp rather than erroring.

## Versioning

- Version in the path: `/v1/orders`.
- Additive changes (new optional field, new endpoint) do not bump the version.
- Removing a field, renaming one, tightening validation, or changing a status
  code is breaking — new version, and the old one stays up through the
  deprecation window.
- Announce deprecation with a `Sunset` header before removal.

## Idempotency

Any `POST` that moves money, sends a message, or creates a billable record
must accept an `Idempotency-Key` header and return the original response on
replay. `PUT` and `DELETE` must be naturally idempotent.

## Checklist before merging an endpoint

- [ ] Path is a plural noun, at most one nesting level
- [ ] Every failure path returns the standard error envelope
- [ ] Collection endpoints paginate and cap `limit`
- [ ] Auth failures distinguish 401 from 403
- [ ] No internal detail in any client-visible message
- [ ] Side-effecting POSTs accept `Idempotency-Key`
