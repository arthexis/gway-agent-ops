# AgentOps terminal LCD/Sound notifications

See [LCD Sound #74](https://github.com/arthexis/gway-lcd-sound/issues/74).
The existing `reporting.enqueue` remains authoritative. Once it commits a
new issue/invocation terminal record to the private outbox, an optional
best-effort hook emits the typed `gway-notification/v1` LCD message and
one named sound. Re-reading the same record returns without another sound.

Identity is the full `arthexis/repository#issue`, the accepted invocation
timestamp `at` (hashed into a bounded episode ID), and the executor node.
LCD displays `AGT@001 FAILED` / repository and `ISSUE #N` /
`CODEX FAILED`, with extra pages for the complete repository when needed.
This preserves links to the precise issue without displaying untrusted issue
titles, findings, logs, credentials or prompt data.

Status `finished` means the Codex process returned zero, **not** that a
repository PR or external administrative effect was independently verified.
The LCD calls that `PROCESS ENDED` and plays a neutral notice. Actual
`codex_failed` and `interrupted` events are warnings. A missing LCD,
playback failure or uncertain transport cannot fail Codex, alter its report,
or trigger another attempt. Hooking Codex's generic `agent-turn-complete`
would lack the issue identity and is intentionally not used.

The hook defaults off in production and in tests. After the GWay Remote
receiver and LCD Sound renderer are compatible and validated, the gway-001
operator can create `~/.config/gway-agent-ops/lcd-notifications.enabled`
as a private, owner-only **0600** file containing `enabled`. The exact
user owning the AgentOps state/reporter must own this marker. A trusted
environment alternative is `GWAY_AGENTOPS_LCD_NOTIFICATIONS=1`.
No new Watchtower bearer, SSH identity, agent execution permission or job
runner is needed for the local gway-001 producer.

For rollback, remove only the opt-in marker; do not touch AgentOps job state,
GitHub reporting outbox, Codex sessions, or the parent LCD/Sound installation.
