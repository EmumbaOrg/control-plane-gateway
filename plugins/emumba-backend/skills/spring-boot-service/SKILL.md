---
name: spring-boot-service
description: Spring Boot service structure for Emumba — layering, configuration, transactions, exception handling, and testing. Use when writing or reviewing Java/Kotlin Spring Boot code.
version: 0.1.0
author: Emumba
tags: [Backend, Java, Kotlin, Spring Boot, JPA]
dependencies: []
---

# Spring Boot Service Standards

Applies the conventions in `rest-api-conventions` to a Spring Boot codebase.

> Draft — pending sign-off by the backend guild.

## When to use this skill

- Adding a controller, service, or repository to a Spring Boot project
- Reviewing Spring Boot code for structure or transaction correctness
- Diagnosing lazy-loading, proxy, or transaction-boundary problems

## Layering

```
controller/   HTTP only — bind, validate, delegate, map to DTO
service/      Business rules and transaction boundaries
repository/   Data access, one per aggregate root
domain/       Entities and value objects, no Spring annotations beyond JPA
dto/          Request and response records, never entities on the wire
```

Never return a JPA entity from a controller. It couples the wire format to the
schema and serializes lazy proxies at the worst possible moment.

## Dependency injection

Constructor injection only. No `@Autowired` on fields — it hides required
dependencies and blocks construction in tests.

```java
@Service
public class OrderService {
    private final OrderRepository orders;

    public OrderService(OrderRepository orders) {
        this.orders = orders;
    }
}
```

## Transactions

- `@Transactional` belongs on the service method, never the controller or
  repository.
- Mark read paths `@Transactional(readOnly = true)` — it lets the driver skip
  dirty checking and can route to a replica.
- Self-invocation does not go through the proxy, so a `@Transactional` method
  called from within the same class is **not** transactional. Extract it.
- Never call an external HTTP API inside a transaction. The connection is held
  for the duration of the remote call.

## Configuration

- Bind config to typed `@ConfigurationProperties` records, not scattered
  `@Value` strings.
- Secrets come from the environment or the secrets manager — never from
  `application.yml` committed to the repo.
- Profile-specific overrides live in `application-{profile}.yml`; the base file
  holds only safe defaults.

## Exception handling

One `@RestControllerAdvice` per service, producing the standard error
envelope:

```java
@RestControllerAdvice
class ApiExceptionHandler {
    @ExceptionHandler(NotFoundException.class)
    ResponseEntity<ApiError> notFound(NotFoundException e) {
        return ResponseEntity.status(404)
            .body(ApiError.of("order_not_found", e.getMessage()));
    }
}
```

Let the advice map domain exceptions to status codes. Controllers should not
build error responses inline.

## Persistence

- Default fetch type `LAZY` on every `@ManyToOne` and `@OneToOne`; JPA's
  default is EAGER and it is almost always wrong.
- Solve N+1 with an explicit `join fetch` query or an entity graph, not by
  switching to EAGER.
- Schema changes go through Flyway or Liquibase migrations. `ddl-auto` is
  `validate` in every environment above local, never `update`.

## Testing

- `@WebMvcTest` for controllers with the service mocked — fast, no context.
- `@SpringBootTest` with Testcontainers for repository and integration tests.
  An in-memory H2 standing in for Postgres hides dialect and constraint bugs.
- Assert on status code and error `code`, not on the human-readable message.

## Checklist

- [ ] Constructor injection throughout
- [ ] `@Transactional` on service methods only; read paths `readOnly`
- [ ] No remote calls inside a transaction
- [ ] DTOs on the wire, never entities
- [ ] Associations `LAZY`, N+1 handled by fetch joins
- [ ] Migrations checked in, `ddl-auto: validate`
