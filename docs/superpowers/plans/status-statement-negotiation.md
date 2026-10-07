# Status-Statement Energy Negotiation — Development Plan

## Goal

Request a `status-statement` through `MasterClient`, use it to derive and publish
an energy proposal, wait for a response, and publish a `transfer` to `central`
when the response type is `take`.

## Agreed decisions

- Set the AMQP publish property `user_id` to `city.TAL`. This is a publish
  property, not a message-body field.
- If `energy_delta` is negative, propose `direction: "take"` with
  `quantity: round(abs(energy_delta), 1)`.
- If `energy_delta` is positive, propose `direction: "give"` with
  `quantity: round(energy_delta, 1)`.
- Set `pricePerEnergy` to `round(1.05 * generationCost, 1)`.
- Use Python’s built-in `round(number, 1)` for these calculations.
- `generationCost` comes from the `status-statement` requested through
  `MasterClient`.
- Do not suppress or adjust a proposal when
  `quantity * pricePerEnergy` exceeds the available budget.
- Send transfers to `central`. Set the transfer's `becauseOf` to the
  `msgId` of the corresponding `negotiation-proposal`.
- Use the `error` message type for an error response; do not introduce an
  application-level `nack` type.
- Drop the negotiation if the second 30-second response wait times out.

## Implementation plan

1. **Define and validate message schemas**
   - Add or update schemas for `status-statement`, `negotiation-proposal`,
     `give`, `take`, `error`, and `transfer`.
   - Validate required fields, types, identifiers, and timestamps at ingress.
   - Ensure each published message has a new UUID `msgId`, and its `idpk` differs
     from `msgId`.

2. **Request the status statement and build the proposal**
   - Request the `status-statement` through `MasterClient` and read
     `energy_delta` and `generationCost` from its response.
   - Calculate proposal `direction`, `quantity`, and `pricePerEnergy` using the
     agreed rules.
   - Set envelope property `cityId` to `TAL` and AMQP publish property
     `user_id` to `city.TAL`.
   - Do not reject or change the proposal because its estimated total exceeds
     the available budget.
   - Test positive, negative, and zero `energy_delta`, rounding, and price
     calculation.

3. **Add response correlation and workflow orchestration**
   - Dispatch responses to active workflows using `data.target`.
   - Wait up to 30 seconds for `give`, `take`, or `error`.
   - On timeout, retry exactly once with the same `idpk` and business payload,
     but a new `msgId`.
   - Accept a valid delayed response to either attempt.
   - Drop the negotiation if the second wait times out.
   - Keep the consumer able to process other deliveries while workflows wait;
     do not block the message-consumption loop for 30 seconds.

4. **Publish transfers safely**
   - For a valid `take` response, publish a `transfer` to `central`.
   - Set `becauseOf` to the `msgId` of the corresponding
     `negotiation-proposal`.
   - Calculate the transfer `quantity` from `energy_delta` using
     `round(abs(energy_delta), 1)`.
   - Set the transfer’s AMQP envelope property `cityId` to `TAL` and publish
     property `user_id` to `city.TAL`.
   - Make transfer publication idempotent so duplicate status statements or
     responses cannot cause duplicate transfers.

5. **Handle delivery, persistence, and shutdown**
   - Acknowledge an incoming status statement only after its workflow is safely
     recorded or completed, consistent with the existing ACK policy.
   - Deduplicate by `idpk`; never apply an already-applied `idpk` to the ledger,
     but record it as a duplicate.
   - Choose durable workflow state/outbox behavior in accordance with ADRs
     AD1–AD3, so a restart does not lose an in-flight negotiation.
   - Use bounded retries and backoff for publish failures, and ensure active
     workflows shut down cleanly.

## Verification

- Unit-test proposal calculations, validation, and each response type.
- Test the first timeout, single retry, second timeout/drop, and delayed
  responses to either attempt.
- Test proposals whose estimated total exceeds the available budget to verify
  they are still sent.
- Test transfer correlation, quantity rounding, duplicate deliveries/responses,
  idempotent transfer publication, and ledger deduplication.
- Test broker/master failures and shutdown during an active negotiation.
- Add integration tests for message schemas, envelope properties, publish
  properties, and broker routing.
- Run the backend test suite and relevant formatting/lint checks.

## Completion checklist

- Contracts and implementation agree on all message fields and semantics.
- Published messages use envelope property `cityId: "TAL"` and AMQP publish
  property `user_id: "city.TAL"`.
- Transfers are sent to `central`, reference the proposal through `becauseOf`,
  and use the agreed quantity calculation.
- Existing connector ACK/requeue behavior remains correct for malformed
  messages and unavailable services.
- Tests cover success, error, timeout/drop, retries, duplicates, and recovery.
- Add the required AI-log entry under `docs/ai-logs/` in the same PR, following
  `AGENTS.md`.