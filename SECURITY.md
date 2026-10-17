# Security Policy

## What this repository is

`atomic-admission` bounds agent authority at the meter: it stops agent over-spend before bytes move by requiring vendor-side atomic admission checks with zero outbound authority.

## Reporting a vulnerability

**Preferred: GitHub private vulnerability reporting.** Open the **Security** tab on this repository and choose **Report a vulnerability**. That channel is private between you and the maintainer, requires no email, and nothing is posted publicly.

If you cannot use that channel, open a **minimal public issue** stating only that you have a security report and how to reach you. Please do **not** include exploit details, proof-of-concept code, or affected-version specifics in a public issue.

## Scope

**In scope:**
- Any path where an admission token or spend authority can be bypassed, replayed, or forged.
- Concurrency or race conditions allowing over-spend past configured limits.
- Deserialization or validation failures in request processing.

**Out of scope / stated plainly:**
- Deployment configurations outside the reference admission harness.
- Network-level DoS attacks against the hosting server.

## What to expect

| Stage | Commitment |
|---|---|
| Acknowledgement of your report | within 7 days |
| Initial assessment and severity call | within 14 days |
| Fix, or an agreed public disclosure | coordinated with you |

You will be credited in the fix or advisory unless you ask to remain anonymous.

## What this policy does NOT offer

There is **no bug bounty**, and no monetary reward is offered or implied. This is an independent research project maintained by one person. What it can offer is a fast, honest response and public credit.
